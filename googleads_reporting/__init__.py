"""Read-only Google Ads API reporting.

Phase 1 is read-only by construction: the only API surface this package exposes
is ``GoogleAdsService.search`` / ``search_stream`` and
``CustomerService.list_accessible_customers``. See
:mod:`googleads_reporting.client` for how that is enforced, and for the seam
Phase 2 should add mutate support behind.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
