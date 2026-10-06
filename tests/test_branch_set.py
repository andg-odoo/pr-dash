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
        [_row("odoo/enterprise#2"), _row("odoo/odoo#1")], [_row("odoo/upgrade#3")])
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
    rows, companions = [_row("odoo/odoo#1"), _row("odoo/enterprise#2")], [_row("odoo/upgrade#3")]
    rows[0]["head_sha"], rows[1]["head_sha"] = "bbb", "aaa"
    [s] = branch_set.group(rows, companions)
    assert s.heads_key == "aaa+bbb"
    rows[1]["head_sha"] = "ccc"
    [moved] = branch_set.group(rows, companions)
    assert moved.heads_key == "bbb+ccc"


def test_heads_key_of_a_set_of_one_is_its_bare_sha():
    sets = branch_set.group([_row("odoo/odoo#1"), _row("odoo/upgrade#3", head_branch="mig")])
    assert [s.heads_key for s in sets] == ["sha-odoo/odoo#1", "sha-odoo/upgrade#3"]


def test_context_is_every_other_half_with_a_cached_diff():
    rows = [_row("odoo/odoo#1"), _row("odoo/enterprise#2"), _row("odoo/design-themes#3")]
    [s] = branch_set.group(rows, [_row("odoo/upgrade#4", author="asmith")])
    odoo, enterprise, themes = s.halves
    assert s.context_heads(odoo, lambda h: True) == "sha-odoo/design-themes#3+sha-odoo/enterprise#2"
    assert (s.context_heads(odoo, lambda h: h is enterprise),
            s.context_heads(themes, lambda h: False)) == ("sha-odoo/enterprise#2", "")


def test_context_follows_the_cached_diff_not_the_archive_state():
    # ff5a1d2 and 730f1ec: keying on the partner being active hid correctly stored reviews.
    odoo, archived = _row("odoo/odoo#1"), {**_row("odoo/enterprise#2"), "archived_at": "t"}
    [s] = branch_set.group([odoo, archived])
    assert (s.context_heads(odoo, lambda h: True),
            s.context_heads(archived, lambda h: False)) == ("sha-odoo/enterprise#2", "")


def test_a_companion_of_another_author_joins_every_set_on_its_branch():
    rows = [_row("odoo/odoo#1"), _row("odoo/enterprise#2", author="asmith")]
    companions = [_row("odoo/upgrade#3", author="bot"), _row("odoo/upgrade#3", author="bot")]
    sets = branch_set.group(rows, companions)
    assert [(_ids(s.halves), s.companion["id"]) for s in sets] == [
        (["odoo/odoo#1"], "odoo/upgrade#3"), (["odoo/enterprise#2"], "odoo/upgrade#3")]


def test_an_upgrade_pr_asked_for_review_stays_a_row_and_still_attaches_as_companion():
    rows = [_row("odoo/odoo#1"), _row("odoo/upgrade#3", author="bot")]
    sets = branch_set.group(rows, [_row("odoo/upgrade#3", author="bot")])
    assert [(_ids(s.halves), s.companion and s.companion["id"]) for s in sets] == [
        (["odoo/odoo#1"], "odoo/upgrade#3"), (["odoo/upgrade#3"], None)]
