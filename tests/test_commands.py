from pathlib import Path

from pr_dash.commands import PRForCommands, build

REPO_PATHS = {
    "odoo/odoo": Path("/home/dev/odoo"),
    "odoo/enterprise": Path("/home/dev/enterprise"),
}


def test_single_repo_with_modules_switches_sibling_to_target_branch():
    pr = PRForCommands(repo="odoo/odoo", number=42, target_branch="19.0", modules=["sale", "account"])
    cmds = build(pr, REPO_PATHS)
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
    cmds = build(pr, REPO_PATHS)
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
    cmds = build(pr, REPO_PATHS)
    labels = [c.label for c in cmds]
    assert "Test" not in labels
    assert any("framework-only" in c.command for c in cmds)


def test_enterprise_only_switches_odoo_to_target_branch():
    pr = PRForCommands(repo="odoo/enterprise", number=55, target_branch="19.0",
                       modules=["account_accountant"])
    cmds = build(pr, REPO_PATHS)
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
    cmds = build(pr, REPO_PATHS)
    checkout = cmds[0].command
    # No "checkout 19.0" sibling step - only PR refs
    assert "checkout 19.0" not in checkout
    assert "checkout pr-100" in checkout
    assert "checkout pr-200" in checkout


def test_skips_unknown_repo():
    pr = PRForCommands(repo="other/repo", number=1, target_branch="master", modules=["foo"])
    cmds = build(pr, REPO_PATHS)
    # No checkout/cleanup since repo not in map; DB commands still present
    labels = [c.label for c in cmds]
    assert "Checkout" not in labels
    assert "Fresh DB" in labels
    assert "Cleanup" not in labels
