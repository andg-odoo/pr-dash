import json
import urllib.error
import urllib.request
from pathlib import Path

from click.testing import CliRunner

from pr_dash import cli, mergebot
from pr_dash.mergebot import LinkedPR

FIXTURES = Path(__file__).parent / "fixtures" / "mergebot"


def _parse(name):
    return mergebot.parse((FIXTURES / f"{name}.html").read_text())


def _checks(state):
    return {c.name: (c.status, c.overridden_by) for c in state.checks}


def test_blocked_on_linked_prs_with_an_overridden_check():
    state = _parse("odoo_odoo_290109_blocked_linked")
    assert (state.state, state.r_plus, state.merge_method) == ("blocked", False, True)
    assert _checks(state) == {
        "legal/cla": ("ok", None),
        "ci/runbot": ("ok", None),
        "ci/upgrade_enterprise": ("ok", None),
        "ci/style": ("ok", "kmagusiak"),
        "ci/security": ("ok", None),
        "ci/l10n": ("ok", None),
        "ci/documentation": ("ok", None),
        "ci/design-theme": ("ok", None),
        "ci/distro-check": ("ok", None),
    }
    assert [c.name for c in state.checks if c.overridden] == ["ci/style"]
    assert state.linked == [
        LinkedPR("odoo/enterprise", 132695, False, ["missing r+"]),
        LinkedPR("odoo/upgrade", 11389, False, ["missing r+"]),
    ]


def test_missing_merge_method_and_statuses():
    state = _parse("odoo_odoo_291953_missing_statuses")
    assert (state.state, state.r_plus, state.merge_method) == ("blocked", False, False)
    assert [(c.name, c.status) for c in state.checks if c.status != "ok"] == [
        ("ci/documentation", "lazy"),
        ("ci/design-theme", "lazy"),
    ]
    assert state.linked == [
        LinkedPR("odoo/enterprise", 133776, False, ["missing statuses", "missing r+"]),
    ]


def test_red_check_not_overridden():
    state = _parse("odoo_odoo_269608_red_check")
    assert (state.state, state.r_plus, state.merge_method, state.linked) == (
        "blocked",
        False,
        True,
        [],
    )
    assert {n: s for n, (s, _) in _checks(state).items() if s != "ok"} == {
        "ci/style": "fail",
        "ci/l10n": "lazy",
        "ci/documentation": "lazy",
        "ci/design-theme": "lazy",
    }


def test_final_states():
    for name in ["odoo_odoo_290657_merged", "odoo_odoo_291981_merged_forward_port"]:
        state = _parse(name)
        assert (state.state, state.r_plus, state.merge_method, state.linked) == (
            "merged",
            None,
            None,
            [],
        )
        assert {(c.status, c.overridden) for c in state.checks} == {(None, False)}
    merged = _parse("odoo_odoo_290657_merged").checks
    assert (len(merged), merged[0].description) == (
        9,
        "Status resent by Andrew Gavgavian (andg)",
    )
    assert _parse("odoo_odoo_255698_closed") == mergebot.MergebotState("closed")
    assert _parse("odoo_odoo_292618_staged") == mergebot.MergebotState("staged")


def test_garbage_and_empty_pages_are_unknown():
    for html in [
        (FIXTURES / "garbage_changelog.html").read_text(),
        "",
        "<h1>x</h1><div>",
    ]:
        state = mergebot.parse(html)
        assert (state.state, state.checks, state.linked, state.r_plus) == (
            "unknown",
            [],
            [],
            None,
        )
        assert state.reason.startswith("unparseable page")


def test_network_failure_is_unknown(monkeypatch):
    def boom(url, timeout):
        msg = "timed out"
        raise TimeoutError(msg)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    state = mergebot.fetch("odoo/odoo", 1)
    assert state.state == "unknown"
    assert (
        state.reason
        == "fetch https://mergebot.odoo.com/odoo/odoo/pull/1 failed: timed out"
    )


def test_not_found_is_unmanaged_but_a_server_error_is_unknown(monkeypatch):
    def respond(code):
        def urlopen(url, timeout):
            raise urllib.error.HTTPError(url, code, "x", {}, None)

        return urlopen

    monkeypatch.setattr(urllib.request, "urlopen", respond(404))
    assert mergebot.fetch("odoo/odoo-ls", 658).state == "unmanaged"
    monkeypatch.setattr(urllib.request, "urlopen", respond(502))
    assert mergebot.fetch("odoo/odoo", 1).state == "unknown"


def test_query_mergebot_resolves_short_and_url_refs(monkeypatch):
    seen = []
    monkeypatch.setattr(
        mergebot,
        "fetch",
        lambda repo, n: seen.append((repo, n)) or mergebot.MergebotState("closed"),
    )
    runner = CliRunner()
    for ref in [
        "enterprise#132695",
        "https://mergebot.odoo.com/odoo/enterprise/pull/132695",
    ]:
        result = runner.invoke(cli.cli, ["query", "mergebot", ref])
        assert json.loads(result.output)["state"] == "closed"
    assert seen == [("odoo/enterprise", 132695)] * 2
    assert runner.invoke(cli.cli, ["query", "mergebot", "132695"]).exit_code == 1
