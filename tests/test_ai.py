import json
import subprocess

from pr_dash import ai


def _difffile(path, body="+code\n"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


def test_diff_for_prompt_notes_omissions_and_caps():
    diff = _difffile("m/x.py", body="+a\n" * 100) + _difffile("m/i18n/fr.po")
    out = ai._diff_for_prompt(diff, cap=50)
    assert "omitted" in out and "fr.po" in out
    # cap applies to the kept code, the notes are appended after
    assert "x.py" in out and "truncated at 50" in out


def test_diff_for_prompt_flags_stub_as_ours():
    diff = _difffile("m/data/big.csv", body="+row\n" * 20_000) + _difffile("m/x.py")
    out = ai._diff_for_prompt(diff, cap=100_000, repo="odoo/odoo", number=7)
    # The model must read the stub as pr-dash omitting a file, not as the PR
    # emptying one - inline in the diff and again in the trailing note.
    assert "omitted by pr-dash" in out and "pull/7/files#diff-" in out
    assert "pr-dash stub" in out and "m/data/big.csv" in out
    assert "+row" not in out


def test_diff_for_prompt_all_noise_message():
    out = ai._diff_for_prompt(_difffile("m/i18n/fr.po"), cap=1000)
    assert "nothing to review" in out


def _req():
    return ai.ReviewRequest(
        head_sha="deadbeef1234",
        title="t",
        body="b",
        modules=["sale"],
        branch="16.0",
        diff="diff --git a b",
    )


def _envelope(structured_output, *, is_error=False, subtype="success"):
    class Proc:
        stdout = json.dumps({
            "is_error": is_error,
            "subtype": subtype,
            "result": "some prose narration the model wrote",
            "structured_output": structured_output,
        })
    return Proc()


def test_parses_structured_output(monkeypatch):
    so = {
        "summary": "adds a line",
        "verdict": "minor",
        "concerns": [{"severity": "med", "message": "missing ensure_one", "where": "sale.py"}],
    }
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _envelope(so))

    result = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000).result

    assert result is not None
    assert result.summary == "adds a line"
    assert result.verdict == "minor"
    assert result.concerns == [{"severity": "med", "message": "missing ensure_one", "where": "sale.py"}]


def test_uses_json_schema_and_no_tools(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _envelope({"summary": "x", "verdict": "looks-good", "concerns": []})

    monkeypatch.setattr(subprocess, "run", fake_run)
    ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    args = captured["args"]
    assert "--allowedTools" in args and args[args.index("--allowedTools") + 1] == ""
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert schema["required"] == ["summary", "verdict", "concerns"]
    assert args[args.index("--model") + 1] == "sonnet"


def test_empty_model_omits_flag(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _envelope({"summary": "x", "verdict": "looks-good", "concerns": []})

    monkeypatch.setattr(subprocess, "run", fake_run)
    ai._review_one(_req(), timeout=90, model="", cap=50_000)

    assert "--model" not in captured["args"]


def test_timeout_is_terminal_no_retry(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd="claude", timeout=90)

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    assert (outcome.result, outcome.reason) == (None, "timeout")
    assert calls["n"] == 1  # timeout must not trigger a second 90s attempt


def test_error_envelope_retries_once(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _envelope(None, is_error=True, subtype="error_during_execution")
        return _envelope({"summary": "ok now", "verdict": "looks-good", "concerns": []})

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    assert calls["n"] == 2
    assert outcome.result is not None and outcome.result.summary == "ok now"


def test_missing_structured_output_retryable(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _envelope(None)  # structured_output is null

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    assert (outcome.result, outcome.reason) == (None, "no-structured-output")
    assert calls["n"] == 2  # retried once, then gave up


def test_clean_pr_with_no_concerns_is_a_result(monkeypatch):
    # A trivially clean PR answers with no concerns, which is a looks-good to store.
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _envelope({"summary": "", "verdict": "looks-good",
                                                   "concerns": []}))
    outcome = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    assert outcome.reason == ""
    assert outcome.result is not None and outcome.result.verdict == "looks-good"


def test_verdictless_payload_is_empty(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _envelope({"note": "I could not review this"}))
    outcome = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    assert (outcome.result, outcome.reason) == (None, "empty")


def test_batch_reports_a_failure_per_request(monkeypatch):
    monkeypatch.setattr(ai, "is_available", lambda: True)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired(cmd="claude", timeout=90)))
    outcomes = ai.review_batch([_req()], timeout=90)

    assert [(o.head_sha, o.reason) for o in outcomes] == [("deadbeef1234", "timeout")]


def test_batch_without_the_cli_blames_the_cli(monkeypatch):
    # Every queued PR comes back unreviewed, for a reason that is not the PR's fault.
    monkeypatch.setattr(ai, "is_available", lambda: False)
    outcomes = ai.review_batch([_req()], timeout=90)

    assert [o.reason for o in outcomes] == ["cli-missing"]


def _flat_prompt(req):
    """The prompt with its hard wrapping collapsed, so an assertion on a
    sentence doesn't depend on where a line break happens to fall."""
    return " ".join(ai._build_prompt(req, 50_000).split())


def test_prompt_states_the_migration_either_way():
    # Present: the model is told not to raise a missing migration, and gets the
    # script itself - which is in no diff it would otherwise see.
    req = _req()
    req.companion_repo = "odoo/upgrade"
    req.companion = ai.Companion(
        repo="odoo/upgrade", number=10894, title="[ADD] l10n_us: move res.city",
        state="OPEN", diff=_difffile("migrations/l10n_us/pre-migrate.py",
                                     "+util.merge_module(cr, 'a', 'b')\n"),
    )
    present = _flat_prompt(req)
    assert "do NOT flag a missing migration, one exists" in present
    assert "odoo/upgrade#10894, OPEN" in present
    assert "util.merge_module" in present

    # Absent, but *checked*: that is what makes flagging an unmigrated data move
    # honest rather than a guess from an addons-only diff.
    req.companion = None
    absent = _flat_prompt(req)
    assert "No migration PR ships with this change in odoo/upgrade" in absent
    assert "nothing will carry them across" in absent

    # Never looked (repo unreachable, or the feature is off): say neither.
    req.companion_repo = ""
    silent = _flat_prompt(req)
    assert "migration PR" not in silent and "Migration diff" not in silent


def test_prompt_annotates_a_truncated_migration_diff():
    req = _req()
    req.companion_repo = "odoo/upgrade"
    req.companion = ai.Companion(
        repo="odoo/upgrade", number=1,
        diff=_difffile("migrations/m/pre-migrate.py", "+sql\n" * 400),
    )
    # A quarter of the budget, and cut with the same note the rest of the prompt
    # uses - a migration stopping mid-hunk must not read as one that stops there.
    assert "[diff truncated at 25 characters]" in ai._build_prompt(req, 100)
