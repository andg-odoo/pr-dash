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


def test_prune_uses_primary_member_sha(tmp_path):
    # current sha is the first member's head_sha (matches app.js isHidden).
    mapping = {"odoo/odoo#1": {"head_sha": "mem", "hidden_at": "t"}}
    item = {"id": "odoo/odoo#1", "head_sha": "top", "members": [{"head_sha": "mem"}]}
    assert hidden.prune(mapping, [item]) == mapping
