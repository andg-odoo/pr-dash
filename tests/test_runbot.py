import copy
import json
from pathlib import Path

import pytest

from pr_dash import db, runbot
from tests.fakes import FakeClock, FakeRunbotHttp

FIXTURES = Path(__file__).parent / "fixtures" / "runbot"
PA_BUNDLE = "master-l10n_pa_edi-hka-6572580-andg"


def _json(name: str):
    return json.loads((FIXTURES / name).read_text())


def _static(build: dict, file: str) -> str:
    return build["log_files"][0]["url"].rpartition("/")[0] + "/" + file


@pytest.fixture
def fake():
    fake = FakeRunbotHttp()
    fake.builds = {b["id"]: b for b in _json("builds.json")}
    return fake


@pytest.fixture
def client(tmp_path, fake):
    return runbot.Runbot(db.connect(tmp_path / "c.db"), fake, FakeClock())


def test_failing_tests_are_read_from_their_rows(fake, client):
    l10n = fake.builds[128064843]
    fail = next(r for r in l10n["logs"] if r["message"].startswith("FAIL: "))
    fail["known_error"] = {"id": 7, "first_seen_date": "2026-10-01 02:00:00", "random": True}
    # A retried test logs its row a second time, the first one is kept.
    l10n["logs"].append(fail | {"message": "FAIL: TestUBLBE.test_import_and_create_partner_ubl\n"
                                           "AssertionError: retried"})
    urls = [f"https://runbot.odoo.com/runbot/batch/2808804/build/{i}"
            for i in (128070971, 128064843)]

    studio, edi = client.triggers([runbot.parse_check_url(u)[1] for u in urls])

    assert fake.calls == ["api build/128070971,128064843"]
    assert [(t.name, t.verdict) for t in (studio, edi)] == [
        ("Enterprise Tests", "red"), ("L10n standalone test", "red")]
    [tour], [test] = studio.failures, edi.failures
    # The tour row of the same test is the summary, the traceback keeps the test frame and the tail.
    assert (tour["test"], tour["summary"][:40], tour["tag"], tour["known_error"]) == (
        "web_studio: TestUi.test_new_app_and_report", "FAILED: [16/26] Tour web_studio_new_repo",
        "/web_studio/tests/test_ui.py:TestUi.test_new_app_and_report", False)
    assert test["traceback"].splitlines()[:3] == [
        '  File "/data/build/odoo/addons/l10n_account_edi_ubl_cii_tests/tests/test_xml_ubl_be.py",'
        " line 298, in test_import_and_create_partner_ubl",
        "    self.assertRecordValues(invoice.partner_id, [partner_vals])",
        "AssertionError: Lists differ: [{'name': 'Buyer', 'email': 'buyer@yahoo.c[39 chars]05'}]"
        " != []"]
    assert (test["test"], test["summary"][:28], test["known_error"]["id"]) == (
        "l10n_account_edi_ubl_cii_tests: TestUBLBE.test_import_and_create_partner_ubl",
        "AssertionError: Lists differ", 7)
    assert {k: tour[k] for k in ("build_id", "build_name", "url")} == {
        "build_id": 128070971, "build_name": "Post install tests for !web -> !website",
        "url": "https://runbot.odoo.com/runbot/build/128070971"}


