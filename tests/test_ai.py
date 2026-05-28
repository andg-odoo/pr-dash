import json
import subprocess

from pr_dash import ai


def _difffile(path, body="+code\n"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


def test_strip_noise_drops_generated_keeps_code():
    diff = (
        _difffile("sale/models/sale_order.py")
        + _difffile("sale/i18n/fr.po")
        + _difffile("web/static/lib/foo.min.js")
        + _difffile("package-lock.json")
        + _difffile("sale/views/views.xml")
    )
    kept, dropped = ai._strip_noise(diff)
    assert "sale_order.py" in kept and "views.xml" in kept
    assert "fr.po" not in kept and "min.js" not in kept and "package-lock" not in kept
    assert set(dropped) == {"sale/i18n/fr.po", "web/static/lib/foo.min.js", "package-lock.json"}


def test_strip_noise_all_noise_is_empty():
    diff = _difffile("a/i18n/es.po") + _difffile("yarn.lock")
    kept, dropped = ai._strip_noise(diff)
    assert kept == ""
    assert len(dropped) == 2


def test_diff_for_prompt_notes_omissions_and_caps():
    diff = _difffile("m/x.py", body="+a\n" * 100) + _difffile("m/i18n/fr.po")
    out = ai._diff_for_prompt(diff, cap=50)
    assert "omitted" in out and "fr.po" in out
    # cap applies to the kept code, the note is appended after
    assert "x.py" in out


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

    result = ai._review_one(_req(), timeout=90, model="sonnet")

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
    ai._review_one(_req(), timeout=90, model="sonnet")

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
    ai._review_one(_req(), timeout=90, model="")

    assert "--model" not in captured["args"]


def test_timeout_is_terminal_no_retry(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd="claude", timeout=90)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ai._review_one(_req(), timeout=90, model="sonnet")

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
    result = ai._review_one(_req(), timeout=90, model="sonnet")

    assert calls["n"] == 2
    assert result is not None and result.summary == "ok now"


def test_missing_structured_output_retryable(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _envelope(None)  # structured_output is null

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ai._review_one(_req(), timeout=90, model="sonnet")

    assert result is None
    assert calls["n"] == 2  # retried once, then gave up
