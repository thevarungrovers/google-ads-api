"""Guardrails and validation in the write layer. No API calls."""

from pathlib import Path

import pytest

from googleads_reporting.config import Settings
from googleads_reporting.write import ads, campaigns
from googleads_reporting.write.client import (
    ALLOWED_MUTATE_SERVICES,
    DEFAULT_MAX_DAILY_BUDGET,
    GuardrailViolation,
    MutatingGoogleAdsClient,
    MutationError,
    MutationResult,
    _request_type_for,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        client_id="960632522930-x.apps.googleusercontent.com",
        client_secret="placeholder",
        refresh_token="placeholder",
        login_customer_id=None,
        customer_id="0987654321",
        api_version="v25",
        output_dir=tmp_path / "output",
    )


class _FakeAudit:
    def __init__(self):
        self.records = []

    def attempt(self, **kw):
        self.records.append(("attempt", kw))
        return "corr-1"

    def outcome(self, cid, **kw):
        self.records.append(("outcome", kw))


@pytest.fixture
def client(settings):
    return MutatingGoogleAdsClient(object(), settings, audit_log=_FakeAudit())


# --------------------------------------------------------------------------
# Budget guardrail
# --------------------------------------------------------------------------


def test_a_plausible_budget_passes(client):
    client.check_daily_budget(50.0)
    client.check_daily_budget(DEFAULT_MAX_DAILY_BUDGET)


def test_an_amount_entered_in_micros_is_refused(client):
    """50_000_000 is what you get typing $50 as micros. It must not go through."""
    with pytest.raises(GuardrailViolation, match="micros"):
        client.check_daily_budget(50_000_000.0)


def test_the_guardrail_can_be_overridden_deliberately(client):
    client.check_daily_budget(50_000_000.0, override=True)


def test_the_ceiling_is_configurable(settings):
    tight = MutatingGoogleAdsClient(
        object(), settings, audit_log=_FakeAudit(), max_daily_budget=10.0
    )
    tight.check_daily_budget(9.99)
    with pytest.raises(GuardrailViolation):
        tight.check_daily_budget(10.01)


# --------------------------------------------------------------------------
# Service allowlist
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALLOWED_MUTATE_SERVICES))
def test_allowlisted_services_are_named(name):
    assert name in ALLOWED_MUTATE_SERVICES


@pytest.mark.parametrize(
    "name",
    ["CustomerService", "ConversionUploadService", "BillingSetupService",
     "CustomerUserAccessService", "AccountLinkService"],
)
def test_services_outside_the_allowlist_are_refused(client, name):
    """The write surface is a chosen list, not 'whatever the library offers'."""
    with pytest.raises(MutationError, match=name):
        client.service(name)


def test_billing_and_account_access_are_not_reachable():
    """The two that would hurt most are absent by construction."""
    assert "BillingSetupService" not in ALLOWED_MUTATE_SERVICES
    assert "CustomerUserAccessService" not in ALLOWED_MUTATE_SERVICES


def test_an_unknown_mutate_method_is_refused():
    with pytest.raises(MutationError, match="Unsupported mutate method"):
        _request_type_for("mutate_billing_setups")


@pytest.mark.parametrize(
    "method",
    ["mutate_campaigns", "mutate_campaign_budgets", "mutate_ad_groups",
     "mutate_ad_group_ads", "mutate_ad_group_criteria"],
)
def test_supported_methods_map_to_a_request_type(method):
    assert _request_type_for(method).startswith("Mutate")


def test_mutate_refuses_an_empty_operation_list(client):
    with pytest.raises(MutationError, match="No operations"):
        client.mutate(
            service_name="CampaignService", method="mutate_campaigns", operations=[]
        )


# --------------------------------------------------------------------------
# MutationResult
# --------------------------------------------------------------------------


def test_a_validated_result_is_not_an_applied_one():
    validated = MutationResult(validated_only=True)
    assert validated.applied is False
    assert "validated" in str(validated)

    applied = MutationResult(validated_only=False, resource_names=["customers/1/x"])
    assert applied.applied is True
    assert "APPLIED" in str(applied)


# --------------------------------------------------------------------------
# Status arguments
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["DELETED", "ARCHIVED", "enabled!", "", "UNKNOWN"])
def test_an_invalid_campaign_status_is_refused_before_any_call(client, status):
    with pytest.raises(MutationError, match="not a settable campaign status"):
        campaigns.plan_set_status(client, status=status, campaign_id="1")


def test_find_requires_exactly_one_identifier(client):
    with pytest.raises(MutationError, match="exactly one"):
        campaigns.find(client)
    with pytest.raises(MutationError, match="exactly one"):
        campaigns.find(client, campaign_id="1", name="x")


# --------------------------------------------------------------------------
# RSA text validation
# --------------------------------------------------------------------------


def test_a_valid_rsa_passes():
    ads.validate_rsa_text(
        ["Fresh produce", "Delivered daily", "Order online"],
        ["Local greens delivered to your door.", "Order by midnight."],
        "https://example.com",
    )


def test_too_few_headlines_is_refused():
    with pytest.raises(MutationError, match="need 3-15 headlines"):
        ads.validate_rsa_text(["one", "two"], ["a", "b"], "https://example.com")


def test_an_over_long_headline_names_itself():
    """The API reports a field path and an enum; this names the text."""
    with pytest.raises(MutationError, match="headline 2 is 35 chars"):
        ads.validate_rsa_text(
            ["ok", "x" * 35, "ok"], ["a", "b"], "https://example.com"
        )


def test_an_over_long_description_is_refused():
    with pytest.raises(MutationError, match="description 1 is 95 chars"):
        ads.validate_rsa_text(["a", "b", "c"], ["y" * 95, "b"], "https://e.com")


def test_a_blank_headline_is_refused():
    with pytest.raises(MutationError, match="headline 2 is blank"):
        ads.validate_rsa_text(["a", "   ", "c"], ["x", "y"], "https://e.com")


@pytest.mark.parametrize("url", ["example.com", "/path", "ftp://e.com", ""])
def test_a_non_http_final_url_is_refused(url):
    with pytest.raises(MutationError, match="absolute http"):
        ads.validate_rsa_text(["a", "b", "c"], ["x", "y"], url)


def test_every_problem_is_reported_at_once():
    """One round trip of corrections, not one problem at a time."""
    with pytest.raises(MutationError) as exc:
        ads.validate_rsa_text(["x" * 40], ["y" * 100], "nope")
    message = str(exc.value)
    assert "headlines" in message
    assert "descriptions" in message
    assert "headline 1 is 40 chars" in message
    assert "absolute http" in message
