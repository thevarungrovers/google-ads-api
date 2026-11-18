"""Every failure reaches the agent, prefixed with what kind of failure it is.

Real GoogleAdsFailure protos, built offline, wrapped the way this codebase
wraps them (MutationError / GoogleAdsReportingError raised FROM the library's
exception) -- so the classifier is tested against the shape it will meet.
"""

from __future__ import annotations

import grpc
import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from mcp.server.mcpserver.exceptions import ToolError

from googleads_mcp.errors import refusals_reach_the_caller
from googleads_reporting import errors
from googleads_reporting.client import GoogleAdsReportingError
from googleads_reporting.write.client import MutationError

RAW = GoogleAdsClient(
    credentials=Credentials(token="offline"), developer_token="x",
    version="v25", use_proto_plus=True,
)


class FakeGoogleAdsException(Exception):
    """GoogleAdsException's shape: .failure, .error (a grpc call), .request_id."""

    def __init__(self, failure=None, status=None):
        super().__init__("google said no")
        self.failure = failure
        self.request_id = "req-1"
        self.error = type("Call", (), {"code": lambda _self: status})() if status else None


def failure(field: str, enum_type: str, value: str, *, topic: str | None = None):
    result = RAW.get_type("GoogleAdsFailure")
    error = RAW.get_type("GoogleAdsError")
    enum = getattr(RAW.get_type(enum_type), enum_type.removesuffix("Enum"))
    setattr(error.error_code, field, getattr(enum, value))
    error.message = f"{field} {value}"
    if topic:
        entry = RAW.get_type("PolicyTopicEntry")
        entry.topic = topic
        error.details.policy_finding_details.policy_topic_entries.append(entry)
    result.errors.append(error)
    return result


def wrapped(inner: BaseException, outer=MutationError) -> BaseException:
    try:
        try:
            raise inner
        except BaseException as exc:
            raise outer("mutate_campaigns failed for customer 1.") from exc
    except BaseException as exc:
        return exc


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("quota_error", "QuotaErrorEnum", "RESOURCE_EXHAUSTED"), errors.QUOTA),
        (("authentication_error", "AuthenticationErrorEnum", "OAUTH_TOKEN_EXPIRED"),
         errors.AUTH_EXPIRED),
        (("query_error", "QueryErrorEnum", "UNRECOGNIZED_FIELD"), errors.INVALID_QUERY),
        (("mutate_error", "MutateErrorEnum", "RESOURCE_NOT_FOUND"), errors.NOT_FOUND),
        (("internal_error", "InternalErrorEnum", "INTERNAL_ERROR"), errors.TRANSIENT),
        (("policy_finding_error", "PolicyFindingErrorEnum", "POLICY_FINDING"),
         errors.POLICY),
    ],
)
def test_a_google_failure_is_classified_through_our_wrapper(args, expected):
    exc = wrapped(FakeGoogleAdsException(failure(*args)))
    assert errors.classify(exc) == expected


def test_the_policy_message_names_the_topic():
    exc = wrapped(FakeGoogleAdsException(failure(
        "policy_finding_error", "PolicyFindingErrorEnum", "POLICY_FINDING",
        topic="TRADEMARKS_IN_AD_TEXT",
    )))
    message = errors.prefixed(exc)
    assert message.startswith("POLICY: ")
    assert "TRADEMARKS_IN_AD_TEXT" in message


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (grpc.StatusCode.UNAVAILABLE, errors.TRANSIENT),
        (grpc.StatusCode.DEADLINE_EXCEEDED, errors.TRANSIENT),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, errors.QUOTA),
        (grpc.StatusCode.UNAUTHENTICATED, errors.AUTH_EXPIRED),
    ],
)
def test_a_bare_grpc_status_is_classified(status, expected):
    exc = wrapped(FakeGoogleAdsException(status=status), GoogleAdsReportingError)
    assert errors.classify(exc) == expected
    assert errors.is_transient(exc) is (expected == errors.TRANSIENT)


def test_an_expired_refresh_token_is_auth_expired():
    assert errors.classify(RefreshError("invalid_grant")) == errors.AUTH_EXPIRED


def test_a_policy_error_wins_over_a_transient_one_in_the_same_failure():
    result = failure("internal_error", "InternalErrorEnum", "INTERNAL_ERROR")
    other = failure("policy_finding_error", "PolicyFindingErrorEnum", "POLICY_FINDING")
    result.errors.append(other.errors[0])
    assert errors.classify(FakeGoogleAdsException(result)) == errors.POLICY


# --------------------------------------------------------------------------
# What the tool wrapper turns them into
# --------------------------------------------------------------------------


def _raising(exc):
    @refusals_reach_the_caller
    def tool():
        raise exc

    return tool


def test_an_api_failure_reaches_the_agent_with_its_prefix_and_advice():
    exc = wrapped(FakeGoogleAdsException(
        failure("quota_error", "QuotaErrorEnum", "RESOURCE_EXHAUSTED")
    ))
    with pytest.raises(ToolError) as caught:
        _raising(exc)()
    text = str(caught.value)
    assert text.startswith("QUOTA: mutate_campaigns failed")
    assert "Back off" in text


def test_a_deliberate_refusal_keeps_its_own_wording():
    with pytest.raises(ToolError, match="^new_status must be ENABLED"):
        _raising(ValueError("new_status must be ENABLED or PAUSED"))()


def test_a_lookup_failure_is_not_found_without_keyerror_quotes():
    with pytest.raises(ToolError) as caught:
        _raising(KeyError("Unknown report 'x'. Available: a, b."))()
    assert str(caught.value) == "NOT_FOUND: Unknown report 'x'. Available: a, b."


def test_an_unexpected_exception_still_reaches_the_agent():
    """A bare 'Error executing tool' is what this whole module exists to prevent."""
    with pytest.raises(ToolError, match="^UNEXPECTED: TypeError: boom"):
        _raising(TypeError("boom"))()
