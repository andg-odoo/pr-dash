import json
import sqlite3
from pathlib import Path

import pytest

from pr_dash import db, runbot
from tests.fakes import FakeClock, FakeRunbotHttp

FIXTURES = Path(__file__).parent / "fixtures" / "runbot"


def _json(name: str):
    return json.loads((FIXTURES / name).read_text())


def _text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _static(build: dict, file: str) -> str:
    return f"http://{build['host']}/runbot/static/build/{build['dest']}/logs/{file}"


@pytest.fixture
def fake():
    return FakeRunbotHttp()


@pytest.fixture
def client(tmp_path, fake):
    return runbot.Runbot(db.connect(tmp_path / "c.db"), fake, "sid", FakeClock())


def test_failing_tests_are_read_from_the_whole_step_log(fake, client):
    fake.rows["runbot.build"] = _json("ko_children_query.json")
    logs = {128070971: "enterprise_post_install", 128070202: "enterprise_at_install",
            128064843: "l10n_child", 128059598: "tests_post_install"}
    tour = ("2026-10-08 21:34:00,100 26 ERROR 128059598-20-0-all odoo.addons.website_sale."
            "tests.test_snippets.TestSnippets.test_01_snippet_products_edition.browser: "
            "FAILED: [16/16]\n")
    for b in fake.rows["runbot.build"]:
        name = logs[b["id"]]
        tail, block = _text(f"log_{name}_tail.txt"), _text(f"log_{name}_block.txt")
        # The block sits mid-file, and a retried test logs its marker a second time.
        fake.files[_static(b, b["log_list"].split(",")[-1] + ".txt")] = (
            tail + tour + block + tail + block)
    urls = [f"https://runbot.odoo.com/runbot/batch/2808804/build/{i}" for i in logs]

    triggers = client.triggers([runbot.parse_check_url(u)[1] for u in urls])

    assert len(fake.calls) == 5
    assert [(t.name, t.verdict) for t in triggers] == [
        ("Enterprise Tests", "red"), ("Enterprise Tests", "red"),
        ("L10n  standalone test", "red"), ("With demo", "red")]
    failures = [f for t in triggers for f in t.failures]
    assert [f["test"] for f in failures] == [
        "web_studio: TestUi.test_new_app_and_report",
        "test_mail: TestComposerResultsMass.test_mail_composer_duplicates",
        "l10n_account_edi_ubl_cii_tests: TestUBLBE.test_import_and_create_partner_ubl",
        "website_sale: TestSnippets.test_01_snippet_products_edition"]
    assert [f["summary"][:40] for f in failures] == [
        "FAILED: [16/26] Tour web_studio_new_repo",
        "AttributeError: 'NoneType' object has no",
        "AssertionError: Lists differ: [{'name': ",
        "FAILED: [16/16] Tour website_sale.snippe"]
    # The test frame and the exception are kept, the QWeb frames between them are not.
    assert failures[1]["traceback"].splitlines() == [
        '  File "/data/build/odoo/addons/test_mail/tests/test_mail_composer.py", line 2567, '
        "in test_mail_composer_duplicates",
        "    composer._action_send_mail()",
        "odoo.addons.base.models.ir_qweb.QWebException: Error while render the template",
        "AttributeError: 'NoneType' object has no attribute 'name'",
        "Template: web.external_layout_standard",
        "Path: /t/div[2]/h2",
        'Node: <h2 t-out="layout_document_title or o.name"/>']
    assert {k: failures[0][k] for k in ("build_id", "build_name", "url")} == {
        "build_id": 128070971, "build_name": "Post install tests for !web -> !website",
        "url": "https://runbot.odoo.com/runbot/build/128070971"}


