"""Customer ID normalization.

Google Ads shows customer IDs as ``123-456-7890`` in the UI but the API accepts
only the bare 10 digits. Every ID entering this package -- from ``.env``, from a
CLI flag, from an API resource name -- goes through
:func:`normalize_customer_id` first, so the dashed form never reaches a request.
"""

from __future__ import annotations

import re

CUSTOMER_ID_LENGTH = 10

#: Characters people legitimately paste around an ID: dashes from the UI,
#: spaces, underscores, and non-breaking spaces from rich-text copy/paste.
_SEPARATORS = re.compile(r"[-\s_ ​]+")

_RESOURCE_NAME = re.compile(r"^customers/(\d+)(?:/.*)?$")


class CustomerIdError(ValueError):
    """Raised when a value cannot be read as a Google Ads customer ID."""


def normalize_customer_id(value: object) -> str:
    """Return ``value`` as a bare 10-digit customer ID.

    Accepts the dashed UI form, the bare form, an ``int``, and a
    ``customers/1234567890`` resource name.

    >>> normalize_customer_id("123-456-7890")
    '1234567890'
    >>> normalize_customer_id(1234567890)
    '1234567890'
    >>> normalize_customer_id("customers/1234567890")
    '1234567890'
    """
    if value is None or isinstance(value, bool):
        raise CustomerIdError(f"Not a customer ID: {value!r}")

    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise CustomerIdError(f"Not a customer ID: {value!r}")

    if not text:
        raise CustomerIdError("Customer ID is empty.")

    match = _RESOURCE_NAME.match(text)
    if match:
        text = match.group(1)

    digits = _SEPARATORS.sub("", text)

    if not digits.isdigit():
        raise CustomerIdError(
            f"Customer ID must be digits (dashes allowed), got {text!r}."
        )
    if len(digits) != CUSTOMER_ID_LENGTH:
        raise CustomerIdError(
            f"Customer ID must be {CUSTOMER_ID_LENGTH} digits, "
            f"got {len(digits)} in {text!r}."
        )
    return digits


def format_customer_id(value: object) -> str:
    """Return the dashed form used in the Google Ads UI, for display only.

    >>> format_customer_id("1234567890")
    '123-456-7890'
    """
    digits = normalize_customer_id(value)
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
