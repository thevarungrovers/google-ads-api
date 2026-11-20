"""A list tool that hits its limit says so, rather than passing a page for the
whole account. Live: find_campaigns returned exactly 50 paused campaigns, the
default limit, with nothing saying more existed."""

from __future__ import annotations

from googleads_mcp import tools_lookup, tools_read
from kindkit import ReadAccount, read_tools, row


def _campaign(i):
    return row(**{"campaign.id": i, "campaign.name": f"c{i}", "campaign.status": "PAUSED",
                  "campaign.advertising_channel_type": "SEARCH", "campaign_budget.id": i,
                  "campaign_budget.amount_micros": 1_000_000, "customer.currency_code": "CAD"})


def test_find_campaigns_ends_with_a_marker_when_more_exist(monkeypatch):
    account = ReadAccount()
    account.answer("FROM campaign", [_campaign(i) for i in range(3)])
    tools = read_tools(tools_read, monkeypatch, account)
    out = tools["find_campaigns"](limit=2)
    assert account.queries[-1].endswith("LIMIT 3"), "one more than asked, to see past the limit"
    assert [c["campaign_id"] for c in out[:2]] == ["0", "1"]
    assert out[2]["truncated"] is True and out[2]["shown"] == 2 and "campaigns" in out[2]["note"]


def test_a_list_that_fits_has_no_marker(monkeypatch):
    account = ReadAccount()
    account.answer("FROM campaign", [_campaign(i) for i in range(2)])
    tools = read_tools(tools_read, monkeypatch, account)
    out = tools["find_campaigns"](limit=2)
    assert len(out) == 2 and not any("truncated" in c for c in out)


def test_list_keywords_and_negatives_say_when_they_were_cut(monkeypatch):
    account = ReadAccount()
    kw = lambda i, neg=False: row(**{
        "ad_group_criterion.criterion_id": i, "ad_group_criterion.keyword.text": f"k{i}",
        "ad_group_criterion.keyword.match_type": "BROAD", "ad_group_criterion.status": "ENABLED",
        "ad_group_criterion.negative": neg, "ad_group.id": 5, "campaign.id": 7})
    account.answer("FROM ad_group_criterion", [kw(i) for i in range(4)])
    tools = read_tools(tools_lookup, monkeypatch, account)
    out = tools["list_keywords"](ad_group_id="5", limit=3)
    assert len(out) == 4 and out[-1]["truncated"] is True and "keywords" in out[-1]["note"]
    negatives = tools["list_negative_keywords"](ad_group_id="5", limit=3)
    assert len(negatives["ad_group_level"]) == 3 and negatives["truncated"] == ["ad_group_level"]
