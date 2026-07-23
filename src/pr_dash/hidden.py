from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pr_dash.config import Config

# Same schema as the browser's localStorage map (app.js): the two are merged
# both ways, so the shapes must stay identical.
#   { pr_id: { "head_sha": str, "hidden_at": iso8601 } }


def _path(cfg: Config) -> Path:
    return cfg.cache_dir / "hidden.json"


def load(cfg: Config) -> dict:
    """Read the hidden map, tolerating a missing or corrupt file (returns {})."""
    path = _path(cfg)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(cfg: Config, mapping: dict) -> None:
    """Atomically write the hidden map (tmp file + os.replace)."""
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        suffix=".tmp",
    ) as tmp:
        json.dump(mapping, tmp)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def apply_ops(cfg: Config, ops: list[dict]) -> dict:
    """Apply hide/unhide ops to the persisted map and return the new map.

    An op is {"op": "hide"|"unhide", "pr_id": str, "head_sha": str|null,
    "hidden_at": iso|null}. A hide inserts/overwrites the entry; an unhide
    deletes it. Unknown ops and ops without a pr_id are ignored.
    """
    mapping = load(cfg)
    for op in ops:
        pr_id = op.get("pr_id")
        if not pr_id:
            continue
        kind = op.get("op")
        if kind == "hide":
            mapping[pr_id] = {
                "head_sha": op.get("head_sha"),
                "hidden_at": op.get("hidden_at"),
            }
        elif kind == "unhide":
            mapping.pop(pr_id, None)
    save(cfg, mapping)
    return mapping


def _current_sha(item: dict) -> str | None:
    members = item.get("members") or []
    if members and members[0].get("head_sha"):
        return members[0]["head_sha"]
    return item.get("head_sha")


def prune(mapping: dict, items: list[dict]) -> dict:
    """Drop entries whose PR no longer exists or whose head_sha moved.

    Mirrors app.js `isHidden`: a hide only holds while the PR is still around
    and its head commit is unchanged - a push auto-unhides it. Returns a new
    map; callers persist it when they have somewhere to write it back.
    """
    by_id = {it["id"]: it for it in items}
    out: dict = {}
    for pr_id, entry in mapping.items():
        item = by_id.get(pr_id)
        if item is None:
            continue
        stored = (entry or {}).get("head_sha")
        current = _current_sha(item)
        if stored and current and stored != current:
            continue
        out[pr_id] = entry
    return out
