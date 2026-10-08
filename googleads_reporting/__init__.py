"""Google Ads API reporting and campaign management.

This package is read-only by construction: the only API surface it exposes is
``GoogleAdsService.search`` / ``search_stream`` and
``CustomerService.list_accessible_customers``. See
:mod:`googleads_reporting.client` for how that is enforced.

Everything that can change an account lives in
:mod:`googleads_reporting.write`, deliberately behind a separate import.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
