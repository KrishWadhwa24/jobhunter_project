import copy
import json

import pytest

from jobhunter import actions, config, db, importer, pipeline
from jobhunter.collectors.base import Collector, FetchResult
from jobhunter.models import Job


class FakeCollector(Collector):
    def __init__(self, jobs, complete=True):
        self.jobs, self.complete = jobs, complete

    def list_jobs(self, company, client):
        return FetchResult(list(self.jobs), complete=self.complete)


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def company():
    return db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "pay_tier": "T1",
                              "careers_url": "https://acme.com/careers", "active": "yes"})


JOBS = [
    Job("1", "Software Engineer (2027 Graduate)", "https://acme/1", "Gurugram, India", "2027 graduates. C++."),
    Job("2", "Senior Software Engineer", "https://acme/2", "Bengaluru, India", "8+ years of experience"),
    Job("3", "Software Engineer II", "https://acme/3", "Pune, India", "3 years of experience"),
    Job("4", "Sales Manager", "https://acme/4", "Mumbai, India", ""),
    Job("5", "Software Engineer", "https://acme/5", "New York, US", ""),
    Job("6", "SDE 1", "https://acme/6", "Hyderabad, India", "Open to 2024-2026 batch graduates only"),
]


def run(monkeypatch, cfg, jobs, complete=True):
    monkeypatch.setattr(pipeline, "collector_for", lambda c: FakeCollector(jobs, complete))
    return pipeline.run_cycle(cfg, force=True, client=object())


def test_cycle_stores_levels_and_finds_matches(monkeypatch, cfg, company):
    stats = run(monkeypatch, cfg, JOBS)
    assert stats.ok == 1 and stats.new_jobs == 4          # 1,2,3,6 are in scope; 4 and 5 are not
    rows = {r["external_id"]: r for r in db.query_jobs([], [], [], only_open=True)}
    assert rows["1"]["verdict"] == "match" and rows["1"]["level"] == "sde1"
    assert rows["2"]["level"] == "sde3"
    assert rows["3"]["level"] == "sde2"
    assert rows["6"]["verdict"] == "reject"                # batch mismatch
    alerts = pipeline.pending_alerts(cfg)
    assert [a["external_id"] for a in alerts] == ["1"]


def test_second_cycle_is_idempotent_and_closes_jobs(monkeypatch, cfg, company):
    run(monkeypatch, cfg, JOBS)
    stats = run(monkeypatch, cfg, JOBS)
    assert stats.new_jobs == 0
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 4
    run(monkeypatch, cfg, JOBS[:1])                        # others disappeared from the feed
    open_ids = {r["external_id"] for r in db.query_jobs([], [], [], only_open=True)}
    assert open_ids == {"1"}


def test_partial_fetch_does_not_close_jobs(monkeypatch, cfg, company):
    run(monkeypatch, cfg, JOBS)
    run(monkeypatch, cfg, JOBS[:1], complete=False)
    assert len(db.query_jobs([], [], [], only_open=True)) == 4


def test_failure_is_recorded_not_raised(monkeypatch, cfg, company):
    def boom(c):
        raise RuntimeError("404")
    monkeypatch.setattr(pipeline, "collector_for", boom)
    stats = pipeline.run_cycle(cfg, force=True, client=object())
    assert stats.errors == 1
    assert db.get_company(company)["last_status"] == "error"


def test_new_targets_via_reevaluate(monkeypatch, cfg, company):
    run(monkeypatch, cfg, JOBS)
    cfg2 = copy.deepcopy(cfg)
    cfg2["targets"]["levels"] = ["sde2", "sde3"]
    r = pipeline.reevaluate_all(cfg2)
    assert r["total"] >= 4
    got = {a["external_id"] for a in pipeline.pending_alerts(cfg2)}
    assert got == {"2", "3"}                                  # alerts follow the new target levels
    assert {a["external_id"] for a in pipeline.pending_alerts(cfg)} == {"1"}


def test_actions_and_events(monkeypatch, cfg, company):
    run(monkeypatch, cfg, JOBS)
    jid = db.query_jobs([], [], [])[0]["id"]
    res = actions.perform(jid, "referrers", cfg)
    assert res["ok"] and "linkedin.com/search" in res["message"] and len(res["pack"]["drafts"][0]["text"]) <= 200
    assert db.get_job(jid)["state"] == "referral"
    actions.perform(jid, "applied", cfg)
    assert db.get_job(jid)["state"] == "applied"
    assert pipeline.pending_alerts(cfg) == [] or all(a["id"] != jid for a in pipeline.pending_alerts(cfg))
    assert len(db.job_events(jid)) == 2


def test_csv_import_upsert():
    n, w = importer.import_csv_text("company,source_type,slug_or_endpoint,active\nAcme,lever,acme,yes\nBeta,ashby,beta,no\n")
    assert n == 2 and not w
    n, _ = importer.import_csv_text("company,source_type,slug_or_endpoint,active\nacme,greenhouse,acme2,yes\n")
    assert len(db.list_companies()) == 2
    assert [c for c in db.list_companies() if c["name"] == "Acme"][0]["source_type"] == "greenhouse"
    with pytest.raises(ValueError):
        importer.import_csv_text("name,x\na,b\n")


def test_real_company_csv_imports():
    n, w = importer.import_csv_file(str(config.ROOT / "data" / "companies.csv"))
    assert n == 205 and not w
    assert any(c["name"] == "Lowes India" for c in db.list_companies())


def test_failed_api_source_falls_back_to_browser(monkeypatch, cfg, company):
    from jobhunter.collectors import browser as browser_mod

    def boom(c):
        class Bad(Collector):
            def list_jobs(self, company, client):
                raise RuntimeError("404 Not Found")
        return Bad()

    monkeypatch.setattr(pipeline, "collector_for", boom)
    monkeypatch.setattr(browser_mod.BrowserCollector, "list_jobs",
                        lambda self, c, cl: FetchResult(list(JOBS[:1]), complete=False, note="read 1 job links"))
    stats = pipeline.run_cycle(cfg, force=True, client=object())
    assert stats.ok == 1 and stats.errors == 0 and stats.new_jobs == 1
    row = db.get_company(company)
    assert row["last_status"] == "ok" and "greenhouse API failed" in row["last_error"]
