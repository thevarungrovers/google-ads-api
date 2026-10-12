"""The mutating client.

Deliberately a sibling of :class:`~googleads_reporting.client.ReadOnlyGoogleAdsClient`
rather than an extension of it. The read-only class keeps its guarantee because
nothing was added to it -- you reach writes by importing a different class from
a different subpackage, which is a decision someone has to make on purpose.

Both share :func:`~googleads_reporting.client.build_raw_client`, so there is
still exactly one place where credentials become a client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .. import logdb
from ..client import build_raw_client
from ..config import Settings

#: Services this subpackage may construct. Narrower than "whatever the library
#: offers": each entry is one someone decided to support, not one that happened
#: to exist. Adding to this list is the deliberate act that widens the blast
#: radius.
ALLOWED_MUTATE_SERVICES = frozenset(
    {
        "CampaignBudgetService",
        "CampaignService",
        "AdGroupService",
        "AdGroupAdService",
        "AdGroupCriterionService",
        "AssetService",
        # Reads are allowed here too: a mutation needs to fetch current state to
        # show a before/after diff. GoogleAdsService also carries the ATOMIC
        # multi-entity mutate used to build a whole campaign in one request --
        # see MutatingGoogleAdsClient.mutate_atomic.
        "GoogleAdsService",
    }
)

#: Daily budget above which a mutation is refused unless explicitly overridden.
#: Not a security control -- it is a typo catch. The failure it exists for is
#: an amount entered in micros by mistake, which turns $50 into $50,000,000.
DEFAULT_MAX_DAILY_BUDGET = 1_000.0


class MutationError(RuntimeError):
    """A mutation failed, or was refused before being sent."""


class GuardrailViolation(MutationError):
    """A mutation was refused by a local guardrail before reaching the API."""


@dataclass
class MutationResult:
    """What happened to a batch of operations."""

    validated_only: bool
    resource_names: list[str] = field(default_factory=list)
    request_id: str | None = None
    partial_failure_error: str | None = None

    @property
    def applied(self) -> bool:
        return not self.validated_only

    def __str__(self) -> str:
        verb = "validated" if self.validated_only else "APPLIED"
        return f"{verb}: {len(self.resource_names)} resource(s)"


class MutatingGoogleAdsClient:
    """A client that can change an account -- but not by accident.

    :meth:`mutate` never writes unless ``apply=True`` is passed explicitly.
    With ``apply=False`` (the default) it still contacts the API, sending
    ``validate_only=True`` so Google reports whether the operation *would*
    succeed. That is the difference between a preview and a guess.
    """

    def __init__(
        self,
        raw_client: Any,
        settings: Settings,
        *,
        audit_log: Any | None = None,
        max_daily_budget: float = DEFAULT_MAX_DAILY_BUDGET,
    ) -> None:
        self._raw = raw_client
        self._settings = settings
        self.max_daily_budget = max_daily_budget
        if audit_log is None:
            from .audit import AuditLog

            # No path argument: the log is a table in logs/google-ads.db,
            # resolved from the package. It used to be derived from
            # settings.output_dir, which meant GOOGLE_ADS_OUTPUT_DIR could move
            # this log while the MCP ledger stayed in the repo.
            audit_log = AuditLog()
        self._audit = audit_log

    @classmethod
    def from_env(
        cls, *, settings: Settings | None = None, **kwargs: Any
    ) -> "MutatingGoogleAdsClient":
        resolved = settings or Settings.from_env()
        return cls(build_raw_client(resolved), resolved, **kwargs)

    # -- plumbing ----------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def raw(self) -> Any:
        """The underlying library client, for building operations and enums."""
        return self._raw

    def resolve_customer_id(self, override: str | None = None) -> str:
        return self._settings.resolve_customer_id(override)

    def service(self, name: str) -> Any:
        if name not in ALLOWED_MUTATE_SERVICES:
            raise MutationError(
                f"{name} is not an allowlisted service. Allowed: "
                f"{', '.join(sorted(ALLOWED_MUTATE_SERVICES))}."
            )
        return self._raw.get_service(name)

    def get_type(self, name: str) -> Any:
        return self._raw.get_type(name)

    @property
    def enums(self) -> Any:
        return self._raw.enums

    # -- reads, for building diffs ----------------------------------------

    def query(self, gaql: str, *, customer_id: str | None = None) -> list[Any]:
        """Run a GAQL read, used to capture state before a mutation."""
        from ..query import assert_select_only

        service = self.service("GoogleAdsService")
        target = self.resolve_customer_id(customer_id)
        rows = []
        with logdb.record_api_call(
            "search_stream", service="GoogleAdsService", customer_id=target, request=gaql
        ) as slot:
            for batch in service.search_stream(
                customer_id=target, query=assert_select_only(gaql)
            ):
                rows.extend(batch.results)
            slot["row_count"] = len(rows)
        return rows

    # -- the one write path ------------------------------------------------

    def mutate(
        self,
        *,
        service_name: str,
        method: str,
        operations: Sequence[Any],
        customer_id: str | None = None,
        apply: bool = False,
        partial_failure: bool = False,
        describe: Sequence[dict[str, Any]] | None = None,
    ) -> MutationResult:
        """Send ``operations``, validating unless ``apply`` is True.

        ``apply=False`` still calls the API -- with ``validate_only=True``, so
        Google performs the full server-side check and changes nothing. This is
        the only method in the package that can write, and it is the only place
        ``validate_only`` is decided.

        ``describe`` is a plain-dict rendering of the operations for the audit
        log; protos do not serialise usefully.
        """
        if not operations:
            raise MutationError("No operations to send.")

        target = self.resolve_customer_id(customer_id)
        service = self.service(service_name)
        validate_only = not apply

        correlation_id = self._audit.attempt(
            customer_id=target,
            service=service_name,
            method=method,
            operations=list(describe or []),
            validate_only=validate_only,
        )

        request = self._raw.get_type(_request_type_for(method))
        request.customer_id = target
        request.operations.extend(operations)
        request.partial_failure = partial_failure
        request.validate_only = validate_only

        try:
            with logdb.record_api_call(
                method,
                service=service_name,
                customer_id=target,
                request={"validate_only": validate_only, "operations": list(describe or [])},
            ) as slot:
                response = getattr(service, method)(request=request)
                slot["row_count"] = len(operations)
        except Exception as exc:  # noqa: BLE001 - re-raised with context
            failure = _describe_failure(exc, target, method)
            self._audit.outcome(correlation_id, ok=False, error=str(failure))
            raise failure from exc

        # validate_only responses carry no resource names -- nothing was made.
        resource_names = [
            r.resource_name for r in getattr(response, "results", []) if r.resource_name
        ]
        partial_error = None
        if getattr(response, "partial_failure_error", None) and (
            response.partial_failure_error.message
        ):
            partial_error = response.partial_failure_error.message

        self._audit.outcome(
            correlation_id,
            ok=True,
            resource_names=resource_names,
            request_id=getattr(response, "request_id", None) or None,
            error=partial_error,
        )

        return MutationResult(
            validated_only=validate_only,
            resource_names=resource_names,
            request_id=getattr(response, "request_id", None) or None,
            partial_failure_error=partial_error,
        )

    def mutate_atomic(
        self,
        operations: Sequence[Any],
        *,
        customer_id: str | None = None,
        apply: bool = False,
        describe: Sequence[dict[str, Any]] | None = None,
    ) -> MutationResult:
        """Send many operations as ONE all-or-nothing request.

        ``GoogleAdsService.mutate`` takes a list of ``MutateOperation`` and
        applies them in a single transaction. Two things make it the right way
        to build a campaign:

        * **Temp resource names.** An operation can reference an entity created
          earlier in the same request by a negative id --
          ``customers/X/campaignBudgets/-1``. That is what lets a budget, a
          campaign, an ad group and an ad be created together, before any of
          them have real ids.
        * **Nothing, or everything.** Sent one call at a time, a failure at the
          ad step leaves a campaign and an ad group already created and billing
          configured -- a half-built campaign someone has to find and clean up.
          Here a failure anywhere means nothing was created at all.

        ``partial_failure`` is deliberately NOT offered: partial success is the
        exact outcome this method exists to prevent.
        """
        if not operations:
            raise MutationError("No operations to send.")

        target = self.resolve_customer_id(customer_id)
        service = self.service("GoogleAdsService")
        validate_only = not apply

        correlation_id = self._audit.attempt(
            customer_id=target,
            service="GoogleAdsService",
            method="mutate (atomic)",
            operations=list(describe or []),
            validate_only=validate_only,
        )

        request = self._raw.get_type("MutateGoogleAdsRequest")
        request.customer_id = target
        request.mutate_operations.extend(operations)
        request.validate_only = validate_only
        request.partial_failure = False

        try:
            response = service.mutate(request=request)
        except Exception as exc:  # noqa: BLE001 - re-raised with context
            failure = _describe_failure(exc, target, "mutate (atomic)")
            self._audit.outcome(correlation_id, ok=False, error=str(failure))
            raise failure from exc

        resource_names = []
        for item in getattr(response, "mutate_operation_responses", []):
            for field in type(item).pb(item).ListFields():
                result = getattr(item, field[0].name, None)
                name = getattr(result, "resource_name", "")
                if name:
                    resource_names.append(name)

        self._audit.outcome(
            correlation_id,
            ok=True,
            resource_names=resource_names,
            request_id=getattr(response, "request_id", None) or None,
        )
        return MutationResult(
            validated_only=validate_only,
            resource_names=resource_names,
            request_id=getattr(response, "request_id", None) or None,
        )

    # -- guardrails --------------------------------------------------------

    def check_daily_budget(self, amount: float, *, override: bool = False) -> None:
        """Refuse an implausibly large daily budget.

        Catches the amount-entered-in-micros mistake, which silently turns
        $50.00 into $50,000,000.00 and is indistinguishable from a real value
        once it reaches the API.
        """
        if override:
            return
        if amount > self.max_daily_budget:
            raise GuardrailViolation(
                f"Refusing a daily budget of {amount:,.2f} "
                f"(guardrail: {self.max_daily_budget:,.2f}).\n"
                "  Amounts here are in the account's currency, NOT micros -- "
                "this tool does the micros conversion for you.\n"
                f"  If you meant {amount / 1_000_000:,.2f}, pass that instead.\n"
                f"  If you really do want {amount:,.2f} per day, re-run with "
                "--override-budget-guardrail."
            )


_REQUEST_TYPES = {
    "mutate_assets": "MutateAssetsRequest",
    "mutate_campaign_budgets": "MutateCampaignBudgetsRequest",
    "mutate_campaigns": "MutateCampaignsRequest",
    "mutate_ad_groups": "MutateAdGroupsRequest",
    "mutate_ad_group_ads": "MutateAdGroupAdsRequest",
    "mutate_ad_group_criteria": "MutateAdGroupCriteriaRequest",
}


def _request_type_for(method: str) -> str:
    try:
        return _REQUEST_TYPES[method]
    except KeyError:
        raise MutationError(
            f"Unsupported mutate method {method!r}. Supported: "
            f"{', '.join(sorted(_REQUEST_TYPES))}."
        ) from None


def _describe_failure(exc: Exception, customer_id: str, method: str) -> MutationError:
    """Lift the actionable part out of a GoogleAdsException."""
    details: list[str] = []
    failure = getattr(exc, "failure", None)
    if failure is not None:
        for error in getattr(failure, "errors", []):
            code = getattr(error, "error_code", None)
            code_name = ""
            if code is not None:
                for descriptor, value in type(code).pb(code).ListFields():
                    code_name = f"{descriptor.name}={getattr(value, 'name', value)}"
                    break
            location = ""
            trigger = getattr(error, "location", None)
            if trigger is not None and getattr(trigger, "field_path_elements", None):
                location = " at " + ".".join(
                    e.field_name for e in trigger.field_path_elements
                )
            details.append(
                f"  - {code_name or 'error'}{location}: {getattr(error, 'message', '')}"
            )

    parts = [f"{method} failed for customer {customer_id}."]
    if details:
        parts.append("Errors:")
        parts.extend(details)
    else:
        parts.append(f"  {type(exc).__name__}: {exc}")
    request_id = getattr(exc, "request_id", None)
    if request_id:
        parts.append(f"request_id: {request_id}")
    return MutationError("\n".join(parts))
