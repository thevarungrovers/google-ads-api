"""Tests for the boundary between reading and writing.

The OAuth scope this project uses (`.../auth/adwords`) has no read-only
variant: the token is fully capable of changing the account. Nothing outside
this repo stops a write, so these tests are the enforcement.

The invariant: everything that can mutate lives in
`googleads_reporting/write/`, and no other source file can.
"""

import ast
import re
from pathlib import Path

import pytest

from googleads_reporting import client as client_module
from googleads_reporting.client import (
    ALLOWED_METHODS,
    ALLOWED_SERVICES,
    ReadOnlyGoogleAdsClient,
    ReadOnlyViolation,
    _ReadOnlyService,
)
from googleads_reporting.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Source-tree scan
# --------------------------------------------------------------------------

#: Any attribute or string that names a mutate method.
MUTATE_NAME = re.compile(r"^mutate_?\w*$")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of Constant nodes that are docstrings, not code."""
    found = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def mutate_hits(source: str) -> list[tuple[int, str]]:
    """Every place ``source`` reaches a mutate method, as (line, what).

    Parsed with ``ast`` rather than matched with a regex, for two reasons the
    regex version got wrong in practice:

    * A comment mentioning ``.mutate()`` produced a false hit, so the old scan
      needed a "skip lines starting with # or a quote" heuristic...
    * ...and that heuristic then hid a REAL hit, because a dispatch-table entry
      ``"mutate_campaigns": ...`` is a line starting with a quote. The
      mutating client dispatches exactly that way, which is how the gap
      surfaced.

    The AST has neither problem: comments are not in it, docstrings are
    identified precisely, and a dict key is just a string constant.

    This remains a tripwire, not a sandbox -- code that assembles a method name
    at runtime can defeat it. It is worth having because the failure it
    prevents is someone adding a write in the obvious way without noticing
    which guarantee they broke.
    """
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        # A CALL, not a bare attribute access. `service.mutate` that is never
        # invoked changes nothing, and the read-only guard in
        # scripts/test_connection.py touches exactly that attribute in order to
        # assert it is REFUSED. Flagging the access would make the proof of the
        # guarantee look like a breach of it.
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and MUTATE_NAME.match(func.attr):
                hits.append((func.lineno, f".{func.attr}()"))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and MUTATE_NAME.match(node.value)
        ):
            hits.append((node.lineno, repr(node.value)))
    return sorted(set(hits))


#: The ONE directory allowed to mutate. Everything else in the repository is
#: held to read-only. Widening this tuple is the deliberate act that enlarges
#: the write surface -- it is not a formality.
WRITE_SURFACE = (PROJECT_ROOT / "googleads_reporting" / "write",)


def _source_files() -> list[Path]:
    """Every source file that must NOT be able to mutate."""
    files = []
    for directory in ("googleads_reporting", "scripts"):
        for path in sorted((PROJECT_ROOT / directory).rglob("*.py")):
            if any(path.is_relative_to(allowed) for allowed in WRITE_SURFACE):
                continue
            files.append(path)
    return files


def test_the_write_surface_exists_and_is_excluded():
    """Guards the exclusion itself.

    If write/ were deleted or moved, the scan below would still pass -- over a
    set that no longer contains the thing it was carved out for. Pinning it
    means the carve-out cannot quietly become unbounded.
    """
    for directory in WRITE_SURFACE:
        assert directory.is_dir(), directory
    scanned = {p.resolve() for p in _source_files()}
    write_files = {
        p.resolve()
        for directory in WRITE_SURFACE
        for p in directory.rglob("*.py")
    }
    assert write_files, "write/ contains no Python files"
    assert not (scanned & write_files), "write/ leaked into the scanned set"


def test_the_write_surface_really_does_mutate():
    """The exclusion must be load-bearing, not decorative.

    If write/ contained no mutate call, the scan would pass everywhere and this
    whole arrangement would prove nothing.
    """
    found = []
    for directory in WRITE_SURFACE:
        for path in directory.rglob("*.py"):
            for line, what in mutate_hits(path.read_text()):
                found.append(f"{path.name}:{line}: {what}")
    assert found, (
        "write/ makes no mutate call, so excluding it from the scan proves "
        "nothing. Either the write package is not wired up, or the scan is "
        "misconfigured."
    )


def test_source_files_were_actually_found():
    """A scan over an empty list passes vacuously; make sure it is not."""
    files = _source_files()
    assert len(files) >= 5, [str(f) for f in files]
    assert any(f.name == "client.py" for f in files)
    # The read-only client specifically must be in scope.
    assert any(
        f == PROJECT_ROOT / "googleads_reporting" / "client.py" for f in files
    )


def test_no_mutate_call_anywhere_in_the_source_tree():
    offenders = []
    for path in _source_files():
        for line, what in mutate_hits(path.read_text()):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line}: {what}")
    assert offenders == [], (
        "Only googleads_reporting/write/ may mutate. Found mutate call(s) "
        "outside it:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "source",
    [
        "service.mutate(request=req)",
        "client.get_service('CampaignService').mutate_campaigns(x)",
        # Indirect forms a `.mutate(` regex misses entirely:
        'getattr(service, "mutate_campaigns")(request=r)',
        'METHODS = {"mutate_ad_groups": AdGroupService}',
        "op = getattr(svc, 'mutate_ad_group_ads')",
        'name = "mutate_campaign_budgets"',
    ],
)
def test_the_scan_catches_a_real_mutate(source):
    """Negative control: a scan matching nothing would pass every file."""
    assert mutate_hits(source), source


@pytest.mark.parametrize(
    "source",
    [
        "# never call .mutate on a service",
        "search_stream(customer_id=cid, query=q)",
        "    # mutate_campaigns is not permitted here",
        '"""This module must never call mutate_campaigns."""',
        "x = 'mutating the list in place'",
    ],
)
def test_the_scan_does_not_fire_on_mere_mentions(source):
    """Comments and docstrings must not produce a false positive."""
    assert mutate_hits(source) == [], source


def test_a_bare_attribute_access_is_not_a_mutation():
    """`service.mutate` without a call cannot change anything.

    This is not a loophole -- it is what the read-only guard in
    scripts/test_connection.py does to prove the attribute is REFUSED. Flagging
    it would make the proof of the guarantee read as a breach of it. Anything
    that actually invokes it is still caught, in either form.
    """
    assert mutate_hits("with pytest.raises(X): service.mutate") == []
    assert mutate_hits("service.mutate(request=r)")
    assert mutate_hits('getattr(service, "mutate_campaigns")')


#: The library's yaml/env config loaders. Using either would put credentials
#: somewhere other than .env, which is the one place this project keeps them.
YAML_LOADER = re.compile(r"\bload_from_(storage|env)\s*\(")


def test_credentials_are_never_loaded_from_a_yaml_file():
    """All config lives in .env, so there is exactly one secret at rest.

    Checks for the loader CALLS rather than the string "google-ads.yaml", so
    prose explaining the decision does not trip it.
    """
    offenders = []
    for path in _source_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if YAML_LOADER.search(line) and not _ALLOWED_CONTEXT.match(line):
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}"
                )
    assert offenders == []


def test_the_yaml_scan_would_catch_a_real_loader_call():
    assert YAML_LOADER.search("GoogleAdsClient.load_from_storage('google-ads.yaml')")
    assert YAML_LOADER.search("client = GoogleAdsClient.load_from_env()")
    assert not YAML_LOADER.search("GoogleAdsClient.load_from_dict(payload)")


# --------------------------------------------------------------------------
# The service allowlist
# --------------------------------------------------------------------------


@pytest.fixture
def settings():
    return Settings(
        client_id="client-id-placeholder",
        client_secret="client-secret-placeholder",
        refresh_token="refresh-token-placeholder",
        login_customer_id="1234567890",
        customer_id="0987654321",
        api_version="v25",
        output_dir=PROJECT_ROOT / "output",
    )


class _FakeRawClient:
    """Stands in for GoogleAdsClient; records what was asked for."""

    def __init__(self):
        self.requested = []

    def get_service(self, name, **kwargs):
        self.requested.append(name)
        return _FakeService()


class _FakeService:
    def search(self, **kwargs):
        return iter(())

    def search_stream(self, **kwargs):
        return iter(())

    def list_accessible_customers(self, **kwargs):
        return _FakeAccessibleCustomers()

    def mutate(self, **kwargs):  # the hole the method allowlist closes
        raise AssertionError("mutate must never be reached")


class _FakeAccessibleCustomers:
    resource_names = ["customers/1234567890", "customers/0987654321"]


@pytest.fixture
def client(settings):
    return ReadOnlyGoogleAdsClient(_FakeRawClient(), settings)


@pytest.mark.parametrize("name", sorted(ALLOWED_SERVICES))
def test_allowed_services_can_be_constructed(client, name):
    assert isinstance(client._service(name), _ReadOnlyService)


@pytest.mark.parametrize(
    "name",
    [
        "CampaignService",
        "AdGroupService",
        "AdGroupAdService",
        "CampaignBudgetService",
        "CustomerClientLinkService",
        "ConversionUploadService",
        "GoogleAdsFieldService",  # read-only in itself, but still not allowlisted
    ],
)
def test_every_other_service_is_refused(client, name):
    with pytest.raises(ReadOnlyViolation, match=name):
        client._service(name)


def test_the_refusal_names_what_is_allowed(client):
    with pytest.raises(ReadOnlyViolation, match="CustomerService, GoogleAdsService"):
        client._service("CampaignService")


# --------------------------------------------------------------------------
# The method allowlist -- the layer that closes GoogleAdsService.mutate
# --------------------------------------------------------------------------


def test_google_ads_service_really_does_expose_mutate():
    """If this ever fails, the method allowlist may look unnecessary. It is not.

    Asserted against the real generated client, not the fake.
    """
    from google.ads.googleads.v25.services.services.google_ads_service import (
        GoogleAdsServiceClient,
    )

    assert hasattr(GoogleAdsServiceClient, "mutate")


def test_mutate_is_unreachable_through_an_allowlisted_service(client):
    service = client._service("GoogleAdsService")
    with pytest.raises(ReadOnlyViolation, match="GoogleAdsService.mutate"):
        service.mutate


@pytest.mark.parametrize(
    "attribute",
    ["mutate", "mutate_campaigns", "create", "update", "remove", "_client", "transport"],
)
def test_non_allowlisted_attributes_are_refused(client, attribute):
    service = client._service("GoogleAdsService")
    with pytest.raises(ReadOnlyViolation):
        getattr(service, attribute)


@pytest.mark.parametrize("method", sorted(ALLOWED_METHODS))
def test_allowlisted_methods_pass_through(client, method):
    service = client._service("CustomerService")
    assert callable(getattr(service, method))


# --------------------------------------------------------------------------
# The guard against the REAL generated services
# --------------------------------------------------------------------------


@pytest.fixture
def real_client(settings):
    """A ReadOnlyGoogleAdsClient over a REAL GoogleAdsClient, offline.

    Constructing GoogleAdsClient directly skips the eager OAuth refresh that
    load_from_dict performs, so this needs no credentials and no network --
    while still handing back the real generated service classes. The fake above
    can only confirm the guard's own logic; this confirms it against the
    objects it will actually wrap.
    """
    from google.ads.googleads.client import GoogleAdsClient
    from google.oauth2.credentials import Credentials

    raw = GoogleAdsClient(
        credentials=Credentials(token="offline-placeholder"),
        login_customer_id=settings.login_customer_id,
        version=settings.api_version,
        use_proto_plus=True,
    )
    return ReadOnlyGoogleAdsClient(raw, settings)


@pytest.mark.parametrize(
    "name",
    [
        "CampaignService",
        "AdGroupAdService",
        "CampaignBudgetService",
        "ConversionUploadService",
        "CustomerClientLinkService",
    ],
)
def test_real_write_services_are_refused(real_client, name):
    with pytest.raises(ReadOnlyViolation, match=name):
        real_client._service(name)


@pytest.mark.parametrize(
    "attribute", ["mutate", "_client", "transport", "common_billing_setup_path"]
)
def test_real_non_read_attributes_are_refused(real_client, attribute):
    service = real_client._service("GoogleAdsService")
    with pytest.raises(ReadOnlyViolation):
        getattr(service, attribute)


def test_real_read_methods_remain_reachable(real_client):
    google_ads = real_client._service("GoogleAdsService")
    assert callable(google_ads.search)
    assert callable(google_ads.search_stream)
    customer = real_client._service("CustomerService")
    assert callable(customer.list_accessible_customers)


def test_the_wrapped_object_really_is_the_generated_service(real_client):
    """Otherwise the test above could be passing over a stub."""
    service = real_client._service("GoogleAdsService")
    assert type(service._service).__name__ == "GoogleAdsServiceClient"
    assert hasattr(service._service, "mutate")


# --------------------------------------------------------------------------
# The write package must not reach back into the read-only one
# --------------------------------------------------------------------------


def test_the_read_only_client_gained_nothing_from_the_write_package():
    """Importing the write package must not alter the read-only surface."""
    import googleads_reporting.write  # noqa: F401

    assert ALLOWED_SERVICES == {"GoogleAdsService", "CustomerService"}
    assert ALLOWED_METHODS == {"search", "search_stream", "list_accessible_customers"}


def test_the_read_only_client_cannot_reach_a_mutating_client(client):
    """No attribute on the read-only client hands back a writer."""
    from googleads_reporting.write import MutatingGoogleAdsClient

    for name in dir(client):
        if name.startswith("__"):
            continue
        try:
            value = getattr(client, name)
        except (ReadOnlyViolation, AttributeError):
            continue
        assert not isinstance(value, MutatingGoogleAdsClient), name


def test_the_read_only_module_does_not_import_the_write_package():
    """A read-only module importing the writer is a smell worth failing on."""
    source = (PROJECT_ROOT / "googleads_reporting" / "client.py").read_text()
    assert "from .write" not in source
    assert "import write" not in source


def test_the_write_package_reuses_the_single_client_factory():
    """The write package must not grow a second place to build a client."""
    source = (
        PROJECT_ROOT / "googleads_reporting" / "write" / "client.py"
    ).read_text()
    assert "build_raw_client" in source
    assert "load_from_dict" not in source


def test_allowlists_have_not_drifted():
    """Pins the contract. Widening either set must be a deliberate edit here."""
    assert ALLOWED_SERVICES == {"GoogleAdsService", "CustomerService"}
    assert ALLOWED_METHODS == {"search", "search_stream", "list_accessible_customers"}


# --------------------------------------------------------------------------
# The public surface
# --------------------------------------------------------------------------


def test_public_methods_are_reads_only():
    public = {
        name
        for name in vars(ReadOnlyGoogleAdsClient)
        if not name.startswith("_")
    }
    assert public == {
        "from_env",
        "settings",
        "api_version",
        "resolve_customer_id",
        "list_accessible_customers",
        "search",
        "search_stream",
        "rows",
    }


def test_list_accessible_customers_normalizes_resource_names(client):
    assert client.list_accessible_customers() == ["1234567890", "0987654321"]


def test_a_non_select_query_is_refused_before_any_request(client):
    from googleads_reporting.query import QueryError

    with pytest.raises(QueryError):
        list(client.search_stream("UPDATE campaign SET status = 'PAUSED'"))
    # Nothing was even constructed.
    assert client._raw.requested == []


def test_build_raw_client_is_the_only_construction_path():
    """The write package must reuse this rather than re-reading config.

    Counts the CALL, not the bare name, so prose mentioning load_from_dict
    does not trip it.
    """
    source = (PROJECT_ROOT / "googleads_reporting" / "client.py").read_text()
    calls = re.findall(r"GoogleAdsClient\.load_from_dict\s*\(", source)
    assert len(calls) == 1, calls
    assert "def build_raw_client" in source
