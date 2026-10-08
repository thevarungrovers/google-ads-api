"""Name resolution tests. No API calls -- a fake client answers the queries."""

import pytest

from googleads_reporting.query import QueryError, contains, quote_double
from googleads_reporting.write.lookup import (
    AmbiguousMatch,
    Candidate,
    NotFound,
    resolve,
    search,
)


class _Row:
    def __init__(self, id_, name, status="ENABLED"):
        self.id, self.name, self.status = id_, name, status


class _FakeClient:
    """Answers an exact-match query, then a LIKE query, from one name list."""

    def __init__(self, names):
        self.rows = [_Row(1000 + i, n) for i, n in enumerate(names)]
        self.queries = []

    def query(self, gaql, customer_id=None):
        self.queries.append(gaql)
        # Crudely mimic GAQL: `= "x"` is exact, `LIKE "%x%"` is ci-substring,
        # and no WHERE at all means every row.
        if "LIKE" in gaql:
            needle = gaql.split('LIKE "%', 1)[1].rsplit('%"', 1)[0]
            return [r for r in self.rows if needle.lower() in r.name.lower()]
        if 'name = "' in gaql:
            needle = gaql.split('name = "', 1)[1].rsplit('"', 1)[0]
            return [r for r in self.rows if r.name == needle]
        return list(self.rows)


def _candidate(row):
    return Candidate(id=str(row.id), name=row.name, status=row.status)


def _resolve(client, needle):
    return resolve(
        client, entity="campaign", id_field="x.id", name_field="x.name",
        resource="campaign", select=("campaign.id", "campaign.name"),
        needle=needle, to_candidate=_candidate,
    )


# --------------------------------------------------------------------------
# Resolution order
# --------------------------------------------------------------------------


def test_a_unique_substring_resolves():
    client = _FakeClient(["Brand - EN", "Shopping - FR"])
    assert _resolve(client, "shopping").name == "Shopping - FR"


def test_matching_is_case_insensitive():
    client = _FakeClient(["2026 - En - CPM - Desktop"])
    assert _resolve(client, "2026 - en - cpm - desktop").id == 1000
    assert _resolve(client, "CPM").id == 1000


def test_an_exact_match_wins_over_substring_candidates():
    """'Brand' is a substring of 'Brand - EN', but it is also a real name.

    Without the exact-first pass, asking for "Brand" would be ambiguous even
    though exactly one campaign is called that -- and the user would have no
    way to express "I mean the one named exactly this".
    """
    client = _FakeClient(["Brand", "Brand - EN", "Brand - FR"])
    assert _resolve(client, "Brand").name == "Brand"


def test_the_exact_pass_runs_before_the_substring_pass():
    client = _FakeClient(["Brand", "Brand - EN"])
    _resolve(client, "Brand")
    assert "LIKE" not in client.queries[0], "exact match must be tried first"
    assert len(client.queries) == 1, "no substring query once exact matched"


def test_a_substring_query_only_runs_when_exact_finds_nothing():
    client = _FakeClient(["Brand - EN"])
    _resolve(client, "brand")
    assert len(client.queries) == 2
    assert "LIKE" in client.queries[1]


# --------------------------------------------------------------------------
# Ambiguity
# --------------------------------------------------------------------------


def test_several_substring_matches_are_refused():
    """No exact match, three substring hits -> refuse rather than guess."""
    client = _FakeClient(["A - CPM - Desktop", "B - CPM - Desktop", "C - CPM - Desktop"])
    with pytest.raises(AmbiguousMatch) as exc:
        _resolve(client, "CPM - Desktop")
    assert len(exc.value.candidates) == 3


def test_an_exact_match_resolves_even_when_it_is_a_substring_of_others():
    """The live case: 'CPM - Desktop' names one campaign AND prefixes others."""
    client = _FakeClient(["CPM - Desktop", "En - CPM - Desktop", "X - CPM - Desktop"])
    assert _resolve(client, "CPM - Desktop").name == "CPM - Desktop"


def test_the_ambiguity_error_lists_ids_so_the_user_can_choose():
    client = _FakeClient(["A - CPM", "B - CPM"])
    with pytest.raises(AmbiguousMatch) as exc:
        _resolve(client, "CPM")
    message = str(exc.value)
    assert "1000" in message and "1001" in message
    assert "A - CPM" in message and "B - CPM" in message
    assert "pass the id directly" in message


def test_duplicate_exact_names_are_still_ambiguous():
    """Google Ads does not require campaign names to be unique."""
    client = _FakeClient(["Brand", "Brand"])
    with pytest.raises(AmbiguousMatch):
        _resolve(client, "Brand")


def test_nothing_matching_says_how_matching_works():
    client = _FakeClient(["Brand - EN"])
    with pytest.raises(NotFound, match="case-insensitive substring"):
        _resolve(client, "Shopping")


# --------------------------------------------------------------------------
# Candidate rendering
# --------------------------------------------------------------------------


def test_candidate_renders_id_name_and_context():
    rendered = Candidate("123", "Brand", "ENABLED", "in Campaign X").render()
    assert "123" in rendered
    assert "Brand" in rendered
    assert "ENABLED" in rendered
    assert "in Campaign X" in rendered


def test_candidate_without_context_has_no_empty_brackets():
    assert Candidate("1", "Brand", "", "").render().strip() == "1                Brand"


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def test_search_with_no_needle_returns_everything():
    client = _FakeClient(["A", "B", "C"])
    assert len(search(
        client, resource="campaign", select=("campaign.id", "campaign.name"),
        name_field="campaign.name", needle=None,
    )) == 3


def test_search_filters_by_substring():
    client = _FakeClient(["Brand - EN", "Brand - FR", "Shopping"])
    assert len(search(
        client, resource="campaign", select=("campaign.id", "campaign.name"),
        name_field="campaign.name", needle="brand",
    )) == 2


# --------------------------------------------------------------------------
# Quoting -- names with apostrophes must work
# --------------------------------------------------------------------------


def test_a_name_with_an_apostrophe_is_quotable():
    """Single-quoted GAQL cannot carry these; ordinary French names have them."""
    assert quote_double("Paniers d'été") == '"Paniers d\'été"'
    assert contains("campaign.name", "d'été") == 'campaign.name LIKE "%d\'été%"'


@pytest.mark.parametrize("bad", ['say "hi"', "back\\slash", "line\nbreak"])
def test_a_name_that_could_close_the_literal_is_refused(bad):
    with pytest.raises(QueryError, match="Refusing to quote"):
        quote_double(bad)


def test_an_empty_search_is_refused():
    with pytest.raises(QueryError, match="empty"):
        contains("campaign.name", "   ")
