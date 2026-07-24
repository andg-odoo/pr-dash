from __future__ import annotations

from pathlib import Path

from pr_dash import hidden
from pr_dash.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(github_login="me", repos={}, cache_dir=tmp_path)


def _item(pr_id, head_sha):
    return {"id": pr_id, "head_sha": head_sha, "members": [{"head_sha": head_sha}]}


def test_save_load_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    assert hidden.load(cfg) == {}
    m = {"odoo/odoo#1": {"head_sha": "abc", "hidden_at": "2026-07-23T00:00:00+00:00"}}
    hidden.save(cfg, m)
    assert hidden.load(cfg) == m
    # Written to hidden.json, and no stray tmp files left behind.
    assert (tmp_path / "hidden.json").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["hidden.json"]


def test_load_tolerates_corrupt_file(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "hidden.json").write_text("{not json")
    assert hidden.load(cfg) == {}


def test_apply_ops_hide_then_unhide(tmp_path):
    cfg = _cfg(tmp_path)
    m = hidden.apply_ops(
        cfg,
        [
            {
                "op": "hide",
                "pr_id": "odoo/odoo#1",
                "head_sha": "abc",
                "hidden_at": "2026-07-23T00:00:00+00:00",
            },
        ],
    )
    assert m == {
        "odoo/odoo#1": {"head_sha": "abc", "hidden_at": "2026-07-23T00:00:00+00:00"}
    }
    # Persisted.
    assert hidden.load(cfg) == m

    m = hidden.apply_ops(cfg, [{"op": "unhide", "pr_id": "odoo/odoo#1"}])
    assert m == {}
    assert hidden.load(cfg) == {}


def test_apply_ops_ignores_bad_ops(tmp_path):
    cfg = _cfg(tmp_path)
    m = hidden.apply_ops(
        cfg,
        [
            {"op": "hide"},  # no pr_id
            {"op": "bogus", "pr_id": "x"},  # unknown op
            {"op": "unhide", "pr_id": "gone"},  # unhide of absent entry
        ],
    )
    assert m == {}


def test_prune_drops_changed_sha_and_missing(tmp_path):
    mapping = {
        "odoo/odoo#1": {"head_sha": "old", "hidden_at": "t"},  # sha moved -> drop
        "odoo/odoo#2": {"head_sha": "same", "hidden_at": "t"},  # unchanged -> keep
        "odoo/odoo#3": {"head_sha": "x", "hidden_at": "t"},  # missing -> drop
    }
    items = [_item("odoo/odoo#1", "new"), _item("odoo/odoo#2", "same")]
    out = hidden.prune(mapping, items)
    assert out == {"odoo/odoo#2": {"head_sha": "same", "hidden_at": "t"}}


def test_prune_uses_member_sha_for_a_solo_pr(tmp_path):
    # A solo PR's composite is just its member sha (matches app.js isHidden).
    mapping = {"odoo/odoo#1": {"head_sha": "mem", "hidden_at": "t"}}
    item = {"id": "odoo/odoo#1", "head_sha": "top", "members": [{"head_sha": "mem"}]}
    assert hidden.prune(mapping, [item]) == mapping


def _pair(pr_id, odoo_sha, ent_sha):
    return {"id": pr_id, "head_sha": odoo_sha,
            "members": [{"head_sha": odoo_sha}, {"head_sha": ent_sha}]}


def test_item_sha_covers_every_member():
    assert hidden.item_sha(_pair("odoo/odoo#1", "aaa", "bbb")) == "aaa+bbb"


def test_item_sha_is_order_independent():
    assert (hidden.item_sha(_pair("odoo/odoo#1", "bbb", "aaa"))
            == hidden.item_sha(_pair("odoo/odoo#1", "aaa", "bbb")))


def test_item_sha_falls_back_to_top_level_without_members():
    assert hidden.item_sha({"id": "odoo/odoo#1", "head_sha": "top"}) == "top"


def test_prune_expires_a_pair_when_the_enterprise_half_moves():
    # The regression this guards: keying on the primary (odoo) member alone let
    # an enterprise-side push - and every re-review request after it - stay
    # hidden for as long as the odoo half sat still.
    mapping = {"odoo/odoo#1": {"head_sha": "aaa+bbb", "hidden_at": "t"}}
    moved = _pair("odoo/odoo#1", "aaa", "ccc")
    assert hidden.prune(mapping, [moved]) == {}


def test_prune_keeps_a_pair_while_both_halves_sit_still():
    mapping = {"odoo/odoo#1": {"head_sha": "aaa+bbb", "hidden_at": "t"}}
    same = _pair("odoo/odoo#1", "aaa", "bbb")
    assert hidden.prune(mapping, [same]) == mapping


def test_prune_expires_a_pairs_legacy_single_sha_entry():
    # Hides taken under the old rule stored only the odoo sha; they no longer
    # match, which is the intended one-time correction.
    mapping = {"odoo/odoo#1": {"head_sha": "aaa", "hidden_at": "t"}}
    assert hidden.prune(mapping, [_pair("odoo/odoo#1", "aaa", "bbb")]) == {}
