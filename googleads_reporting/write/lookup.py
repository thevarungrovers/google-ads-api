"""Finding an entity by name, because that is what people can actually see.

Campaign and ad group IDs are not shown anywhere obvious in the Google Ads UI,
while names are on every screen. So names are the primary way in here, with
three behaviours that make that safe:

* **An exact match wins outright.** "2026 - CPM - Desktop" is a substring of
  nothing else here, but "Brand" might be a substring of "Brand - EN". If a
  name matches exactly, that is the one, and no substring candidate overrides it.
* **Otherwise, case-insensitive substring.** GAQL's ``LIKE`` is
  case-insensitive, so half-remembered capitalisation still finds the campaign.
* **Ambiguity is never resolved silently.** Several matches raise
  :class:`AmbiguousMatch`, which carries the candidates so the caller can print
  them. Picking one for the user is how the wrong campaign gets paused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..query import Query, contains, quote_double
from .client import MutationError


@dataclass(frozen=True)
class Candidate:
    """One possible match, enough to tell it apart from the others."""

    id: str
    name: str
    status: str
    context: str = ""

    def render(self) -> str:
        line = f"  {self.id:<16} {self.name}"
        extras = [part for part in (self.status, self.context) if part]
        if extras:
            line += f"  [{', '.join(extras)}]"
        return line


class NotFound(MutationError):
    """Nothing matched the search."""


class AmbiguousMatch(MutationError):
    """Several things matched, and choosing between them is not ours to do."""

    def __init__(self, entity: str, needle: str, candidates: Sequence[Candidate]):
        self.entity = entity
        self.needle = needle
        self.candidates = list(candidates)
        listing = "\n".join(c.render() for c in self.candidates)
        super().__init__(
            f"{len(self.candidates)} {entity}s match {needle!r}:\n{listing}\n"
            f"Narrow the search, or pass the id directly."
        )


def resolve(
    client: Any,
    *,
    entity: str,
    id_field: str,
    name_field: str,
    resource: str,
    select: Sequence[str],
    needle: str,
    customer_id: str | None = None,
    extra_where: Sequence[str] = (),
    to_candidate=None,
) -> Any:
    """Return the single row matching ``needle`` by name.

    Tries an exact name match first, then a case-insensitive substring.
    """
    def run(condition: str):
        return client.query(
            Query(
                select=tuple(select),
                from_resource=resource,
                where=(condition, *extra_where),
            ).to_gaql(),
            customer_id=customer_id,
        )

    rows = run(f"{name_field} = {quote_double(needle)}")
    if len(rows) == 1:
        return rows[0]

    # An exact match that is itself ambiguous is still ambiguous -- Google Ads
    # does not require campaign names to be unique.
    if len(rows) > 1:
        raise AmbiguousMatch(entity, needle, [to_candidate(r) for r in rows])

    rows = run(contains(name_field, needle))
    if not rows:
        raise NotFound(
            f"No {entity} matching {needle!r}. Names are matched exactly first, "
            "then as a case-insensitive substring."
        )
    if len(rows) > 1:
        raise AmbiguousMatch(entity, needle, [to_candidate(r) for r in rows])
    return rows[0]


def search(
    client: Any,
    *,
    resource: str,
    select: Sequence[str],
    name_field: str,
    needle: str | None,
    customer_id: str | None = None,
    extra_where: Sequence[str] = (),
    limit: int | None = None,
) -> list[Any]:
    """Every row whose name contains ``needle`` (or all rows if None)."""
    where = list(extra_where)
    if needle:
        where.append(contains(name_field, needle))
    return client.query(
        Query(
            select=tuple(select),
            from_resource=resource,
            where=tuple(where),
            order_by=(name_field,),
            limit=limit,
        ).to_gaql(),
        customer_id=customer_id,
    )
