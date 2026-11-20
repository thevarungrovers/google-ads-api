"""Report rows carry money as decimal strings. Live: run_report returned
campaign_budget.amount and metrics.cost as floats (1.0, 0.0), against the rule
that everything an agent sees is a decimal string in major units."""

from __future__ import annotations

from googleads_mcp import tools_read
from kindkit import ReadAccount, read_tools, row


def test_report_money_columns_are_decimal_strings(monkeypatch):
    account = ReadAccount()
    account.answer("FROM campaign", [row(**{
        "campaign.id": 7, "campaign.name": "c", "campaign.status": "ENABLED",
        "campaign_budget.amount_micros": 12_340_000, "metrics.cost_micros": 1_234_567,
        "metrics.average_cpc": 456_789, "metrics.clicks": 3,
    })])
    tools = read_tools(tools_read, monkeypatch, account)
    out = tools["run_report"](report="campaigns", days=7)
    r = out["rows"][0]
    assert r["campaign_budget.amount"] == "12.34"
    assert r["metrics.cost"] == "1.23" and r["metrics.average_cpc"] == "0.46"
    assert r["metrics.clicks"] == 3, "counts stay numbers"
