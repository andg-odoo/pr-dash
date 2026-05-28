from __future__ import annotations

import json
import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from pr_dash import derive

log = logging.getLogger(__name__)

# Generated / translation files carry no review signal but eat the prompt's
# character budget. Stripped before the diff is sent to the model. Mirrors the
# frontend NOISY_RE used to fold these files in the dashboard.
_NOISE_RE = re.compile(
    r"(\.(po|pot|map|lock)$)|(\.min\.(js|css)$)"
    r"|((^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$)",
    re.IGNORECASE,
)


def _strip_noise(diff: str) -> tuple[str, list[str]]:
    """Drop generated/translation files from a combined `.diff` so the model's
    token budget goes to reviewable code. Returns (kept_diff, dropped_paths)."""
    kept: list[str] = []
    dropped: list[str] = []
    for path, chunk in derive.iter_diff_files(diff):
        if path and _NOISE_RE.search(path):
            dropped.append(path)
        else:
            kept.append(chunk)
    return "".join(kept), dropped


def _diff_for_prompt(diff: str, cap: int) -> str:
    """Strip noise, cap, and annotate what was omitted so the model doesn't
    flag e.g. missing translation updates that were intentionally dropped."""
    if not diff:
        return "(no diff available)"
    kept, dropped = _strip_noise(diff)
    if not kept:
        return "(only generated/translation files changed; nothing to review)"
    note = ""
    if dropped:
        shown = ", ".join(dropped[:5]) + ("…" if len(dropped) > 5 else "")
        note = f"\n\n[{len(dropped)} generated/translation file(s) omitted: {shown}]"
    return kept[:cap] + note

def is_available() -> bool:
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


REVIEW_PROMPT_BASE = """You are doing a quick first-pass sanity check on an Odoo pull request.
This is NOT a full code review. It is a triage signal so the reviewer knows
where to look first. Avoid style nits and theoretical concerns. Be specific.

Flag only things a reviewer should actually want to know:
- Bugs that would visibly break behavior
- Missing self.ensure_one(), questionable sudo(), broken record rules
- User-facing strings missing translation (_() pre-18.0, self.env._() 18.0+)
- Hardcoded values or magic strings that look configurable
- Security smells: raw SQL with f-strings, unfiltered user input, unsafe paths
- Logic that contradicts the PR title or description
"""

REVIEW_PROMPT_SINGLE = REVIEW_PROMPT_BASE + """
Title: {title}

Description:
{body}

Modules touched: {modules}
Target branch: {branch}

Diff:
{diff}

Respond with JSON ONLY, no preamble:
{{"summary": "1-2 sentence description of what this PR actually does",
  "concerns": [
    {{"severity": "high|med|low", "message": "specific concern, one line", "where": "file path or null"}}
  ],
  "verdict": "looks-good|minor|major"}}

If you have no concerns, return concerns: []. Max 5 concerns, most important first.
"looks-good" = nothing concerning. "minor" = small things to ask about.
"major" = something that should block merge."""

REVIEW_PROMPT_PAIRED = REVIEW_PROMPT_BASE + """
IMPORTANT: This PR is part of a paired change across odoo/odoo + odoo/enterprise.
The companion change in the sibling repo is included below as CONTEXT ONLY - do
NOT flag missing pieces that are clearly addressed in the companion. Both halves
are reviewed together at merge time, so a model definition in one repo and a
view in the other is normal, not a bug.

You are reviewing THIS half: {title} ({repo}#{number})
Modules touched: {modules}
Target branch: {branch}

Description:
{body}

Companion half (CONTEXT - do not review): {sibling_title} ({sibling_repo}#{sibling_number})
Companion diff (context):
{sibling_diff}

Target diff (REVIEW THIS):
{diff}

Respond with JSON ONLY, no preamble:
{{"summary": "1-2 sentence description of what THIS half does (the companion is just context)",
  "concerns": [
    {{"severity": "high|med|low", "message": "specific concern in THIS half, one line", "where": "file path or null"}}
  ],
  "verdict": "looks-good|minor|major"}}

If you have no concerns, return concerns: []. Max 5 concerns, most important first."""


@dataclass
class ReviewRequest:
    head_sha: str
    title: str
    body: str
    modules: list[str]
    branch: str
    diff: str
    repo: str = ""
    number: int = 0
    sibling_head_sha: str = ""
    sibling_repo: str | None = None
    sibling_number: int | None = None
    sibling_title: str | None = None
    sibling_diff: str | None = None


@dataclass
class ReviewResult:
    head_sha: str
    summary: str
    concerns: list[dict]
    verdict: str


_VERDICTS = ("looks-good", "minor", "major")
_SEVERITIES = ("high", "med", "low")

