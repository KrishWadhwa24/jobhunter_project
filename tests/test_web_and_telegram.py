import pytest
from fastapi.testclient import TestClient

from jobhunter import actions, config, db, pipeline, telegram_bot
from jobhunter.models import Job
from jobhunter.notifier import format_job, job_keyboard
from jobhunter.web.app import create_app
from tests.test_pipeline import FakeCollector, JOBS


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def seeded(monkeypatch, cfg):
    db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "pay_tier": "T1", "active": "yes"})
    monkeypatch.setattr(pipeline, "collector_for", lambda c: FakeCollector(JOBS))
    pipeline.run_cycle(cfg, force=True, client=object())


def test_pages_render(seeded, cfg):
    c = TestClient(create_app(cfg))
    r = c.get("/")
    assert r.status_code == 200 and "Software Engineer (2027 Graduate)" in r.text and "Sales Manager" not in r.text
    assert "Senior Software Engineer" not in r.text                       # not a target level
    r = c.get("/?show=all&levels=sde1,sde2,sde3,staff,intern,unknown")
    assert "Senior Software Engineer" in r.text and "Software Engineer II" in r.text   # other levels still reachable
    assert c.get("/companies").status_code == 200
    assert c.get("/healthz").json() == {"ok": True}
    jid = db.query_jobs([], [], [])[0]["id"]
    assert c.get(f"/job/{jid}").status_code == 200


def test_button_flow_via_web(seeded, cfg):
    c = TestClient(create_app(cfg))
    jid = [r for r in db.query_jobs([], [], []) if r["external_id"] == "1"][0]["id"]
    r = c.post(f"/job/{jid}/action/referrers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("referrers=1")
    page = c.get(r.headers["location"])
    assert "Connection note" in page.text and "linkedin.com/search" in page.text
    c.post(f"/job/{jid}/action/applied", data={"next": "/"})
    assert "Software Engineer (2027 Graduate)" not in c.get("/").text
    assert "Software Engineer (2027 Graduate)" in c.get("/?tab=applied").text


def test_open_redirect_blocked(seeded, cfg):
    c = TestClient(create_app(cfg))
    jid = db.query_jobs([], [], [])[0]["id"]
    r = c.post(f"/job/{jid}/action/skip", data={"next": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_csv_upload(cfg):
    c = TestClient(create_app(cfg))
    r = c.post("/companies/import", files={"file": ("x.csv", b"company,source_type,slug_or_endpoint\nZed,lever,zed\n", "text/csv")},
               follow_redirects=False)
    assert r.status_code == 303 and any(x["name"] == "Zed" for x in db.list_companies())


class FakeTg:
    chat_id = "42"
    def __init__(self): self.sent, self.edits, self.answers = [], [], []
    def send(self, text, keyboard=None, chat_id=None, html_mode=True): self.sent.append((chat_id or self.chat_id, text)); return 1
    def edit(self, mid, text, keyboard=None, chat_id=None): self.edits.append(text)
    def answer(self, cid, text=""): self.answers.append(text)
    def send_job(self, job, base=""): self.sent.append((self.chat_id, format_job(job, base)))


def test_telegram_button_and_security(seeded, cfg):
    tg = FakeTg()
    jid = [r for r in db.query_jobs([], [], []) if r["external_id"] == "1"][0]["id"]
    telegram_bot.handle_update({"callback_query": {"id": "x", "data": f"a:applied:{jid}",
                                                   "message": {"chat": {"id": 999}, "message_id": 5}}}, tg, cfg)
    assert db.get_job(jid)["state"] == "new" and tg.answers == ["Not authorised"]      # stranger is ignored
    telegram_bot.handle_update({"callback_query": {"id": "x", "data": f"a:applied:{jid}",
                                                   "message": {"chat": {"id": 42}, "message_id": 5}}}, tg, cfg)
    assert db.get_job(jid)["state"] == "applied" and "Marked applied" in tg.edits[0]
    telegram_bot.handle_update({"callback_query": {"id": "y", "data": f"a:referrers:{jid}",
                                                   "message": {"chat": {"id": 42}, "message_id": 5}}}, tg, cfg)
    assert any("linkedin.com/search" in t for _, t in tg.sent)
    telegram_bot.handle_update({"message": {"chat": {"id": 7}, "text": "/status"}}, tg, cfg)
    assert len(tg.sent) == 1 or all(c != "7" for c, _ in tg.sent[1:])


def test_message_format_and_keyboard(seeded):
    job = db.query_jobs([], [], ["match"])[0]
    txt = format_job(job, "http://127.0.0.1:8787")
    assert "<b>Software Engineer (2027 Graduate)</b>" in txt and "SDE-1" in txt and "/job/" in txt
    kb = job_keyboard(job["id"])["inline_keyboard"]
    assert [b["text"] for b in kb[0]] == ["Apply", "Find referrers", "Already applied"]
    assert all(len(b["callback_data"]) < 64 for row in kb for b in row)
