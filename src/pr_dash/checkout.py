"""Local git checkouts of a branch, found by reading each candidate's HEAD without running git."""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import TYPE_CHECKING

from pr_dash import tab

if TYPE_CHECKING:
    from pr_dash.config import Config


def head_branch(path: Path) -> str | None:
    """The branch checked out at `path`, None for a detached HEAD or a dir that is no checkout."""
    git = path / ".git"
    try:
        if git.is_file():
            git = path / git.read_text().removeprefix("gitdir:").strip()
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return None
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else None


def find(globs: list[str], repo_short: str, branch: str) -> list[Path]:
    """Checkouts of `branch` matched by `globs`, each dir named after its repo (`.../odoo`)."""
    paths = {Path(p) for g in globs for p in glob.glob(os.path.expanduser(g))}
    return sorted(p for p in paths if p.name == repo_short and head_branch(p) == branch)


def diff_item(cfg: Config, ref: str | int) -> dict:
    """The Review queue row get_diff serves, else a miss saying how to diff an Authored PR locally."""
    try:
        return tab.only(cfg, ref, "get_diff")
    except tab.Elsewhere as e:
        if e.tab is not tab.MINE:
            raise
        raise tab.Elsewhere(e.tab, e.row, f"{e} {_hint(cfg, e.row, ref)}") from e.__cause__


def _hint(cfg: Config, branch_set: dict, ref: str | int) -> str:
    """The local checkout of the Branch set member `ref` names, else a `gh pr diff` command."""
    _, pr = tab.hits([branch_set], ref, tab.mine_prs)[0]
    gh = f"`gh pr diff {pr['number']} -R {pr['repo']}`"
    member = next(
        (m for m in branch_set["members"] if (m["repo"], m["num"]) == (pr["repo"], pr["number"])),
        None,
    )
    if member is None:
        return f"It is a Forward-port with no local branch, run {gh}."
    paths = find(cfg.checkout_globs, pr["repo_short"], branch_set["key"])
    if not paths:
        return f"No local checkout of {branch_set['key']} found, run {gh}."
    cmds = " or ".join(f"`git -C {p} diff origin/{member['target_branch']}...HEAD`" for p in paths)
    return f"Diff its local checkout: {cmds}."
