"""Branch sets: the PRs across repos that share one author and branch name, shown as one row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

# The repo a bundle's migration lives in, ordered last among a set's code halves.
COMPANION_REPO = "odoo/upgrade"

_REPO_RANK = {"odoo/odoo": 0, "odoo/enterprise": 1}


def _order(row: Mapping) -> tuple:
    return (
        row["repo"] == COMPANION_REPO,
        _REPO_RANK.get(row["repo"], 2),
        row["repo"],
        row["number"],
    )


@dataclass(frozen=True)
class BranchSet:
    """One Branch set, its code halves ordered community, enterprise, other repos, upgrade last."""

    key: tuple[str, str]
    halves: tuple[Mapping, ...]
    companion: Mapping | None = None

    @property
    def members(self) -> tuple[Mapping, ...]:
        return self.halves + ((self.companion,) if self.companion else ())

    @property
    def primary(self) -> Mapping:
        return self.halves[0]

    @property
    def heads_key(self) -> str:
        """The code halves' head shas, sorted and joined with "+"."""
        return "+".join(sorted(m["head_sha"] for m in self.halves))

    def context(self, member: Mapping, diff_cached: Callable[[Mapping], bool]) -> tuple:
        """The other code halves with a cached diff, the context of an AI review of `member`."""
        return tuple(h for h in self.halves if h is not member and diff_cached(h))

    def context_heads(self, member: Mapping, diff_cached: Callable[[Mapping], bool]) -> str:
        """The context key an AI review of `member` is stored under, '' when it has no context."""
        return "+".join(sorted(h["head_sha"] for h in self.context(member, diff_cached)))


def from_item(item: Mapping, members: Iterable[Mapping] | None = None) -> BranchSet:
    """The Branch set a rendered Review queue item was built from, optionally with new members."""
    halves = tuple(item["members"] if members is None else members)
    return BranchSet((item["author"], item["head_branch"]), halves, item.get("companion"))


def group(rows: Iterable[Mapping], companions: Iterable[Mapping] = ()) -> list[BranchSet]:
    """Group rows on (author, head branch) and attach each stored Companion to its branch's sets.

    :param rows: the PRs shown as rows, mappings with id, repo, number, author, head_branch, head_sha
    :param companions: stored Companions, joined by branch alone since others often write them
    """
    groups: dict[tuple[str, str], list[Mapping]] = {}
    for row in rows:
        groups.setdefault((row["author"], row["head_branch"]), []).append(row)
    attached = {}
    for row in companions:
        for key, members in groups.items():
            if key[1] == row["head_branch"] and all(m["id"] != row["id"] for m in members):
                attached.setdefault(key, row)
    return [
        BranchSet(key, tuple(sorted(members, key=_order)), attached.get(key))
        for key, members in groups.items()
    ]
