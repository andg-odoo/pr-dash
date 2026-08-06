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

    result = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

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
    result = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    assert result is None
    assert calls["n"] == 1  # timeout must not trigger a second 90s attempt


def test_error_envelope_retries_once(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _envelope(None, is_error=True, subtype="error_during_execution")
        return _envelope({"summary": "ok now", "verdict": "looks-good", "concerns": []})

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    assert calls["n"] == 2
    assert result is not None and result.summary == "ok now"


def test_missing_structured_output_retryable(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _envelope(None)  # structured_output is null

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ai._review_one(_req(), timeout=90, model="sonnet", cap=50_000)

    assert result is None
    assert calls["n"] == 2  # retried once, then gave up
