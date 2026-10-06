"""Branch sets: the PRs across repos that share one author and branch name, shown as one row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# The repo whose bundle member carries the migration, so it is the Companion and never a code half.
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
    """One Branch set, its members ordered community, enterprise, other repos, Companion last."""

    key: tuple[str, str]
    members: tuple[Mapping, ...]

    @property
    def halves(self) -> tuple[Mapping, ...]:
        return tuple(m for m in self.members if m["repo"] != COMPANION_REPO)

    @property
    def companion(self) -> Mapping | None:
        return next((m for m in self.members if m["repo"] == COMPANION_REPO), None)

    @property
    def primary(self) -> Mapping:
        """The first code half, or the Companion of a set that has none."""
        return self.members[0]


def group(rows: Iterable[Mapping]) -> list[BranchSet]:
    """Group rows on (author, head branch), in the order each set's first row came.

    :param rows: mappings carrying at least id, repo, number, author, head_branch and head_sha
    """
    groups: dict[tuple[str, str], list[Mapping]] = {}
    for row in rows:
        groups.setdefault((row["author"], row["head_branch"]), []).append(row)
    return [
        BranchSet(key, tuple(sorted(members, key=_order)))
        for key, members in groups.items()
    ]
