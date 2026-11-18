"""Reads retry on a transient failure; nothing else does."""

from __future__ import annotations

import grpc
import pytest

from googleads_reporting import retry
from googleads_reporting.client import GoogleAdsReportingError, ReadOnlyGoogleAdsClient
from googleads_reporting.config import Settings


class Flaky(Exception):
    def __init__(self, status):
        super().__init__(status.name)
        self.error = type("Call", (), {"code": lambda _self: status})()


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(retry, "sleep", waits.append)
    return waits


def test_a_transient_failure_is_retried_with_backoff(instant):
    calls = []

    def read():
        calls.append(1)
        if len(calls) < 3:
            raise Flaky(grpc.StatusCode.UNAVAILABLE)
        return "rows"

    assert retry.retry_read(read) == "rows"
    assert len(calls) == 3
    assert instant == [0.5, 1.0]


def test_it_gives_up_after_three_tries(instant):
    def read():
        raise Flaky(grpc.StatusCode.DEADLINE_EXCEEDED)

    with pytest.raises(Flaky):
        retry.retry_read(read)
    assert len(instant) == 2


@pytest.mark.parametrize(
    "status", [grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.RESOURCE_EXHAUSTED]
)
def test_a_real_answer_is_not_retried(instant, status):
    """A bad query or an exhausted quota will not improve in half a second."""
    calls = []

    def read():
        calls.append(1)
        raise Flaky(status)

    with pytest.raises(Flaky):
        retry.retry_read(read)
    assert calls == [1] and instant == []


class _Service:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def search_stream(self, **_kw):
        self.calls += 1
        if self.calls <= self.failures:
            raise Flaky(grpc.StatusCode.UNAVAILABLE)
        return iter(())


class _Raw:
    def __init__(self, service):
        self.service = service

    def get_service(self, _name, **_kw):
        return self.service


def test_the_read_client_retries_through_its_own_error_wrapper():
    """search_stream wraps the failure in GoogleAdsReportingError; still retried."""
    settings = Settings(
        client_id="x", client_secret="x", refresh_token="x",
        login_customer_id="1234567890", customer_id="0987654321",
        api_version="v25", output_dir=None,
    )
    service = _Service(failures=2)
    client = ReadOnlyGoogleAdsClient(_Raw(service), settings)
    assert client.rows("SELECT campaign.id FROM campaign") == []
    assert service.calls == 3

    service = _Service(failures=5)
    client = ReadOnlyGoogleAdsClient(_Raw(service), settings)
    with pytest.raises(GoogleAdsReportingError):
        client.rows("SELECT campaign.id FROM campaign")
    assert service.calls == 3
