from pathlib import Path

from pr_dash.commands import PRForCommands, build
from pr_dash.config import RepoSpec

REPOS = {
    "odoo/odoo": RepoSpec(default=Path("/home/dev/odoo")),
    "odoo/enterprise": RepoSpec(default=Path("/home/dev/enterprise")),
}


def test_single_repo_with_modules_switches_sibling_to_target_branch():
    pr = PRForCommands(repo="odoo/odoo", number=42, target_branch="19.0", modules=["sale", "account"])
    cmds = build(pr, REPOS)
    labels = [c.label for c in cmds]
    assert labels == ["Checkout", "Fresh DB", "Test", "Cleanup"]
    checkout = cmds[0].command
    assert "fetch origin pull/42/head:pr-42" in checkout
    assert "/home/dev/odoo" in checkout
    # Sibling enterprise repo should also be moved to 19.0 with a ff-only update
    assert "/home/dev/enterprise" in checkout
    assert "git -C /home/dev/enterprise checkout 19.0" in checkout
    assert "merge --ff-only origin/19.0" in checkout
    # Multi-line format with && \ continuation
    assert " \\\n" in checkout
    assert cmds[1].command == "onew pr_42 -i sale,account"
    assert cmds[2].command == "otest pr_42 /sale,/account"
    assert "ocleanup pr_42 y" in cmds[3].command
    assert "git -C /home/dev/odoo checkout -" in cmds[3].command
    assert "git -C /home/dev/enterprise checkout -" in cmds[3].command


def test_paired_pr_includes_both_repos():
    pr = PRForCommands(
        repo="odoo/odoo", number=100, target_branch="master", modules=["sale"],
        paired_repo="odoo/enterprise", paired_number=200,
    )
    cmds = build(pr, REPOS)
    checkout = cmds[0].command
    assert "pull/100/head:pr-100" in checkout
    assert "pull/200/head:pr-200" in checkout
    assert "/home/dev/odoo" in checkout
    assert "/home/dev/enterprise" in checkout
    cleanup = cmds[-1].command
    assert "git -C /home/dev/odoo checkout -" in cleanup
    assert "git -C /home/dev/enterprise checkout -" in cleanup


def test_framework_only_pr_has_no_test_command():
    pr = PRForCommands(repo="odoo/odoo", number=7, target_branch="master", modules=[])
    cmds = build(pr, REPOS)
    labels = [c.label for c in cmds]
    assert "Test" not in labels
    assert any("framework-only" in c.command for c in cmds)


def test_custom_command_templates():
    pr = PRForCommands(repo="odoo/odoo", number=42, target_branch="19.0", modules=["sale", "account"])
    templates = {
        "fresh_db": "mydb create {db} --install {modules}",
        "test": "pytest {tags} # {number} on {branch}",
        "cleanup": "dropdb {db}",
    }
    cmds = {c.label: c.command for c in build(pr, REPOS, templates)}
    assert cmds["Fresh DB"] == "mydb create pr_42 --install sale,account"
    assert cmds["Test"] == "pytest /sale,/account # 42 on 19.0"
    assert cmds["Cleanup"].startswith("dropdb pr_42")
    # git checkout- steps are still appended to cleanup automatically
    assert "git -C /home/dev/odoo checkout -" in cmds["Cleanup"]


def test_enterprise_only_switches_odoo_to_target_branch():
    pr = PRForCommands(repo="odoo/enterprise", number=55, target_branch="19.0",
                       modules=["account_accountant"])
    cmds = build(pr, REPOS)
    checkout = cmds[0].command
    # PR's repo gets the pull ref
    assert "/home/dev/enterprise" in checkout
    assert "pull/55/head:pr-55" in checkout
    # Sibling repo gets the target branch + ff-only update
    assert "/home/dev/odoo" in checkout
    assert "git -C /home/dev/odoo checkout 19.0" in checkout
    assert "merge --ff-only origin/19.0" in checkout
    # Cleanup returns both
    cleanup = cmds[-1].command
    assert "git -C /home/dev/enterprise checkout -" in cleanup
    assert "git -C /home/dev/odoo checkout -" in cleanup