def test_verdict_and_tally_cover_the_whole_tree(fake, client):
    green = fake.builds[127220311]
    green |= {"local_result": "ok", "global_result": "ok"}
    running = copy.deepcopy(green) | {"id": 128069548}
    running["children"][0] |= {"local_state": "testing", "local_result": False}
    fake.builds[128069548] = running

    # A build shared by two PRs of a Branch set is asked twice and read once.
    result = client.triggers([127220311, 128006873, 87459850, 128069548, 128006873])
    triggers = {t.build_id: t for t in result}

    assert fake.calls == ["api build/127220311,128006873,87459850,128069548"]
    assert {i: (t.verdict, t.children, t.failures) for i, t in triggers.items()} == {
        127220311: ("green", {"done": 2, "ok": 2, "ko": 0, "killed": 0, "testing": 0}, []),
        128006873: ("red", {"done": 2, "ok": 2, "ko": 0, "killed": 0, "testing": 0},
                    [{"killed": "timeout", "step": "install_all", "build_time": 1812,
                      "build_id": 128006873, "build_name": "Enterprise Tests",
                      "url": runbot.build_url(128006873)}]),
        # The old child logged no row at all.
        87459850: ("red", {"done": 15, "ok": 14, "ko": 1, "killed": 0, "testing": 0},
                   [{"log": "unparsed", "build_id": 87459864, "build_name": "Enterprise Tests",
                     "url": runbot.build_url(87459864)}]),
        128069548: ("running", {"done": 1, "ok": 1, "ko": 0, "killed": 0, "testing": 1}, []),
    }
    with pytest.raises(runbot.RunbotError, match="API reads 50 at most"):
        client.triggers(list(range(51)))
    assert len(fake.calls) == 1

    # The upgrade build links a migration build, whose red is its red, an orphan child's is not.
    upgrade = fake.builds[131519702]
    [migration] = upgrade["children"]
    row = fake.builds[128071553]["logs"][0]
    migration |= {"local_result": "ko", "logs": [
        row | {"message": "Some modules have inconsistent states:\n['l10n_cl']"},
        row | {"level": "WARNING", "message": "a warning is no error"}]}
    green["children"][0] |= {"local_result": "ko", "orphan_result": True}
    [up, light] = client.triggers([131519702, 127220311])
    assert (up.verdict, up.children, light.verdict, light.children["done"]) == (
        "red", {"done": 1, "ok": 0, "ko": 1, "killed": 0, "testing": 0}, "green", 1)
    # A migration that fails outside a test reports its ERROR rows, the markdown name made plain.
    assert up.failures == [{
        "error": "Some modules have inconsistent states:", "build_id": 131519739,
        "build_name": "Testing migration from 20.0 to master using db no-demo-all",
        "url": runbot.build_url(131519739)}]
    # An orphan link leaves the tree, runbot's own global_result still makes it red.
    migration["link_orphan_result"] = True
    upgrade["global_result"] = "ko"
    [up] = client.triggers([131519702])
    assert (up.verdict, up.children["done"], up.failures[0]["log"]) == ("red", 0, "unparsed")


def test_style_and_policy_checks(fake, client):
    style, stable = fake.builds[127730874], fake.builds[128071553]
    fake.builds[1] = stable | {"id": 1, "logs": []}
    base = _json("ruff_merge_base_output.json")
    dropped = base.pop()
    fake.files = {
        _static(style, "check_style_ruff-ruff-output.json"): (FIXTURES / "ruff_output.json")
        .read_text(),
        _static(style, "merge_base_check_style_ruff-ruff-output.json"): json.dumps(base),
    }

    style_t, stable_t, bare_t = client.triggers([127730874, 128071553, 1])

    assert len(fake.calls) == 3
    where = {i: {"build_id": i, "build_name": n, "url": runbot.build_url(i)}
             for i, n in ((127730874, "Check Style"), (128071553, "Stable policy check"),
                          (1, "Stable policy check"))}
    # The PR's E902 "No such file" entries are deleted files, only the dropped base entry is new.
    assert style_t.failures == [
        {"rule": dropped["code"],
         "path": dropped["filename"].removeprefix("/data/build/merge_base_"),
         "line": dropped["location"]["row"], "message": dropped["message"], **where[127730874]},
        {"check": "check_semgrep_style", "path": "l10n_ec_edi/models/account_move.py",
         "line": 1116, "message": "commit-in-models: Committing or rolling back transactions should"
         " not be done in models. Ask framework-py. `self.env.cr.commit()`",
         "source_url": "https://github.com/odoo/enterprise/blob/4fb43f4979d12ab5eacb63c528b2bd28409"
         "cb15e/l10n_ec_edi/models/account_move.py#L1116-L1116", **where[127730874]},
    ]
    assert (len(stable_t.failures), stable_t.failures[-1]) == (7, {
        "check": "stable_policy_check", "path": None, "line": None, "source_url": None,
        "message": "Module dependencies changed for l10n_cl_edi_stock_reform. Was [], is"
                   " ['l10n_cl_edi_stock'].", **where[128071553]})
    # A red check that logged nothing has an unknown count, not zero.
    assert bare_t.failures == [{"check": "stable_policy_check", "count": None, **where[1]}]

    # A worker host refusing the ruff output costs that build its failures, and is read again.
    fake.failing[_static(style, "check_style_ruff-ruff-output.json")] = 502
    [style_t] = client.triggers([127730874])
    assert (style_t.failures, style_t.settled) == (
        [{"log": "fetch failed", "status": 502, **where[127730874]}], [])
    fake.files, fake.failing = {}, {}
    assert client.triggers([127730874], {127730874: [{"kept": 1}]})[0].failures == [{"kept": 1}]
    assert client.triggers([127730874])[0].failures == [{"log": "gone", **where[127730874]}]