def test_verdict_and_tally_cover_the_whole_tree(fake, client):
    # These trees were read without trigger_id, which runbot sets on every build of a tree.
    green, killed, old, running = (
        [b | {"trigger_id": [3, "Enterprise Tests"]} for b in _json(f"tree_{name}.json")]
        for name in ("red_runbot_light_killed", "killed_install", "red_enterprise_old", "running"))
    green[0] |= {"local_result": "ok", "global_result": "ok"}
    killed[0]["build_time"] = 1812
    # Six more failing children than the old tree had, past the per-tree log budget.
    for child in old[3:9]:
        child["local_result"] = "ko"
    fake.rows["runbot.build"] = green + killed + old + running
    # A worker host refusing one log costs that build its failures, not the tree.
    fake.failing[_static(old[3], old[3]["log_list"].split(",")[-1] + ".txt")] = 502

    # A build shared by two PRs of a Branch set is asked twice and read once.
    ids = [b[0]["id"] for b in (green, killed, old, running, killed)]
    result = client.triggers(ids)
    triggers = {t.build_id: t for t in result}

    # The old tree's logs are garbage-collected, nothing else needs a log.
    assert len(result) == 4
    assert fake.calls == ["rpc runbot.build", *(
        _static(b, b['log_list'].split(',')[-1] + '.txt')
        for b in old[2:2 + runbot.FAILING_LOGS_PER_TREE])]
    assert {i: (t.verdict, t.children, t.failures) for i, t in triggers.items()} == {
        127220311: ("green", {"done": 2, "ok": 2, "ko": 0, "killed": 0, "testing": 0}, []),
        128006873: ("red", {"done": 2, "ok": 2, "ko": 0, "killed": 0, "testing": 0},
                    [{"killed": "timeout", "step": "install_all", "build_time": 1812,
                      "build_id": 128006873,
                      "build_name": "Enterprise Tests", "url": runbot.build_url(128006873)}]),
        87459850: ("red", {"done": 15, "ok": 8, "ko": 7, "killed": 0, "testing": 0}, [
            {"log": "gone" if i < runbot.FAILING_LOGS_PER_TREE else "not fetched",
             "build_id": b["id"], "build_name": b["description"] or "Enterprise Tests",
             "url": runbot.build_url(b["id"])}
            | ({"log": "fetch failed", "status": 502} if i == 1 else {})
            for i, b in enumerate(old[2:9])]),
        128069548: ("running", {"done": 0, "ok": 0, "ko": 0, "killed": 0, "testing": 0}, []),
    }

    fake.calls, fake.crash_on_build_time = [], True
    [retried] = client.triggers([128006873])
    assert fake.calls == ["rpc runbot.build", "rpc runbot.build"]
    assert (retried.failures[0]["killed"], retried.failures[0]["build_time"]) == (None, None)

    # A parent whose own steps passed while a child still tests is running, not green.
    green[1] |= {"local_state": "testing", "local_result": False}
    assert client.triggers([127220311])[0].verdict == "running"

    # Build 128071236 links reused migration builds, whose red is its red, an orphan child's is not.
    build = {"trigger_id": [9, "ci/upgrade_enterprise"], "local_state": "done", "host": "h",
             "dest": "d", "log_list": False, "description": False}
    fake.rows["runbot.build"] += [
        build | {"id": 128071236, "parent_id": False, "local_result": "ok",
                 "linked_children_build_ids": [128070672, 128070673]},
        build | {"id": 128070672, "parent_id": False, "local_result": "ok"},
        build | {"id": 128070673, "parent_id": False, "local_result": "ko"},
        build | {"id": 128071237, "parent_id": [128071236, ""], "local_result": "ko",
                 "orphan_result": True},
    ]
    fake.calls, fake.crash_on_build_time = [], False
    [upgrade] = client.triggers([128071236])
    assert (upgrade.verdict, upgrade.children, fake.calls) == (
        "red", {"done": 2, "ok": 1, "ko": 1, "killed": 0, "testing": 0}, ["rpc runbot.build"] * 2)
    # A migration that fails outside a test reports its ERROR lines, the markdown name made plain.
    fake.rows["runbot.build"][-2] |= {"log_list": "restore,test-migration",
                                      "description": "Testing migration from **18.0**"}
    fake.files[_static(fake.rows["runbot.build"][-2], "test-migration.txt")] = (
        "2026-10-08 22:12:45,131 60 ERROR db odoo.modules.loading: Some modules have inconsistent"
        " states: ['l10n_cl_edi_stock_reform']\n")
    [upgrade] = client.triggers([128071236])
    assert upgrade.failures[0] | {"url": None} == {
        "error": "odoo.modules.loading: Some modules have inconsistent states:"
                 " ['l10n_cl_edi_stock_reform']",
        "build_id": 128070673, "build_name": "Testing migration from 18.0", "url": None}
    # Without the link, runbot's own global_result still makes it red.
    fake.rows["runbot.build"][-4] |= {"linked_children_build_ids": [], "global_result": "ko"}
    [upgrade] = client.triggers([128071236])
    assert (upgrade.verdict, upgrade.failures[0]["log"]) == ("red", "unparsed")