def test_paired_pr_does_not_switch_base_branch():
    # When both repos are part of the PR, they're already on the right branches -
    # don't try to switch them again.
    pr = PRForCommands(
        repo="odoo/odoo", number=100, target_branch="19.0", modules=["sale"],
        paired_repo="odoo/enterprise", paired_number=200,
    )
    cmds = build(pr, REPOS)
    checkout = cmds[0].command
    # No "checkout 19.0" sibling step - only PR refs
    assert "checkout 19.0" not in checkout
    assert "checkout pr-100" in checkout
    assert "checkout pr-200" in checkout


def test_skips_unknown_repo():
    pr = PRForCommands(repo="other/repo", number=1, target_branch="master", modules=["foo"])
    cmds = build(pr, REPOS)
    # No checkout/cleanup since repo not in map; DB commands still present
    labels = [c.label for c in cmds]
    assert "Checkout" not in labels
    assert "Fresh DB" in labels
    assert "Cleanup" not in labels


# --- Worktree-per-version layout -------------------------------------------

def _worktree_repos(tmp_path, branch):
    """Build a REPOS map where both repos use a {branch} worktree pattern, with
    the worktree dirs for `branch` actually present on disk so they resolve."""
    odoo_wt = tmp_path / "wt" / f"odoo-{branch}"
    ent_wt = tmp_path / "wt" / f"enterprise-{branch}"
    odoo_wt.mkdir(parents=True)
    ent_wt.mkdir(parents=True)
    return {
        "odoo/odoo": RepoSpec(default=tmp_path / "src" / "odoo",
                              pattern=str(tmp_path / "wt" / "odoo-{branch}")),  # noqa: RUF027
        "odoo/enterprise": RepoSpec(default=tmp_path / "src" / "enterprise",
                                    pattern=str(tmp_path / "wt" / "enterprise-{branch}")),  # noqa: RUF027
    }, odoo_wt, ent_wt


def test_worktree_checkout_uses_branch_worktree_and_skips_sibling_dance(tmp_path):
    repos, odoo_wt, ent_wt = _worktree_repos(tmp_path, "17.0")
    pr = PRForCommands(repo="odoo/odoo", number=42, target_branch="17.0", modules=["sale"])
    cmds = {c.label: c.command for c in build(pr, repos)}
    checkout = cmds["Checkout"]
    # PR repo checks out in the 17.0 worktree, not the default clone
    assert f"git -C {odoo_wt} fetch origin pull/42/head:pr-42" in checkout
    assert f"git -C {odoo_wt} checkout pr-42" in checkout
    assert str(tmp_path / "src" / "odoo") not in checkout
    # Sibling worktree is already on 17.0 - no fetch/checkout/merge, no cleanup
    assert str(ent_wt) not in checkout
    assert "merge --ff-only" not in checkout
    cleanup = cmds["Cleanup"]
    assert f"git -C {odoo_wt} checkout -" in cleanup
    assert str(ent_wt) not in cleanup
    # repo_path placeholder resolves to the worktree
    assert cmds["Fresh DB"] == "onew pr_42 -i sale"


def test_worktree_falls_back_to_default_when_branch_has_no_worktree(tmp_path):
    # Worktrees exist for 17.0 only; a PR targeting 18.0 falls back to the
    # shared clone and the sibling-switch dance returns.
    repos, _, _ = _worktree_repos(tmp_path, "17.0")
    pr = PRForCommands(repo="odoo/odoo", number=9, target_branch="18.0", modules=["sale"])
    cmds = {c.label: c.command for c in build(pr, repos)}
    checkout = cmds["Checkout"]
    odoo_default = tmp_path / "src" / "odoo"
    ent_default = tmp_path / "src" / "enterprise"
    assert f"git -C {odoo_default} fetch origin pull/9/head:pr-9" in checkout
    # No worktree for the sibling at 18.0 either -> falls back and gets switched
    assert f"git -C {ent_default} checkout 18.0" in checkout
    assert "merge --ff-only origin/18.0" in checkout
