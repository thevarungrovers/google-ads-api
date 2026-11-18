"""What kind of failure an exception is, so an agent can react to it.

Every failure used to reach an agent as prose -- or, from any exception that
was not a ToolError, as a bare "Error executing tool X" with nothing else. An
agent cannot back off on a quota error, re-authorise on an expired token or
fix a malformed query if all three read the same. So each failure is put in
one of a few classes, and the MCP layer prefixes its message with the class:

    AUTH_EXPIRED   credentials refused -- a human must re-authorise; retrying
                   will never help
    QUOTA          rate or daily operation limit -- back off, try later
    INVALID_QUERY  the request itself is wrong -- change it, do not retry it
    NOT_FOUND      no such entity -- re-read the ids
    POLICY         Google's ad policy refused it -- the message names the topic
    TRANSIENT      Google had a moment -- safe to retry (reads already were)

The classification walks the exception's ``__cause__`` chain, because this
codebase wraps the library's GoogleAdsException in its own
GoogleAdsReportingError / MutationError (with a readable message) and keeps the
original as the cause. The readable message is kept; the class comes from the
original.

Lives in googleads_reporting rather than googleads_mcp because the read client
needs :func:`is_transient` for its retries, and the library must not import
the server.
"""

from __future__ import annotations

from typing import Any, Iterator

AUTH_EXPIRED = "AUTH_EXPIRED"
QUOTA = "QUOTA"
INVALID_QUERY = "INVALID_QUERY"
NOT_FOUND = "NOT_FOUND"
POLICY = "POLICY"
TRANSIENT = "TRANSIENT"

#: gRPC status names worth retrying. Anything else is a real answer.
TRANSIENT_STATUSES = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"})

_STATUS_CLASS = {
    "UNAVAILABLE": TRANSIENT,
    "DEADLINE_EXCEEDED": TRANSIENT,
    "INTERNAL": TRANSIENT,
    "RESOURCE_EXHAUSTED": QUOTA,
    "UNAUTHENTICATED": AUTH_EXPIRED,
    "PERMISSION_DENIED": AUTH_EXPIRED,
    "NOT_FOUND": NOT_FOUND,
    "INVALID_ARGUMENT": INVALID_QUERY,
}

#: GoogleAdsError.error_code oneof field -> class. Order of precedence is the
#: order of _PRECEDENCE below, not of this dict.
_CODE_CLASS = {
    "authentication_error": AUTH_EXPIRED,
    "authorization_error": AUTH_EXPIRED,
    "quota_error": QUOTA,
    "query_error": INVALID_QUERY,
    "request_error": INVALID_QUERY,
    "policy_finding_error": POLICY,
    "policy_violation_error": POLICY,
    "policy_validation_parameter_error": POLICY,
    "internal_error": TRANSIENT,
}

#: When one failure carries several errors, the most actionable class wins.
_PRECEDENCE = (AUTH_EXPIRED, QUOTA, POLICY, NOT_FOUND, INVALID_QUERY, TRANSIENT)

#: What to do about each class, appended to the message.
ADVICE = {
    AUTH_EXPIRED: "The credentials were refused. A human has to re-authorise "
    "(scripts/generate_refresh_token.py); retrying will not help.",
    QUOTA: "Google's rate or operations limit. Back off and try later; do not loop.",
    INVALID_QUERY: "The request itself is malformed. Change it; do not retry it as is.",
    NOT_FOUND: "No such entity. Re-read the ids with a find_*/list_* tool.",
    POLICY: "Google's ad policy refused this. Change the text or targeting it names.",
    TRANSIENT: "A temporary failure at Google's end. Safe to retry shortly.",
}


def chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and everything it was raised from, outermost first."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _oneof_name(message: Any) -> tuple[str, str]:
    """(field, enum value name) of the populated oneof on an error_code."""
    try:
        for descriptor, value in type(message).pb(message).ListFields():
            enum_type = getattr(descriptor, "enum_type", None)
            if enum_type is not None:
                found = enum_type.values_by_number.get(value)
                return descriptor.name, found.name if found else str(value)
            return descriptor.name, str(getattr(value, "name", value))
    except Exception:  # noqa: BLE001 - a fake or partial message
        pass
    return "", ""


def _status_name(exc: BaseException) -> str:
    """The gRPC status of a GoogleAdsException or a grpc/api_core error."""
    for candidate in (getattr(exc, "error", None), exc):
        code = getattr(candidate, "code", None)
        try:
            value = code() if callable(code) else code
        except Exception:  # noqa: BLE001
            continue
        name = getattr(value, "name", None)
        if isinstance(name, str):
            return name
    grpc_status = getattr(exc, "grpc_status_code", None)  # google.api_core
    return getattr(grpc_status, "name", "") or ""


def policy_topics(exc: BaseException) -> list[str]:
    """Policy topics named anywhere in a failure, for the POLICY message."""
    topics: list[str] = []
    for link in chain(exc):
        failure = getattr(link, "failure", None)
        for error in getattr(failure, "errors", []) or []:
            details = getattr(error, "details", None)
            finding = getattr(details, "policy_finding_details", None)
            for entry in getattr(finding, "policy_topic_entries", []) or []:
                if getattr(entry, "topic", ""):
                    topics.append(entry.topic)
            violation = getattr(details, "policy_violation_details", None)
            name = getattr(violation, "external_policy_name", "")
            if name:
                topics.append(name)
    return sorted(set(topics))


def classify(exc: BaseException) -> str | None:
    """The class of ``exc``, or None if it is not a recognised API failure."""
    found: set[str] = set()
    for link in chain(exc):
        type_name = type(link).__name__
        if type_name == "RefreshError":
            return AUTH_EXPIRED
        if type_name in ("QueryError",):
            found.add(INVALID_QUERY)
        if type_name in ("NotFound",) and type(link).__module__.endswith("lookup"):
            found.add(NOT_FOUND)
        failure = getattr(link, "failure", None)
        for error in getattr(failure, "errors", []) or []:
            field, value = _oneof_name(getattr(error, "error_code", None))
            if field in _CODE_CLASS:
                found.add(_CODE_CLASS[field])
            elif "NOT_FOUND" in value:
                found.add(NOT_FOUND)
        status = _status_name(link)
        if status in _STATUS_CLASS and not found:
            found.add(_STATUS_CLASS[status])
        # google.api_core's own exception types, for calls made without the
        # GoogleAdsException wrapper.
        if type_name in ("ServiceUnavailable", "DeadlineExceeded", "InternalServerError"):
            found.add(TRANSIENT)
        if type_name == "ResourceExhausted":
            found.add(QUOTA)
    for name in _PRECEDENCE:
        if name in found:
            return name
    return None


def is_transient(exc: BaseException) -> bool:
    """Whether retrying the same READ could plausibly succeed."""
    return classify(exc) == TRANSIENT


def prefixed(exc: BaseException) -> str:
    """``exc``'s message with its class in front and advice behind."""
    message = str(exc).strip() or type(exc).__name__
    kind = classify(exc)
    if kind is None:
        return message
    if kind == POLICY:
        topics = policy_topics(exc)
        if topics:
            message += f"\nPolicy topics: {', '.join(topics)}"
    return f"{kind}: {message}\n{ADVICE[kind]}"
