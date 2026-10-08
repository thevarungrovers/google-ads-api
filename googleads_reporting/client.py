"""The read-only Google Ads client.

This client must not mutate anything. The OAuth scope
``https://www.googleapis.com/auth/adwords`` has no read-only variant -- the
token these credentials carry is perfectly capable of changing the account --
so the restriction has to live here, in code.

Three layers enforce it:

1. :data:`ALLOWED_SERVICES` -- only ``GoogleAdsService`` and ``CustomerService``
   can be constructed at all.
2. :data:`ALLOWED_METHODS` -- every service is handed back wrapped in
   :class:`_ReadOnlyService`, which refuses any attribute not on the allowlist.
   This layer is not redundant: ``GoogleAdsService`` itself exposes a
   ``mutate`` method, so allowlisting the *service* alone would leave the write
   path one attribute access away.
3. ``tests/test_readonly_guard.py`` greps the whole source tree for a
   ``.mutate*(`` call, so a write has to be added deliberately, inside that
   package, rather than by someone reaching past this module.

**Mutations live in** :mod:`googleads_reporting.write`, a sibling package with
its own allowlist and its own confirmation step, which reuses
:func:`build_raw_client` rather than constructing a second client. Keep it that
way: loosening the sets below to add a write here would end the guarantee this
module exists to make, and the guard test covers everything outside that one
package.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from google.ads.googleads.client import GoogleAdsClient

from .config import Settings
from .customer_id import normalize_customer_id
from .query import Query, assert_select_only, parse_select
from .fields import row_to_dict

#: The only services this client may construct.
ALLOWED_SERVICES = frozenset({"GoogleAdsService", "CustomerService"})

#: The only methods it may call on them. Note that GoogleAdsService also
#: offers `mutate`, which is exactly what this set exists to keep out.
ALLOWED_METHODS = frozenset({"search", "search_stream", "list_accessible_customers"})


class ReadOnlyViolation(RuntimeError):
    """Raised when something tries to reach a write-capable API surface."""


class GoogleAdsReportingError(RuntimeError):
    """A request failed. Carries the API's own failure details where present."""


class _ReadOnlyService:
    """Wraps a generated service client, exposing only allowlisted methods."""

    __slots__ = ("_service", "_name")

    def __init__(self, service: Any, name: str) -> None:
        self._service = service
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        if attribute not in ALLOWED_METHODS:
            raise ReadOnlyViolation(
                f"{self._name}.{attribute} is not permitted on the read-only "
                f"client. Allowed: {', '.join(sorted(ALLOWED_METHODS))}."
            )
        return getattr(self._service, attribute)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<_ReadOnlyService {self._name}>"


def build_raw_client(settings: Settings) -> GoogleAdsClient:
    """Construct the underlying library client from ``settings``.

    The single place credentials become a client. The write package calls this
    too rather than re-reading config, so there stays exactly one construction
    path to audit.

    **This performs a network call.** ``load_from_dict`` refreshes the OAuth
    token eagerly, so a bad client ID, client secret or refresh token raises
    ``google.auth.exceptions.RefreshError`` here, at construction, rather than
    on the first query. ``scripts/test_connection.py`` relies on that: the rung
    that constructs the client is already the rung that validates the OAuth
    credentials.
    """
    return GoogleAdsClient.load_from_dict(
        settings.to_google_ads_dict(), version=settings.api_version
    )


