"""Mutations: everything that can change an account.

Everything in this subpackage can change a live advertising account. It is the
**only** place in the repository allowed to call a ``mutate*`` method --
``tests/test_readonly_guard.py`` scans every other file for them, so the
read-only guarantee survives intact: ``ReadOnlyGoogleAdsClient`` still cannot
write, and nothing outside this directory can either.

Three habits make that guarantee worth something:

* **Validate before you ask.** Every mutation is first sent with
  ``validate_only=True``. Google checks it server-side and reports what would
  fail, so a preview is something the API confirmed rather than something this
  code guessed.
* **Applying is explicit.** :meth:`MutatingGoogleAdsClient.mutate` does not
  write unless ``apply=True``. The CLI turns that into a typed confirmation;
  an agent driving this library supplies its own gate. The library never
  assumes consent.
* **Everything is logged.** Applied mutations append to an audit file before
  and after the call, so a change is reconstructable even if the process dies
  mid-request.
"""

from .audit import AuditLog
from .client import (
    ALLOWED_MUTATE_SERVICES,
    MutatingGoogleAdsClient,
    MutationError,
    MutationResult,
)

__all__ = [
    "ALLOWED_MUTATE_SERVICES",
    "AuditLog",
    "MutatingGoogleAdsClient",
    "MutationError",
    "MutationResult",
]
