from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pr_dash import derive

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pr-dash" / "config.toml"


@dataclass
class Thresholds:
    staleness_minutes: int = 15
    diff_max_files: int = 100
    diff_max_lines: int = 5000
    diff_max_bytes: int = 2_000_000
    stale_review_days: int = 7


@dataclass
class BucketThresholds:
    M: float = 5.0
    L: float = 20.0
    XL: float = 60.0


@dataclass
class AIConfig:
    enabled: bool = True
    # A ceiling, not a fixed wait: reviews run in parallel and the slowest one
    # gates the run. Sonnet on a mid-size diff measured ~75s, so 90s left no
    # headroom for diffs near review_max_diff_chars; 120s covers the range.
    timeout_seconds: int = 120
    review_enabled: bool = True
    # Both the review gate and the prompt's own diff budget: a PR is queued
    # exactly when its compacted diff fits whole, so nothing is skipped over
    # bytes the model would never see, nor cut down to a size the gate would
    # have rejected. A paired PR's companion half gets half of this on top, as
    # context. Keep in step with ai.DEFAULT_MAX_DIFF_CHARS.
    review_max_diff_chars: int = 50_000
    # Model for the first-pass sanity check. Sonnet is ~2x faster than the
    # default Opus on a triage review and reaches the same verdict; the slower
    # default model was overrunning timeout_seconds. Empty string = CLI default.
    model: str = "sonnet"


@dataclass
class CompanionConfig:
    """Discovery of a bundle's migration PR in a third repo.

    A change that moves data between modules ships its upgrade script in
    odoo/upgrade, which appears in no addons diff and requests no reviewer - so
    "this data move has no migration" gets raised against changes that have one.
    Matching is by head branch, the key robodoo bundles on.

    Deliberately not an entry in `repos`: that table is local clone paths driving
    the checkout/cleanup commands, which have no business switching an upgrade
    clone onto every PR's target branch.
    """
    enabled: bool = True
    repo: str = derive.COMPANION_REPO


@dataclass
class RepoSpec:
    """Where a repo's working copy lives.

    `default` is a single clone path (the classic layout). `pattern` is an
    optional worktree template containing `{branch}`; when set, the worktree for
    a given target branch is preferred and `default` is the fallback for branches
    that have no worktree checked out.
    """
    default: Path | None = None
    pattern: str | None = None

    def worktree_for(self, branch: str) -> Path | None:
        """Return the branch's worktree path if the pattern resolves to an
        existing directory, else None. Existence-gated so a branch without a
        worktree falls back to `default` instead of pointing at a missing dir."""
        if not (self.pattern and branch):
            return None
        p = Path(os.path.expanduser(self.pattern.format(branch=branch)))
        return p if p.is_dir() else None

    def resolve(self, branch: str) -> tuple[Path | None, bool]:
        """Resolve to (path, is_worktree). is_worktree is True only when a
        branch-specific worktree was found - callers use it to skip the
        branch-switching steps a shared clone would need."""
        wt = self.worktree_for(branch)
        if wt is not None:
            return wt, True
        return self.default, False


@dataclass
class Commands:
    # Shell snippets for the per-PR action buttons. Placeholders:
    # {db} {modules} {tags} {repo_path} {number} {branch}. Defaults assume the
    # onew/otest/ocleanup Odoo-dev aliases; retemplate for your own workflow.
    fresh_db: str = "onew {db} -i {modules}"
    test: str = "otest {db} {tags}"
    cleanup: str = "ocleanup {db} y"


@dataclass
class Config:
    github_login: str
    repos: dict[str, RepoSpec]
    thresholds: Thresholds = field(default_factory=Thresholds)
    buckets: BucketThresholds = field(default_factory=BucketThresholds)
    ai: AIConfig = field(default_factory=AIConfig)
    companion: CompanionConfig = field(default_factory=CompanionConfig)
    commands: Commands = field(default_factory=Commands)
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "pr-dash")
    # Port the `pr-dash mcp` server binds on 127.0.0.1 for the dashboard's
    # write-through hidden-state sync. First MCP instance to bind wins.
    hidden_sync_port: int = 7391

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "pr_dash.db"

    @property
    def html_path(self) -> Path:
        return self.cache_dir / "index.html"