class ReadOnlyGoogleAdsClient:
    """A Google Ads client that can read and cannot write.

    Only three operations are reachable: :meth:`search`, :meth:`search_stream`
    and :meth:`list_accessible_customers`.
    """

    def __init__(self, raw_client: GoogleAdsClient, settings: Settings) -> None:
        self._raw = raw_client
        self._settings = settings

    @classmethod
    def from_env(cls, *, settings: Settings | None = None) -> "ReadOnlyGoogleAdsClient":
        resolved = settings or Settings.from_env()
        return cls(build_raw_client(resolved), resolved)

    # -- configuration ----------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def api_version(self) -> str:
        return self._settings.api_version

    def resolve_customer_id(self, override: str | None = None) -> str:
        return self._settings.resolve_customer_id(override)

    # -- the guarded service accessor -------------------------------------

    def _service(self, name: str) -> _ReadOnlyService:
        """Return an allowlisted service, wrapped so only read methods exist."""
        if name not in ALLOWED_SERVICES:
            raise ReadOnlyViolation(
                f"{name} is not permitted on the read-only client. "
                f"Allowed services: {', '.join(sorted(ALLOWED_SERVICES))}."
            )
        return _ReadOnlyService(self._raw.get_service(name), name)

    # -- reads -------------------------------------------------------------

    def list_accessible_customers(self) -> list[str]:
        """Customer IDs the configured credentials can reach, as bare digits.

        These are the accounts the OAuth user can see directly -- typically the
        MCC itself rather than every account beneath it. Use the ``accounts``
        report to enumerate the accounts under the MCC.
        """
        service = self._service("CustomerService")
        response = service.list_accessible_customers()
        return [
            normalize_customer_id(name) for name in response.resource_names
        ]

    def search_stream(
        self, query: Query | str, *, customer_id: str | None = None
    ) -> Iterator[Any]:
        """Stream rows for ``query``.

        Preferred over :meth:`search` for reporting: the API streams batches
        rather than paging, so a large report needs one request rather than one
        per page.
        """
        gaql = self._to_gaql(query)
        target = self.resolve_customer_id(customer_id)
        service = self._service("GoogleAdsService")
        try:
            for batch in service.search_stream(customer_id=target, query=gaql):
                yield from batch.results
        except Exception as exc:  # noqa: BLE001 - re-raised with context below
            raise self._describe_failure(exc, target, gaql) from exc

    def search(
        self, query: Query | str, *, customer_id: str | None = None
    ) -> Iterator[Any]:
        """Page through rows for ``query``.

        The returned pager handles paging transparently; prefer
        :meth:`search_stream` unless you need a page-at-a-time cursor.
        """
        gaql = self._to_gaql(query)
        target = self.resolve_customer_id(customer_id)
        service = self._service("GoogleAdsService")
        try:
            yield from service.search(customer_id=target, query=gaql)
        except Exception as exc:  # noqa: BLE001 - re-raised with context below
            raise self._describe_failure(exc, target, gaql) from exc

    def rows(
        self, query: Query | str, *, customer_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Run ``query`` and return flattened dicts, columns in SELECT order.

        Money is already out of micros; enums are already names.
        """
        gaql = self._to_gaql(query)
        paths = parse_select(gaql)
        return [
            row_to_dict(row, paths)
            for row in self.search_stream(gaql, customer_id=customer_id)
        ]

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _to_gaql(query: Query | str) -> str:
        gaql = query.to_gaql() if isinstance(query, Query) else str(query)
        return assert_select_only(gaql)

    @staticmethod
    def _describe_failure(
        exc: Exception, customer_id: str, gaql: str
    ) -> GoogleAdsReportingError:
        """Turn a GoogleAdsException into something a human can act on.

        The library's own repr buries the useful part -- the per-error
        ``error_code`` and message -- under a protobuf dump.
        """
        details: list[str] = []
        failure = getattr(exc, "failure", None)
        if failure is not None:
            for error in getattr(failure, "errors", []):
                code = getattr(error, "error_code", None)
                # error_code is a oneof; whichever field is set names the
                # category, and that name is the actionable part.
                code_name = ""
                if code is not None:
                    for descriptor, value in type(code).pb(code).ListFields():
                        code_name = f"{descriptor.name}={getattr(value, 'name', value)}"
                        break
                details.append(
                    f"  - {code_name or 'error'}: {getattr(error, 'message', '')}"
                )

        request_id = getattr(exc, "request_id", None)
        parts = [f"Google Ads request failed for customer {customer_id}."]
        if details:
            parts.append("Errors:")
            parts.extend(details)
        else:
            parts.append(f"  {type(exc).__name__}: {exc}")
        if request_id:
            parts.append(f"request_id: {request_id}")
        parts.append("Query:")
        parts.extend(f"  {line}" for line in gaql.splitlines())
        return GoogleAdsReportingError("\n".join(parts))
