from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pr_dash import branch_set

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pr-dash" / "config.toml"


@dataclass
class Thresholds:
    staleness_minutes: int = 15
    # Timer ticks come every 15 minutes for the mine tab, the review queue search stays hourly.
    queue_interval_minutes: int = 60
    # Tracked PRs are a watch list, not the review queue, so they age slower.
    tracked_staleness_minutes: int = 360
    # Authored PRs replace watching GitHub by hand, so they follow the 15-minute timer.
    mine_staleness_minutes: int = 15
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
    # Failed passes at one review context before the queue gives up and stops paying.
    max_attempts: int = 3
    # Reviews one `refresh --cron` tick may start, so an hourly timer cannot fan out twenty.
    cron_max_reviews: int = 5


@dataclass
class CompanionConfig:
    """Discovery of a bundle's migration PR in a third repo.

    A change that moves data between modules ships its upgrade script in
    odoo/upgrade, which appears in no addons diff and requests no reviewer - so
    "this data move has no migration" gets raised against changes that have one.
    Matching is by head branch, the key robodoo bundles on.
    """
    enabled: bool = True
    repo: str = branch_set.COMPANION_REPO


@dataclass
class Config:
    github_login: str
    thresholds: Thresholds = field(default_factory=Thresholds)
    buckets: BucketThresholds = field(default_factory=BucketThresholds)
    ai: AIConfig = field(default_factory=AIConfig)
    companion: CompanionConfig = field(default_factory=CompanionConfig)
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "pr-dash")
    # Port the `pr-dash mcp` server binds on 127.0.0.1 for the dashboard's
    # write-through mark sync. First MCP instance to bind wins.
    hidden_sync_port: int = 7391
    # Globs of local checkouts, searched for an Authored PR's branch when its diff is asked for.
    checkout_globs: list[str] = field(default_factory=list)

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

    paths_raw = raw.get("paths") or {}
    cache_dir = Path(os.path.expanduser(paths_raw.get("cache_dir", "~/.cache/pr-dash")))
    hidden_sync_port = int(paths_raw.get("hidden_sync_port", 7391))
    checkout_globs = list(paths_raw.get("checkouts", []))

    return Config(
        github_login=login,
        thresholds=thresholds,
        buckets=buckets,
        ai=ai,
        companion=companion,
        cache_dir=cache_dir,
        hidden_sync_port=hidden_sync_port,
        checkout_globs=checkout_globs,
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

[thresholds]
staleness_minutes = 15
# Timer ticks search the review queue at most this often, manual refreshes always do.
queue_interval_minutes = 60
# Tracked PRs are watched, not queued, so a timer tick need not re-read all of them.
tracked_staleness_minutes = 360
# Authored PRs (the mine tab) and their Mergebot pages, read on every timer tick.
mine_staleness_minutes = 15
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
# Failed passes at one PR before the queue stops asking; retries back off 1h, then 4h.
max_attempts = 3
# Reviews one `refresh --cron` tick may start, smallest diff first; a manual run is uncapped.
cron_max_reviews = 5

[companion]
# A change that moves data between modules ships its migration script in a third
# repo, matched to a PR by head branch - the same key robodoo bundles on and
# runbot groups by. Nobody is ever requested as a reviewer there, so without this
# the migration is invisible and "this data move has no migration" gets raised
# against changes that have one, by you and by the AI first pass alike.
# The repo is private: no access (or no network) means no companions, never a
# failed refresh. Set enabled = false to skip the lookup entirely.
enabled = true
repo = "{branch_set.COMPANION_REPO}"

[paths]
cache_dir = "~/.cache/pr-dash"
# Port the `pr-dash mcp` server binds on 127.0.0.1 so the dashboard page can
# write mark changes back to disk. The first running MCP instance wins the
# bind; the rest run without the listener.
hidden_sync_port = 7391
# Globs of checkouts named after their repo, e.g. "~/Dev/wt/*/*", get_diff points at them.
checkouts = []
'''