def test_style_and_policy_checks(fake, client):
    roots = {b["id"]: b for b in _json("builds_style_security_stable_roots.json")}
    style, stable, minimal = roots[127730874], roots[128071553], roots[104334159]
    fake.rows["runbot.build"] = [b | {"trigger_id": [7, "ci/style"]}
                                 for b in (style, stable, minimal)]
    where = {i: {"build_id": i, "build_name": n, "url": runbot.build_url(i)}
             for i, n in ((127730874, "ci/style"), (128071553, "ci/style"),
                          (104334159, "Minimal check before starting other tests"))}
    base = _json("ruff_merge_base_output.json")
    dropped = base.pop()
    fake.files = {
        _static(style, "check_style_ruff-ruff-output.json"): _text("ruff_output.json"),
        _static(style, "merge_base_check_style_ruff-ruff-output.json"): json.dumps(base),
        _static(style, "check_semgrep_style.txt"): _text("log_style_check_semgrep_style.txt"),
        _static(stable, "stable_policy_check.txt"): _text("log_style_check_style_ruff.txt"),
    }

    style_t, stable_t, minimal_t = client.triggers([127730874, 128071553, 104334159])

    assert len(fake.calls) == 5
    # The PR's E902 "No such file" entries are deleted files, only the dropped base entry is new.
    assert style_t.failures == [
        {"rule": dropped["code"],
         "path": dropped["filename"].removeprefix("/data/build/merge_base_"),
         "line": dropped["location"]["row"], "message": dropped["message"], **where[127730874]},
        {"check": "check_semgrep_style", "count": 5, **where[127730874]},
    ]
    # The stable_policy log has no "Findings: N" line, so its count is unknown, not zero.
    assert stable_t.failures == [{"check": "stable_policy_check", "count": None,
                                  **where[128071553]}]
    assert minimal_t.failures == [{"log": "unparsed", **where[104334159]}]


def test_rpc_errors_and_the_request_cap(tmp_path, fake, client):
    fake.error = "odoo.http.SessionExpiredException"
    with pytest.raises(runbot.SessionExpired, match="SessionExpiredException"):
        client.triggers([1])
    fake.error = "odoo.exceptions.ValidationError"
    with pytest.raises(runbot.RpcError, match="ValidationError"):
        client.triggers([1])
    assert fake.calls == ["rpc runbot.build"] * 2
    fake.error = None
    fake.rows["runbot.build"] = [{"id": 1, "parent_id": False}, {"id": 2, "parent_id": [1, "p"]}]
    with pytest.raises(runbot.RunbotError, match="hit its limit of 2"):
        client.search_read("runbot.build", [("id", "child_of", [1])], [], limit=2)

    other = db.connect(tmp_path / "c.db")
    now = client.clock()
    assert all(db.claim_runbot_request(other, now, 30, 150) for _ in range(27))
    calls = len(fake.calls)
    with pytest.raises(runbot.CapReached):
        client.search_read("runbot.build", [("id", "child_of", [1])], [], limit=10)
    assert len(fake.calls) == calls
    for _ in range(4):
        client.clock.advance(hours=1)
        assert all(db.claim_runbot_request(other, client.clock(), 30, 150) for _ in range(30))
    client.clock.advance(hours=1)
    with pytest.raises(runbot.CapReached):
        client.triggers([1])
    client.clock.advance(hours=24)
    assert db.claim_runbot_request(other, client.clock(), 30, 150)
    assert other.execute("SELECT count(*) FROM runbot_request").fetchone()[0] == 1


def _seed_bundles(fake) -> dict[tuple[int, str], dict]:
    """Seed both bundle fixtures as raw rows, returning each slot's build by (batch, trigger)."""
    builds = {}
    for name in ("bundle_named", "bundle_with_prs"):
        data = _json(f"{name}.json")
        bundle = data["bundle"]
        fake.rows.setdefault("runbot.bundle", []).append(bundle)
        for batch in data["batches"]:
            fake.rows.setdefault("runbot.batch", []).append({
                "id": batch["id"], "bundle_id": [bundle["id"], bundle["name"]], "hidden": False,
                "category_id": [batch["category"]["id"], batch["category"]["name"]]})
            for i, slot in enumerate(batch["slots"]):
                trigger = [0, slot["trigger"]]
                builds[batch["id"], slot["trigger"]] = slot["build"] | {
                    "trigger_id": trigger, "parent_id": False, "log_list": False}
                fake.rows.setdefault("runbot.batch.slot", []).append({
                    "id": batch["id"] * 100 + i, "batch_id": [batch["id"], ""],
                    "trigger_id": trigger, "build_id": [slot["build"]["id"], ""], "active": True})
    # A build matched into two batches is one runbot row.
    fake.rows["runbot.build"] = list({b["id"]: b for b in builds.values()}.values())
    return builds