def test_the_request_cap(tmp_path, client, fake):
    other = db.connect(tmp_path / "c.db")
    assert all(db.claim_runbot_request(other, client.clock(), 30, 150) for _ in range(30))
    with pytest.raises(runbot.CapReached):
        client.triggers([127220311])
    assert fake.calls == []
    for _ in range(4):
        client.clock.advance(hours=1)
        assert all(db.claim_runbot_request(other, client.clock(), 30, 150) for _ in range(30))
    client.clock.advance(hours=1)
    with pytest.raises(runbot.CapReached):
        client.triggers([127220311])
    client.clock.advance(hours=24)
    assert db.claim_runbot_request(other, client.clock(), 30, 150)
    assert other.execute("SELECT count(*) FROM runbot_request").fetchone()[0] == 1


def _seed_bundles(fake) -> dict[tuple[int, str], dict]:
    """Seed the PA EDI bundle, its two batches and their slot builds, by (batch, trigger)."""
    fake.bundles[PA_BUNDLE] = _json("bundle.json")
    builds = {}
    for batch in _json("batches.json"):
        fake.batches[batch["id"]] = batch
        for slot in batch["slots"]:
            if slot["build"]:
                slot["build"]["trigger"] = slot["trigger"]
                builds[batch["id"], slot["trigger"]] = fake.builds[slot["build"]["id"]] = (
                    slot["build"])
    return builds


def test_bundle_walk_and_previous_batch(fake, client):
    builds = _seed_bundles(fake)
    latest, before = 2869143, 2866696
    builds[before, "Check Style"] |= {"global_result": "ok"}
    builds[latest, "CLA"]["local_result"] = "ko"
    builds[before, "CLA"] |= {"global_state": "testing", "global_result": "ok"}
    # A push runbot skipped has no build to show.
    skipped = next(b for b in fake.bundles[PA_BUNDLE][0]["batches"] if b["state"] == "skipped")
    fake.bundles[PA_BUNDLE][0]["batches"].insert(0, skipped)

    walk = client.bundle(PA_BUNDLE)

    roots = ",".join(str(b["id"]) for (i, _), b in builds.items() if i == latest)
    assert fake.calls == [f"api bundle/{PA_BUNDLE}", f"api batch/{latest},{before}",
                          f"api build/{roots}",
                          _static(builds[latest, "Check Style"],
                                  "check_style_ruff-ruff-output.json")]
    assert (walk.bundle, walk.batch_id, len(walk.triggers)) == (PA_BUNDLE, latest, 16)
    assert {t.name: (t.verdict, t.previous) for t in walk.triggers if t.verdict != "green"} == {
        "Check Style": ("red", "green"), "Check Security enterprise": ("red", "red"),
        "CLA": ("red", None)}
    fake.calls = []
    assert client.bundle(PA_BUNDLE, known=walk.key) is None
    assert len(fake.calls) == 2

    with pytest.raises(runbot.NoBundle, match="needs the exact name") as miss:
        client.bundle("l10n_pa_edi")
    assert (miss.value.count, miss.value.candidates) == (0, [])
    fake.bundles["a,b"] = [{"name": "a"}, {"name": "b"}]
    with pytest.raises(runbot.NoBundle) as both:
        client.bundle("a,b")
    assert (both.value.count, both.value.candidates) == (2, ["a", "b"])


def test_batch_names_failing_builds_without_reading_them(fake, client):
    builds = _seed_bundles(fake)
    red = builds[2869143, "Check Style"]
    red["children"] = [{"id": 9, "description": "Child", "relation": "child",
                        "orphan_result": False, "local_state": "done", "local_result": "killed",
                        "children": []}]

    triggers = {t.name: t for t in client.batch(2869143)}

    assert fake.calls == ["api batch/2869143"]
    assert (len(triggers), triggers["Check Style"].failures) == (16, [
        {"build_id": red["id"], "build_name": "Check Style", "url": runbot.build_url(red["id"])},
        {"build_id": 9, "build_name": "Child", "url": runbot.build_url(9)}])
    assert client.batch(1) == []
