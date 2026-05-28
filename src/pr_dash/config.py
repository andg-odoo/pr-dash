from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pr-dash" / "config.toml"


@dataclass
class Thresholds:
    staleness_minutes: int = 15
    diff_max_files: int = 100
    diff_max_lines: int = 5000
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
    review_max_diff_chars: int = 40_000
    # Model for the first-pass sanity check. Sonnet is ~2x faster than the
    # default Opus on a triage review and reaches the same verdict; the slower
    # default model was overrunning timeout_seconds. Empty string = CLI default.
    model: str = "sonnet"


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
    repos: dict[str, Path]
    thresholds: Thresholds = field(default_factory=Thresholds)
    buckets: BucketThresholds = field(default_factory=BucketThresholds)
    ai: AIConfig = field(default_factory=AIConfig)
    commands: Commands = field(default_factory=Commands)
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "pr-dash")

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
    repos = {k: Path(os.path.expanduser(v)) for k, v in repos_raw.items()}

    thr_raw = raw.get("thresholds") or {}
    thresholds = Thresholds(**{k: v for k, v in thr_raw.items() if k in Thresholds.__dataclass_fields__})

    buckets_raw = raw.get("bucket_thresholds") or {}
    buckets = BucketThresholds(**{k: v for k, v in buckets_raw.items() if k in BucketThresholds.__dataclass_fields__})

    ai_raw = raw.get("ai") or {}
    ai = AIConfig(**{k: v for k, v in ai_raw.items() if k in AIConfig.__dataclass_fields__})

    cmd_raw = raw.get("commands") or {}
    commands = Commands(**{k: v for k, v in cmd_raw.items() if k in Commands.__dataclass_fields__})

    paths_raw = raw.get("paths") or {}
    cache_dir = Path(os.path.expanduser(paths_raw.get("cache_dir", "~/.cache/pr-dash")))

    return Config(
        github_login=login,
        repos=repos,
        thresholds=thresholds,
        buckets=buckets,
        ai=ai,
        commands=commands,
        cache_dir=cache_dir,
    )


def write_default(path: Path | None = None) -> Path:
    path = path or DEFAULT_CONFIG_PATH
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    login = _detect_gh_login()
    path.write_text(_render_default(login))
    return path


def _detect_gh_login() -> str:
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "your-github-login"


def _render_default(login: str) -> str:
    return f'''[user]
github_login = "{login}"

[repos]
"odoo/odoo" = "~/Dev/src/odoo"
"odoo/enterprise" = "~/Dev/src/enterprise"

[thresholds]
staleness_minutes = 15
diff_max_files = 100
diff_max_lines = 5000
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
review_max_diff_chars = 40000

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
'''