def load(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run `pr-dash init` to create one."
        )
    raw = tomllib.loads(path.read_text())

    user = raw.get("user") or {}
    login = user.get("github_login")
    if not login:
        raise ValueError(f"{path}: [user].github_login is required")

    repos_raw = raw.get("repos") or {}
    if not repos_raw:
        raise ValueError(f"{path}: [repos] table is required")
    repos = {k: _parse_repo_spec(path, k, v) for k, v in repos_raw.items()}

    thr_raw = raw.get("thresholds") or {}
    thresholds = Thresholds(**{k: v for k, v in thr_raw.items() if k in Thresholds.__dataclass_fields__})

    buckets_raw = raw.get("bucket_thresholds") or {}
    buckets = BucketThresholds(**{k: v for k, v in buckets_raw.items() if k in BucketThresholds.__dataclass_fields__})

    ai_raw = raw.get("ai") or {}
    ai = AIConfig(**{k: v for k, v in ai_raw.items() if k in AIConfig.__dataclass_fields__})

    comp_raw = raw.get("companion") or {}
    companion = CompanionConfig(**{
        k: v for k, v in comp_raw.items() if k in CompanionConfig.__dataclass_fields__
    })

    cmd_raw = raw.get("commands") or {}
    commands = Commands(**{k: v for k, v in cmd_raw.items() if k in Commands.__dataclass_fields__})

    paths_raw = raw.get("paths") or {}
    cache_dir = Path(os.path.expanduser(paths_raw.get("cache_dir", "~/.cache/pr-dash")))
    hidden_sync_port = int(paths_raw.get("hidden_sync_port", 7391))

    return Config(
        github_login=login,
        repos=repos,
        thresholds=thresholds,
        buckets=buckets,
        ai=ai,
        companion=companion,
        commands=commands,
        cache_dir=cache_dir,
        hidden_sync_port=hidden_sync_port,
    )


def _parse_repo_spec(path: Path, key: str, value: object) -> RepoSpec:
    """A repo entry is either a string clone path, or a table with `pattern`
    (a `{branch}` worktree template) and/or `default` (the fallback clone)."""
    def _expand(p: object, field: str) -> Path:
        if not isinstance(p, str):
            raise ValueError(f'{path}: [repos]."{key}".{field} must be a string path, got {type(p).__name__}')
        return Path(os.path.expanduser(p))

    if isinstance(value, str):
        return RepoSpec(default=Path(os.path.expanduser(value)))
    if isinstance(value, dict):
        pattern = value.get("pattern")
        default = value.get("default")
        if pattern is not None and (not isinstance(pattern, str) or "{branch}" not in pattern):
            raise ValueError(f'{path}: [repos]."{key}".pattern must be a string containing "{{branch}}"')
        if pattern is None and default is None:
            raise ValueError(f'{path}: [repos]."{key}" must set "pattern", "default", or both')
        return RepoSpec(
            default=_expand(default, "default") if default is not None else None,
            pattern=pattern,
        )
    raise ValueError(
        f'{path}: [repos]."{key}" must be a string path or a table, got {type(value).__name__}'
    )


DETECT_FAILED_LOGIN = "your-github-login"


def write_default(path: Path | None = None) -> tuple[Path, str | None]:
    """Write a default config if none exists.

    Returns (path, login): login is the detected GitHub login on a fresh write,
    DETECT_FAILED_LOGIN if auto-detection failed, or None if the file already
    existed and was left untouched.
    """
    path = path or DEFAULT_CONFIG_PATH
    if path.exists():
        return path, None
    path.parent.mkdir(parents=True, exist_ok=True)
    login = _detect_gh_login()
    path.write_text(_render_default(login))
    return path, login