# Passed to `claude --json-schema` so the answer comes back in the envelope's
# `structured_output` field already conforming to this shape. This removes the
# old failure mode where the model narrated its analysis in prose and we failed
# to parse `result` as JSON.
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": list(_VERDICTS)},
        "concerns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(_SEVERITIES)},
                    "message": {"type": "string"},
                    "where": {"type": ["string", "null"]},
                },
                "required": ["severity", "message"],
            },
        },
    },
    "required": ["summary", "verdict", "concerns"],
}


def _build_prompt(req: ReviewRequest) -> str:
    if req.sibling_diff is not None:
        # Split the diff budget between target (35k) and sibling context (25k).
        return REVIEW_PROMPT_PAIRED.format(
            title=req.title,
            repo=req.repo,
            number=req.number,
            body=(req.body or "(no description)")[:3000],
            modules=", ".join(req.modules) or "(none)",
            branch=req.branch,
            diff=_diff_for_prompt(req.diff, 35_000),
            sibling_repo=req.sibling_repo,
            sibling_number=req.sibling_number,
            sibling_title=req.sibling_title or "(unknown)",
            sibling_diff=_diff_for_prompt(req.sibling_diff, 25_000) if req.sibling_diff else "(no diff)",
        )
    return REVIEW_PROMPT_SINGLE.format(
        title=req.title,
        body=(req.body or "(no description)")[:4000],
        modules=", ".join(req.modules) or "(none)",
        branch=req.branch,
        diff=_diff_for_prompt(req.diff, 50_000),
    )


def _parse_review(parsed: dict, head_sha: str) -> ReviewResult | None:
    summary = str(parsed.get("summary") or "").strip()
    verdict = parsed.get("verdict") or "looks-good"
    if verdict not in _VERDICTS:
        verdict = "looks-good"
    raw_concerns = parsed.get("concerns") or []
    if not isinstance(raw_concerns, list):
        raw_concerns = []
    concerns: list[dict] = []
    for c in raw_concerns[:5]:
        if not isinstance(c, dict):
            continue
        sev = c.get("severity")
        if sev not in _SEVERITIES:
            sev = "low"
        msg = str(c.get("message") or "").strip()
        if not msg:
            continue
        where = c.get("where")
        where = str(where).strip() if where else None
        concerns.append({"severity": sev, "message": msg, "where": where or None})
    if not summary and not concerns:
        return None
    return ReviewResult(head_sha, summary, concerns, verdict)


def _attempt_review(
    prompt: str, timeout: int, head_sha: str, model: str,
) -> tuple[ReviewResult | None, bool]:
    """Run claude once. Returns (result, retryable). retryable is True for
    transient failures (error envelope, absent structured output) worth one
    retry, False for terminal ones (claude missing, timeout).

    `--allowedTools ""` keeps this a single-shot inference: the diff is already
    inline in the prompt, so the model has no reason to use tools, and denying
    them stops it from exploring the filesystem and burning extra turns.
    `--json-schema` makes the model emit its answer in the envelope's
    `structured_output` field, so there is no prose to scrape. `model` pins a
    fast triage model; empty falls back to the CLI default."""
    sha = head_sha[:8]
    args = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--allowedTools", "",
        "--json-schema", json.dumps(REVIEW_SCHEMA),
    ]
    if model:
        args += ["--model", model]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=True,
        )
    except FileNotFoundError:
        log.warning("claude CLI not on PATH; skipping AI review")
        return None, False
    except subprocess.CalledProcessError as e:
        log.warning("claude returned non-zero (%s) for review of %s", e.returncode, sha)
        return None, True
    except subprocess.TimeoutExpired:
        # Retrying with the same timeout just spends another `timeout` seconds
        # and tends to time out again; skip this PR for the run instead.
        log.warning("claude review timed out (%ds) for %s; skipping", timeout, sha)
        return None, False

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.warning("claude returned non-JSON envelope for %s: %.200r", sha, proc.stdout)
        return None, True

    if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
        log.warning("claude review errored for %s: subtype=%s", sha, envelope.get("subtype"))
        return None, True

    parsed = envelope.get("structured_output")
    if not isinstance(parsed, dict):
        log.warning("claude returned no structured output for %s", sha)
        return None, True

    return _parse_review(parsed, head_sha), False


def _review_one(req: ReviewRequest, timeout: int, model: str) -> ReviewResult | None:
    prompt = _build_prompt(req)
    for attempt in range(2):
        result, retryable = _attempt_review(prompt, timeout, req.head_sha, model)
        if result is not None or not retryable:
            return result
        if attempt == 0:
            log.debug("retrying AI review for %s after transient failure", req.head_sha[:8])
    return None


def review_batch(reqs: list[ReviewRequest], *, timeout: int = 90,
                 model: str = "sonnet", max_workers: int = 4) -> dict[str, ReviewResult]:
    if not reqs:
        return {}
    if not is_available():
        log.warning("claude CLI not available; AI review skipped for %d PRs", len(reqs))
        return {}

    out: dict[str, ReviewResult] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_review_one, r, timeout, model): r for r in reqs}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                out[result.head_sha] = result
    return out
