from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from pr_dash.config import Commands, RepoSpec


@dataclass
class Command:
    label: str
    command: str


@dataclass
class PRForCommands:
    repo: str
    number: int
    target_branch: str
    modules: list[str]  # installable only (no _core/_root)
    paired_repo: str | None = None
    paired_number: int | None = None


# Single source of truth for the command snippets is the Commands dataclass in
# config.py; this is just its defaults, used when no [commands] table is configured.
DEFAULT_TEMPLATES = dataclasses.asdict(Commands())


def build(pr: PRForCommands, repos: dict[str, RepoSpec],
          templates: dict[str, str] | None = None) -> list[Command]:
    cmds: list[Command] = []
    branch = pr.target_branch
    pr_repo_set = {pr.repo}
    pr_entries: list[tuple[str, int]] = [(pr.repo, pr.number)]
    if pr.paired_repo and pr.paired_number:
        pr_entries.append((pr.paired_repo, pr.paired_number))
        pr_repo_set.add(pr.paired_repo)

    # Resolve each PR repo to the path for this target branch (a per-version
    # worktree when configured, else the single clone).
    pr_paths: list[tuple[str, int, str]] = []
    for r, n in pr_entries:
        spec = repos.get(r)
        if spec is None:
            continue
        path, _ = spec.resolve(branch)
        if path is None:
            continue
        pr_paths.append((r, n, str(path)))

    # For single-repo PRs, the *other* configured repos must be on target_branch
    # so the DB builds against matching framework + addons versions.
    # A per-version worktree is already on that branch, so it needs no switching
    # (and must not be mutated); a shared clone gets the fetch/checkout/merge dance.
    # Skip entirely if we couldn't resolve the PR's own repo.
    sibling_switch: list[str] = []  # shared-clone paths to switch (and restore)
    is_single_repo = pr.paired_repo is None
    if pr_paths and is_single_repo and branch:
        for r, spec in repos.items():
            if r in pr_repo_set:
                continue
            path, is_worktree = spec.resolve(branch)
            if path is None or is_worktree:
                continue  # worktree already on target_branch - nothing to do
            sibling_switch.append(str(path))

    checkout_steps: list[str] = []
    for _, n, rp in pr_paths:
        checkout_steps.append(f"git -C {rp} fetch origin pull/{n}/head:pr-{n}")
        checkout_steps.append(f"git -C {rp} checkout pr-{n}")
    for rp in sibling_switch:
        checkout_steps.append(f"git -C {rp} fetch origin {branch}")
        checkout_steps.append(f"git -C {rp} checkout {branch}")
        checkout_steps.append(f"git -C {rp} merge --ff-only origin/{branch}")

    if checkout_steps:
        cmds.append(Command("Checkout", _chain(checkout_steps)))

    t = {**DEFAULT_TEMPLATES, **(templates or {})}
    db_suffix = f"pr_{pr.number}"
    pr_repo_path = next((rp for r, _, rp in pr_paths if r == pr.repo), "")
    ctx = {
        "db": db_suffix,
        "modules": ",".join(pr.modules),
        "tags": ",".join(f"/{m}" for m in pr.modules),
        "repo_path": pr_repo_path,
        "number": pr.number,
        "branch": branch,
    }
    if pr.modules:
        cmds.append(Command("Fresh DB", t["fresh_db"].format(**ctx)))
        cmds.append(Command("Test", t["test"].format(**ctx)))
    else:
        cmds.append(Command("Fresh DB", "# framework-only PR - no installable modules detected"))

    # Only paths we actually checked out need restoring. Worktree siblings were
    # never touched, so they stay out of cleanup.
    touched_paths = [rp for _, _, rp in pr_paths] + sibling_switch
    if touched_paths:
        cleanup_steps = [t["cleanup"].format(**ctx)]
        for rp in touched_paths:
            cleanup_steps.append(f"git -C {rp} checkout -")
        cmds.append(Command("Cleanup", _chain(cleanup_steps)))

    return cmds


def _chain(steps: list[str]) -> str:
    """Join shell steps with `&& \\` and a newline, indenting continuations.

    The result is one logical command (chained with &&), but readable and
    paste-safe - bash treats `\\` + newline as a continuation and runs the
    whole thing as a single statement.
    """
    if len(steps) == 1:
        return steps[0]
    return " \\\n".join([steps[0]] + [f"  && {s}" for s in steps[1:]])
