"""Change kinds: every write the server can make, one class each.

See :mod:`.base` for what a kind is and :mod:`.engine` for the sequence every
kind goes through. To add one, subclass ``ChangeKind`` in a module here, list
it in :func:`all_kinds`, pin its two tool names in tests/test_mcp_surface.py
and install.py, and give it the tests the project CLAUDE.md lists.
"""

from __future__ import annotations

from googleads_mcp.changes.base import ChangeKind


def all_kinds() -> list[ChangeKind]:
    """Every published kind, in the order the tools are registered."""
    from googleads_mcp.changes.adgroups import AdGroupCpcBid, AdGroupStatus
    from googleads_mcp.changes.campaigns import CampaignDailyBudget, CampaignStatus

    return [
        CampaignStatus(),
        CampaignDailyBudget(),
        AdGroupStatus(),
        AdGroupCpcBid(),
    ]


__all__ = ["ChangeKind", "all_kinds"]
