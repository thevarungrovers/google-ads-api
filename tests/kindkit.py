"""A test rig for change kinds: real v25 protos, no network, nothing sent.

The write-path tests stub every edge with SimpleNamespaces, which proves the
SEQUENCE but cannot catch a misspelt proto field -- and a misspelt field is the
commonest way a new kind breaks. So this rig builds operations with a REAL
offline GoogleAdsClient (constructed directly, skipping the OAuth refresh that
load_from_dict performs) and hands rows back as real GoogleAdsRow messages.
Setting a field that does not exist in v25 raises here exactly as it would in
production.

Not a test module (no ``test_`` prefix). Imported by the kind tests.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials

from googleads_mcp.changes.context import WriteContext
from googleads_mcp.changes.engine import Engine
from googleads_mcp.guardrails import Limits, SessionCounters
from googleads_mcp.previews import PreviewStore
from googleads_reporting.config import Settings
from googleads_reporting.write.client import MutatingGoogleAdsClient, MutationResult
from googleads_reporting.write.plan import execute

CUSTOMER = "1112223333"

RAW = GoogleAdsClient(
    credentials=Credentials(token="offline-placeholder"),
    developer_token="offline-placeholder",
    version="v25",
    use_proto_plus=True,
)

SETTINGS = Settings(
    client_id="x", client_secret="x", refresh_token="x",
    login_customer_id=None, customer_id=CUSTOMER, api_version="v25",
    output_dir=Path("/tmp"),
)


def row(**paths: Any):
    """A GoogleAdsRow with dotted ``paths`` set: row(**{"campaign.id": 1}).

    Enum fields take the value NAME as a string.
    """
    message = RAW.get_type("GoogleAdsRow")
    for path, value in paths.items():
        *parents, leaf = path.split(".")
        target = message
        for part in parents:
            target = getattr(target, part)
        if isinstance(value, list):
            getattr(target, leaf).extend(value)
            continue
        current = getattr(target, leaf)
        if hasattr(current, "name") and isinstance(value, str) and hasattr(type(current), value):
            value = getattr(type(current), value)
        setattr(target, leaf, value)
    return message


class FakeWriteClient(MutatingGoogleAdsClient):
    """The real mutating client with the network cut at both ends.

    ``answers`` maps a substring of the GAQL to the rows to return (first
    match wins), so a test states the account it needs in a few lines.
    ``sent`` records (apply, operations) for every mutate.
    """

    def __init__(self) -> None:
        super().__init__(RAW, SETTINGS, audit_log=SimpleNamespace(
            attempt=lambda **_k: "c", outcome=lambda *_a, **_k: None,
        ))
        self.answers: list[tuple[str | Callable[[str], bool], list[Any]]] = []
        self.queries: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.refuse: str | None = None
        self._next_id = 9000

    def answer(self, needle, rows: list[Any]) -> None:
        self.answers.insert(0, (needle, rows))

    def query(self, gaql: str, *, customer_id: str | None = None) -> list[Any]:
        self.queries.append(gaql)
        for needle, rows in self.answers:
            if (needle(gaql) if callable(needle) else needle in gaql):
                return list(rows)
        return []

    def _result(self, operations, apply: bool) -> MutationResult:
        names = []
        if apply:
            for _ in operations:
                self._next_id += 1
                names.append(f"customers/{CUSTOMER}/thing/{self._next_id}")
        return MutationResult(validated_only=not apply, resource_names=names)

    def mutate(self, *, service_name, method, operations, customer_id=None,
               apply=False, partial_failure=False, describe=None):
        from googleads_reporting.write.client import MutationError, _request_type_for

        _request_type_for(method)  # an unsupported method must fail here too
        self.service(service_name)  # and a service off the allowlist
        if self.refuse:
            raise MutationError(self.refuse)
        self.sent.append({"apply": apply, "service": service_name, "method": method,
                          "operations": list(operations)})
        return self._result(operations, apply)

    def mutate_atomic(self, operations, *, customer_id=None, apply=False, describe=None):
        from googleads_reporting.write.client import MutationError

        if self.refuse:
            raise MutationError(self.refuse)
        self.sent.append({"apply": apply, "service": "GoogleAdsService",
                          "method": "mutate (atomic)", "operations": list(operations)})
        return self._result(operations, apply)

    @property
    def applied(self) -> list[dict[str, Any]]:
        return [s for s in self.sent if s["apply"]]


class FakeRead:
    """`read.rows()` with canned flattened dicts, matched the same way."""

    def __init__(self) -> None:
        self.answers: list[tuple[str, list[dict[str, Any]]]] = [
            ("customer.currency_code", [{"customer.currency_code": "CAD"}]),
            ("metrics.cost_micros", [{
                "metrics.cost_micros": 7.0, "metrics.clicks": 70,
                "metrics.impressions": 1000, "metrics.conversions": 2.0,
            }]),
        ]
        self.queries: list[str] = []

    def answer(self, needle: str, rows: list[dict[str, Any]]) -> None:
        self.answers.insert(0, (needle, rows))

    def rows(self, query, *, customer_id=None):
        gaql = query.to_gaql() if hasattr(query, "to_gaql") else str(query)
        self.queries.append(gaql)
        for needle, rows in self.answers:
            if needle in gaql:
                return list(rows)
        return []


def limits(**over) -> Limits:
    base = dict(
        max_daily_budget=Decimal("100"), max_budget_change_pct=50.0,
        max_cpc_bid=Decimal("5"), max_bid_increase_pct=50.0,
        max_entities_per_call=25, max_projected_delta_per_day=Decimal("50"),
        max_applies_per_session=20, max_projected_session_delta=Decimal("150"),
    )
    base.update(over)
    return Limits(**base)


def rig(**limit_overrides):
    """(engine, client, read) wired the way tools_write.register wires them."""
    lim = limits(**limit_overrides)
    state = SimpleNamespace(
        limits=lim, counters=SessionCounters(lim), previews=PreviewStore(),
        read_client=FakeRead(), default_customer_id=CUSTOMER,
    )
    client = FakeWriteClient()
    state.write_client = client
    engine = Engine(WriteContext(state, client, execute))
    return engine, client, state.read_client


def preview_and_apply(engine, kind, **args):
    preview = engine.preview(kind, args)
    assert not preview.blocked, preview.checks
    return preview, engine.apply(kind, preview.preview_token)


def failed(preview, name: str) -> bool:
    return any(line.startswith("FAIL") and f" {name}:" in line for line in preview.checks)
