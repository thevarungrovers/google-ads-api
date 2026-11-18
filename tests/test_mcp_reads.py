"""Read tools, through the real read client over a fake transport."""

from __future__ import annotations

import pytest

from googleads_mcp import readkit, tools_read
from kindkit import ReadAccount, read_tools, row


def ad_group_rows(n: int):
    return [
        row(**{
            "campaign.id": 1, "campaign.name": "brand", "ad_group.id": i,
            "ad_group.name": f"g{i}", "ad_group.status": "ENABLED",
            "metrics.cost_micros": (n - i) * 1_000_000, "metrics.clicks": 10,
            "metrics.impressions": 100,
        })
        for i in range(n)
    ]


def test_a_report_never_silently_cuts_rows(monkeypatch):
    account = ReadAccount()
    account.answer("FROM ad_group", ad_group_rows(12))
    tools = read_tools(tools_read, monkeypatch, account)

    out = tools["run_report"](report="ad_groups", limit=5)

    assert out["row_count"] == 5
    assert out["truncated"] is True
    assert out["total_rows_available"] == 12
    # Totals are over all twelve rows, not the five returned: 12+11+...+1.
    assert float(out["totals"]["metrics.cost"]) == pytest.approx(78.0)
    assert "LIMIT" not in out["gaql"]


def test_a_report_is_one_row_per_entity_unless_asked_for_days(monkeypatch):
    account = ReadAccount()
    tools = read_tools(tools_read, monkeypatch, account)

    tools["run_report"](report="campaigns")
    tools["run_report"](report="campaigns", segment_by_date=True)

    aggregated, daily = account.queries[-2:]
    assert "segments.date BETWEEN" in aggregated
    assert not aggregated.startswith("SELECT segments.date")
    assert daily.startswith("SELECT segments.date")


def test_the_scan_cap_is_reported_rather_than_hidden(monkeypatch):
    account = ReadAccount()
    account.answer("FROM ad_group", ad_group_rows(8))
    monkeypatch.setattr(readkit, "SCAN_CAP", 3)
    tools = read_tools(tools_read, monkeypatch, account)
    out = readkit.fetch(tools_read.ReadOnlyGoogleAdsClient.from_env(),
                        tools_read.get_report("ad_groups").build(), 10, scan_cap=3)
    assert out["total_rows_available"] == "3+" and out["truncated"] is True


def test_an_ad_group_filter_on_a_report_without_ad_groups_is_refused(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    tools = read_tools(tools_read, monkeypatch, ReadAccount())
    with pytest.raises(ToolError, match="no ad group"):
        tools["run_report"](report="devices", ad_group_id="5")
