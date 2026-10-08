"""Read-only enforcement tests.

The OAuth scope this project uses (`.../auth/adwords`) has no read-only
variant: the token is fully capable of changing the account. Nothing outside
this repo stops a write, so these tests are the enforcement.
"""

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

#: `.mutate(`, `.mutate_campaigns(`, `.mutateAll(` -- any mutate call at all.
MUTATE_CALL = re.compile(r"\.\s*mutate\w*\s*\(")

#: Lines that legitimately contain the word while documenting the ban.
_ALLOWED_CONTEXT = re.compile(r"^\s*(#|\*|\"|'|$)")


def _source_files() -> list[Path]:
    files = []
    for directory in ("googleads_reporting", "scripts"):
        files.extend(sorted((PROJECT_ROOT / directory).rglob("*.py")))
    return files


def test_source_files_were_actually_found():
    """A scan over an empty list passes vacuously; make sure it is not."""
    files = _source_files()
    assert len(files) >= 5, [str(f) for f in files]
    assert any(f.name == "client.py" for f in files)


def test_no_mutate_call_anywhere_in_the_source_tree():
    offenders = []
    for path in _source_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if MUTATE_CALL.search(line) and not _ALLOWED_CONTEXT.match(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
    assert offenders == [], (
        "Phase 1 is read-only. Found mutate call(s):\n" + "\n".join(offenders)
    )


def test_the_scan_would_catch_a_real_mutate_call():
    """Negative control: a regex that matches nothing passes every file."""
    assert MUTATE_CALL.search("service.mutate(request=req)")
    assert MUTATE_CALL.search("client.get_service('CampaignService').mutate_campaigns(x)")
    assert MUTATE_CALL.search("svc .mutate ( req )")
    assert not MUTATE_CALL.search("# never call .mutate on a service")
    assert not MUTATE_CALL.search("search_stream(customer_id=cid, query=q)")


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
        developer_token="dev-token-placeholder",
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
    """Phase 2 must reuse this rather than re-reading config elsewhere."""
    source = (PROJECT_ROOT / "googleads_reporting" / "client.py").read_text()
    assert source.count("load_from_dict") == 1
    assert "def build_raw_client" in source
