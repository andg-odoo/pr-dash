from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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


DEFAULT_TEMPLATES = {
    "fresh_db": "onew {db} -i {modules}",
    "test": "otest {db} {tags}",
    "cleanup": "ocleanup {db} y",
}


def build(pr: PRForCommands, repo_paths: dict[str, Path],
          templates: dict[str, str] | None = None) -> list[Command]:
    cmds: list[Command] = []
    pr_repo_set = {pr.repo}
    pr_entries: list[tuple[str, int]] = [(pr.repo, pr.number)]
    if pr.paired_repo and pr.paired_number:
        pr_entries.append((pr.paired_repo, pr.paired_number))
        pr_repo_set.add(pr.paired_repo)

    # Resolve paths for the PR's repos
    pr_paths: list[tuple[str, int, str]] = []
    for r, n in pr_entries:
        rp = repo_paths.get(r)
        if rp is None:
            continue
        pr_paths.append((r, n, str(rp)))

    # For single-repo PRs, also switch the *other* configured repos to target_branch
    # so the DB is built against matching framework + addons versions.
    # Skip if we couldn't resolve the PR's own repo - switching siblings alone is pointless.
    sibling_paths: list[tuple[str, str]] = []  # (path, branch)
    is_single_repo = pr.paired_repo is None
    if pr_paths and is_single_repo and pr.target_branch:
        for r, rp in repo_paths.items():
            if r not in pr_repo_set:
                sibling_paths.append((str(rp), pr.target_branch))

    checkout_steps: list[str] = []
    for _, n, rp in pr_paths:
        checkout_steps.append(f"git -C {rp} fetch origin pull/{n}/head:pr-{n}")
        checkout_steps.append(f"git -C {rp} checkout pr-{n}")
    for rp, branch in sibling_paths:
        checkout_steps.append(f"git -C {rp} fetch origin {branch}")
        checkout_steps.append(f"git -C {rp} checkout {branch}")
        checkout_steps.append(f"git -C {rp} merge --ff-only origin/{branch}")

    if checkout_steps:
        cmds.append(Command("Checkout", _chain(checkout_steps)))

    t = {**DEFAULT_TEMPLATES, **(templates or {})}
    db_suffix = f"pr_{pr.number}"
    ctx = {
        "db": db_suffix,
        "modules": ",".join(pr.modules),
        "tags": ",".join(f"/{m}" for m in pr.modules),
        "repo_path": str(repo_paths.get(pr.repo, "")),
        "number": pr.number,
        "branch": pr.target_branch,
    }
    if pr.modules:
        cmds.append(Command("Fresh DB", t["fresh_db"].format(**ctx)))
        cmds.append(Command("Test", t["test"].format(**ctx)))
    else:
        cmds.append(Command("Fresh DB", "# framework-only PR - no installable modules detected"))

    touched_paths = [rp for _, _, rp in pr_paths] + [rp for rp, _ in sibling_paths]
    if touched_paths:
        cleanup_steps = [t["cleanup"].format(**ctx)]
        for rp in touched_paths:
            cleanup_steps.append(f"git -C {rp} checkout -")
        cmds.append(Command("Cleanup", _chain(cleanup_steps)))

    return cmds


def _chain(steps: list[str], indent: str = "  ") -> str:
    """Join shell steps with `&& \\` and a newline, indenting continuations.

    The result is one logical command (chained with &&), but readable and
    paste-safe - bash treats `\\` + newline as a continuation and runs the
    whole thing as a single statement.
    """
    if len(steps) == 1:
        return steps[0]
    out = [steps[0]]
    for s in steps[1:]:
        out.append(f"  && {s}" if indent == "  " else f"{indent}&& {s}")
    return " \\\n".join(out)