def test_bundle_walk_and_previous_batch(fake, client):
    builds = _seed_bundles(fake)
    latest, before = 2799890, 2795085
    for key in ((latest, "Enterprise Tests"), (latest, "Check Style"),
                (before, "Enterprise Tests")):
        builds[key]["local_result"] = "ko"
    builds[before, "Check Style"]["local_state"] = "testing"
    red = builds[latest, "Enterprise Tests"] | {"log_list": "test_only", "host": "h", "dest": "d"}
    builds[latest, "Enterprise Tests"].update(red)
    fake.files[_static(red, "test_only.txt")] = _text("log_l10n_child_block.txt")

    walk = client.bundle("master-l10n_pe")

    assert fake.calls == ["rpc runbot.bundle", "rpc runbot.bundle", "rpc runbot.batch",
                          "rpc runbot.batch.slot", "rpc runbot.build",
                          _static(red, "test_only.txt"),
                          "rpc runbot.build"]
    [tests] = next(t for t in walk.triggers if t.name == "Enterprise Tests").failures
    assert tests["test"] == ("l10n_account_edi_ubl_cii_tests: "
                             "TestUBLBE.test_import_and_create_partner_ubl")
    assert (walk.bundle, walk.batch_id, len(walk.triggers)) == (
        "master-l10n_pe-withholding-6508826-andg", latest, 16)
    assert {t.name: (t.verdict, t.previous) for t in walk.triggers if t.verdict != "green"} == {
        "Enterprise Tests": ("red", "red"), "Check Style": ("red", None)}
    with pytest.raises(runbot.NoBundle) as miss:
        client.bundle("l10n_pe-withholding-6508826")
    assert miss.value.candidates == sorted(m["name"] for m in _json("bundle_partial.json")[
        "multiple_matches"])
    with pytest.raises(runbot.NoBundle, match="no runbot bundle"):
        client.bundle("nope")
    # A wide partial name lists its first matches by name and counts them all.
    fake.rows["runbot.bundle"] += [{"id": i, "name": f"wide-{i:02}"} for i in range(25)]
    with pytest.raises(runbot.NoBundle) as wide:
        client.bundle("wide")
    assert (wide.value.count, wide.value.candidates) == (
        25, [f"wide-{i:02}" for i in range(runbot.BUNDLE_CANDIDATES)])


def _profile(home: Path, name: str, value: str, accessed: int) -> sqlite3.Connection:
    profile = home / ".mozilla" / "firefox" / name
    profile.mkdir(parents=True)
    conn = sqlite3.connect(profile / "cookies.sqlite", isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("CREATE TABLE moz_cookies (host, name, value, lastAccessed)")
    conn.execute("INSERT INTO moz_cookies VALUES ('runbot.odoo.com', 'session_id', ?, ?)",
                 (value, accessed))
    return conn


def test_find_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("RUNBOT_SESSION_ID", raising=False)
    with pytest.raises(runbot.SessionExpired):
        runbot.find_session_id()

    # Both connections stay open, so each cookie row exists only in its profile's -wal file.
    old = _profile(tmp_path, "a.default", "old", 1)
    new = _profile(tmp_path, "b.default-release", "new", 2)
    assert (tmp_path / ".mozilla/firefox/b.default-release/cookies.sqlite-wal").stat().st_size
    assert runbot.find_session_id() == "new"
    old.close()
    new.close()

    config = tmp_path / ".config" / "runbot-status"
    config.mkdir(parents=True)
    (config / "session_id").write_text("from-file\n")
    assert runbot.find_session_id() == "from-file"
    monkeypatch.setenv("RUNBOT_SESSION_ID", "from-env")
    assert runbot.find_session_id() == "from-env"
