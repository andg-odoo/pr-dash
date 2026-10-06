from pr_dash import branch_set


def _row(pr_id, author="jdoe", head_branch="feature-x"):
    repo, _, number = pr_id.rpartition("#")
    return {"id": pr_id, "repo": repo, "number": int(number), "author": author,
            "head_branch": head_branch, "head_sha": f"sha-{pr_id}"}


def _ids(rows):
    return [r["id"] for r in rows]


def test_a_bundle_with_its_companion_stays_one_set_of_two_halves():
    # 51473f7: the migration made the group three and "exactly two" un-paired the halves.
    [s] = branch_set.group(
        [_row("odoo/upgrade#3"), _row("odoo/enterprise#2"), _row("odoo/odoo#1")])
    assert s.key == ("jdoe", "feature-x")
    assert _ids(s.halves) == ["odoo/odoo#1", "odoo/enterprise#2"]
    assert s.companion["id"] == "odoo/upgrade#3"
    assert s.primary["id"] == "odoo/odoo#1"


def test_members_order_community_enterprise_other_repos_then_companion():
    rows = [_row("odoo/documentation#4"), _row("odoo/upgrade#7"), _row("odoo/documentation#3"),
            _row("odoo/design-themes#9"), _row("odoo/enterprise#2"), _row("odoo/odoo#1")]
    [s] = branch_set.group(rows)
    assert _ids(s.members) == ["odoo/odoo#1", "odoo/enterprise#2", "odoo/design-themes#9",
                               "odoo/documentation#3", "odoo/documentation#4", "odoo/upgrade#7"]


def test_another_author_or_branch_makes_a_set_of_one():
    rows = [_row("odoo/odoo#1"), _row("odoo/enterprise#2", author="asmith"),
            _row("odoo/enterprise#3", head_branch="other")]
    sets = branch_set.group(rows)
    assert [_ids(s.members) for s in sets] == [
        ["odoo/odoo#1"], ["odoo/enterprise#2"], ["odoo/enterprise#3"]]
    assert sets[1].primary["id"] == "odoo/enterprise#2"
    assert sets[1].companion is None


def test_heads_key_joins_the_halves_shas_and_moves_on_any_halfs_push():
    rows = [_row("odoo/odoo#1"), _row("odoo/enterprise#2"), _row("odoo/upgrade#3")]
    rows[0]["head_sha"], rows[1]["head_sha"] = "bbb", "aaa"
    [s] = branch_set.group(rows)
    assert s.heads_key == "aaa+bbb"
    rows[1]["head_sha"] = "ccc"
    [moved] = branch_set.group(rows)
    assert moved.heads_key == "bbb+ccc"


def test_heads_key_of_a_set_of_one_is_its_bare_sha():
    sets = branch_set.group([_row("odoo/odoo#1"), _row("odoo/upgrade#3", head_branch="mig")])
    assert [s.heads_key for s in sets] == ["sha-odoo/odoo#1", "sha-odoo/upgrade#3"]