def _detect_gh_login() -> str:
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return result.stdout.strip() or DETECT_FAILED_LOGIN
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return DETECT_FAILED_LOGIN


def _render_default(login: str) -> str:
    return f'''[user]
github_login = "{login}"

[repos]
# Each repo is either a single clone path...
"odoo/odoo" = "~/Dev/src/odoo"
"odoo/enterprise" = "~/Dev/src/enterprise"
# ...or, if you keep one git worktree per version, a table with a `{{branch}}`
# pattern. The worktree matching a PR's target branch is used for its commands;
# `default` is the fallback for branches with no worktree checked out. `{{branch}}`
# can sit anywhere in the path - as a suffix (odoo-{{branch}}) or a parent dir
# ({{branch}}/odoo if you group both repos under one per-branch directory):
#   [repos."odoo/odoo"]
#   pattern = "~/Dev/worktrees/odoo-{{branch}}"
#   default = "~/Dev/src/odoo"

[thresholds]
staleness_minutes = 15
# Over these, a diff is compacted before it is cached - generated files and any
# single file bigger than a whole review prompt become a stub - so a PR drowned
# by one data file keeps the code around it. diff_max_files still bails
# outright: stubbing shrinks depth, and such a PR is wide rather than deep.
diff_max_files = 100
diff_max_lines = 5000
diff_max_bytes = 2000000
stale_review_days = 7

[bucket_thresholds]
M = 5
L = 20
XL = 60

[ai]
enabled = true
# Ceiling per review (reviews run in parallel). Sonnet on a mid-size diff is
# ~75s, so this leaves headroom for larger diffs without false timeouts.
timeout_seconds = 120
review_enabled = true
# Sanity-check model. Sonnet is ~2x faster than the default Opus for triage and
# reaches the same verdict. Set to "" to use the claude CLI's default model.
model = "sonnet"
# AI first-pass review fires per-PR when the diff fits under this cap. This is
# a more honest gate than bucket-based gating, since a breadth-XL PR with a tiny
# diff still reviews fine, and a size-L PR with a huge diff would just truncate.
# Measured after compaction - generated files dropped, oversized ones stubbed -
# and it doubles as the prompt's diff budget, so a queued PR always fits whole.
# A paired PR's companion half is included as context for half this again.
review_max_diff_chars = 50000

[companion]
# A change that moves data between modules ships its migration script in a third
# repo, matched to a PR by head branch - the same key robodoo bundles on and
# runbot groups by. Nobody is ever requested as a reviewer there, so without this
# the migration is invisible and "this data move has no migration" gets raised
# against changes that have one, by you and by the AI first pass alike.
# The repo is private: no access (or no network) means no companions, never a
# failed refresh. Set enabled = false to skip the lookup entirely.
enabled = true
repo = "{derive.COMPANION_REPO}"

[commands]
# Shell snippets for the per-PR action buttons. Placeholders:
#   {{db}} {{modules}} {{tags}} {{repo_path}} {{number}} {{branch}}
# Defaults assume the onew/otest/ocleanup Odoo-dev aliases - replace these with
# however you spin up a DB, run tests, and clean up. Fresh DB / Test are only
# shown when the PR touches installable modules. (Checkout/cleanup git steps
# are generated automatically from your [repos] paths.)
fresh_db = "onew {{db}} -i {{modules}}"
test = "otest {{db}} {{tags}}"
cleanup = "ocleanup {{db}} y"

[paths]
cache_dir = "~/.cache/pr-dash"
# Port the `pr-dash mcp` server binds on 127.0.0.1 so the dashboard page can
# write hidden-PR changes back to disk. The first running MCP instance wins the
# bind; the rest run without the listener.
hidden_sync_port = 7391
'''
