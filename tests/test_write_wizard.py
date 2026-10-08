"""Wizard tests: every answer is checked where it is typed.

The bug these exist for: the wizard collected every answer, built a spec, and
only then validated -- so a bad landing-page URL was reported AFTER a dozen
further questions and a set of image paths. In a wizard that is the worst
possible place to find out.
"""

import builtins

import pytest

from googleads_reporting.write import wizard
from googleads_reporting.write.wizard import Aborted


@pytest.fixture
def answers(monkeypatch):
    """Feed scripted answers to input(), and record what was asked."""
    asked: list[str] = []

    def feed(values):
        queue = list(values)

        def fake_input(prompt=""):
            asked.append(prompt)
            if not queue:
                raise EOFError
            return queue.pop(0)

        monkeypatch.setattr(builtins, "input", fake_input)
        return asked

    return feed


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------


def test_a_bare_domain_is_offered_back_with_https(answers):
    asked = answers(["moontreal.lufa.com", "yes"])
    assert wizard._ask_url("URL") == "https://moontreal.lufa.com"
    assert any("needs a scheme" in a for a in asked)


def test_declining_the_suggestion_asks_again(answers):
    answers(["example.com", "no", "https://example.com/final"])
    assert wizard._ask_url("URL") == "https://example.com/final"


def test_a_full_url_passes_straight_through(answers):
    asked = answers(["https://example.com/page"])
    assert wizard._ask_url("URL") == "https://example.com/page"
    assert not any("needs a scheme" in a for a in asked)


def test_http_is_accepted(answers):
    answers(["http://example.com"])
    assert wizard._ask_url("URL") == "http://example.com"


@pytest.mark.parametrize("junk", ["not a url", "/local/path"])
def test_something_that_is_not_a_domain_is_re_asked(answers, junk):
    answers([junk, "https://example.com"])
    assert wizard._ask_url("URL") == "https://example.com"


def test_the_url_question_is_asked_before_any_image_question(answers):
    """Ordering is the whole point: fail on the URL before asking for files."""
    asked = answers([
        "DISPLAY", "c", "5", "PAUSED", "NO", "g", "0.5",
        "bad url", "https://example.com",
    ])
    with pytest.raises(Aborted):
        wizard.ask_for_campaign()
    prompts = " | ".join(asked)
    assert "Landing page URL" in prompts
    assert "path" not in prompts, "images were asked for before the URL settled"


# --------------------------------------------------------------------------
# Length-limited text
# --------------------------------------------------------------------------


def test_over_long_text_is_rejected_at_the_prompt(answers, capsys):
    answers(["x" * 40, "Lufa Farms"])
    assert wizard._ask_text("Business name", limit=25) == "Lufa Farms"
    assert "too long: 40 chars, max 25" in capsys.readouterr().out


def test_the_message_says_how_much_to_drop(answers, capsys):
    answers(["x" * 30, "ok"])
    wizard._ask_text("Business name", limit=25)
    assert "Drop 5." in capsys.readouterr().out


def test_text_at_exactly_the_limit_is_accepted(answers):
    answers(["x" * 25])
    assert wizard._ask_text("Business name", limit=25) == "x" * 25


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


def test_a_non_number_is_re_asked_with_the_units_restated(answers, capsys):
    answers(["lots", "25"])
    assert wizard._ask_float("Budget") == 25.0
    assert "not micros" in capsys.readouterr().out


def test_zero_and_negative_amounts_are_refused(answers):
    answers(["0", "-5", "25"])
    assert wizard._ask_float("Budget", minimum=0.0) == 25.0


def test_a_default_is_used_on_a_blank_answer(answers):
    answers([""])
    assert wizard._ask_float("Bid", default=0.5) == 0.5


# --------------------------------------------------------------------------
# Choices and aborting
# --------------------------------------------------------------------------


def test_a_choice_is_case_insensitive(answers):
    answers(["display"])
    assert wizard._ask_choice("Channel", ("SEARCH", "DISPLAY")) == "DISPLAY"


def test_an_invalid_choice_is_re_asked(answers):
    answers(["video", "SEARCH"])
    assert wizard._ask_choice("Channel", ("SEARCH", "DISPLAY")) == "SEARCH"


def test_eof_aborts_rather_than_accepting_a_default(answers):
    """No answer must never be read as consent."""
    answers([])
    with pytest.raises(Aborted, match="Nothing was created"):
        wizard._ask("Campaign name")


def test_a_blank_required_answer_is_re_asked(answers, capsys):
    answers(["", "real name"])
    assert wizard._ask("Campaign name") == "real name"
    assert "(required)" in capsys.readouterr().out


# --------------------------------------------------------------------------
# A full run
# --------------------------------------------------------------------------


def test_a_complete_search_campaign_is_assembled(answers):
    answers([
        "SEARCH", "My campaign", "25", "PAUSED", "NO",
        "My ad group", "0.75",
        "organic vegetables", "produce delivery", "",
        "example.com", "yes",
        "Fresh produce", "Local greens", "Order online", "",
        "Delivered to your door.", "Order by midnight.", "",
    ])
    spec = wizard.ask_for_campaign()

    assert spec.name == "My campaign"
    assert spec.channel == "SEARCH"
    assert spec.budget_amount == 25.0
    assert spec.status == "PAUSED"
    assert spec.contains_eu_political_advertising is False
    assert spec.ad_groups[0].keywords == ["organic vegetables", "produce delivery"]
    assert spec.ad_groups[0].cpc_bid == 0.75
    ad = spec.ad_groups[0].ads[0]
    assert ad.final_url == "https://example.com"
    assert len(ad.headlines) == 3
    # The spec it produced must be valid, or the prompts let something through.
    assert spec.problems() == []


def test_declaring_eu_political_advertising_is_carried_through(answers):
    answers([
        "SEARCH", "C", "5", "PAUSED", "YES", "G", "0.5", "",
        "https://example.com",
        "a", "b", "c", "", "x", "y", "",
    ])
    assert wizard.ask_for_campaign().contains_eu_political_advertising is True
