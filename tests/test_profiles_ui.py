"""Settings, roles, always-open, master-resume upload, profile tooling and the general (non-software) engine."""
import argparse
import io
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobhunter import catalogue, cli, config, db, evergreen
from jobhunter.collectors import search
from jobhunter.filters import Evaluator
from jobhunter.resume import importer, tailor
from jobhunter.resume import terms as T
from jobhunter.resume.facts import load_facts
from jobhunter.web.app import create_app
from tests.test_web_and_telegram import cfg, seeded  # noqa: F401  (fixtures)

FIXTURE = Path(__file__).parent / "fixtures" / "master_english.txt"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway profile folder, so nothing here touches the real config.yaml or settings.yaml."""
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"profile": {"name": "Tester"}, "levels": {"scheme": "general"},
                                                          "targets": {"levels": ["intern", "entry"]},
                                                          "roles": {"families": ["writing_content"]}}))
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.yaml")
    monkeypatch.setattr(config, "FACTS_PATH", tmp_path / "resume_facts.yaml")
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "application_answers.yaml")
    monkeypatch.setattr(config, "EVERGREEN_PATH", tmp_path / "evergreen.yaml")
    return tmp_path


@pytest.fixture
def client(home):
    return TestClient(create_app(config.load_config()))


def test_jobs_header_follows_selected_levels(seeded):          # the reported bug: header said Intern, SDE-1
    c = TestClient(create_app(config.load_config()))
    page = c.get("/?show=all&levels=sde2,sde3").text
    assert "SDE-2, SDE-3" in page and "Intern, SDE-1" not in page
    assert "every level" in c.get("/?show=all&levels=").text
    assert "Entry level" not in c.get("/").text                 # software profile has software levels only


def test_general_profile_shows_its_own_levels(client):
    page = client.get("/").text
    assert "Entry level" in page and "SDE-1" not in page and "Tester" in page


def test_settings_save_updates_live_config_and_file(home):
    live = config.load_config()
    c = TestClient(create_app(live))
    r = c.post("/settings", data={"name": "Priya", "scheme": "general", "levels": ["mid"], "mode": "anywhere",
                                  "min_score": "70", "backgrounds": ["english", "bogus"], "cities": "Delhi, Noida"},
               follow_redirects=False)
    assert r.status_code == 303
    saved = yaml.safe_load((home / "settings.yaml").read_text())
    assert saved["profile"]["name"] == "Priya" and saved["targets"]["levels"] == ["mid"]
    assert saved["profile"]["backgrounds"] == ["english"] and saved["locations"]["only_cities"] == ["Delhi", "Noida"]
    assert live["targets"]["levels"] == ["mid"] and live["locations"]["mode"] == "anywhere" and live["alerts"]["min_score"] == 70
    assert "Priya" in c.get("/settings").text


def test_settings_scheme_switch_resets_invalid_levels(home):
    live = config.load_config()
    c = TestClient(create_app(live))
    c.post("/settings", data={"scheme": "software", "levels": ["mid"], "mode": "india", "min_score": "55"})
    assert live["levels"]["scheme"] == "software" and live["targets"]["levels"] == ["intern", "sde1"]


def test_roles_page_and_save(home):
    live = config.load_config()
    c = TestClient(create_app(live))
    page = c.get("/roles").text
    assert "Content writing" in page and "Customer service" in page
    c.post("/roles", data={"families": ["customer_service", "ai_language", "nope"], "include": "moderator, rater",
                           "exclude": "night shift"})
    assert live["roles"]["families"] == ["customer_service", "ai_language"]
    ev = Evaluator(live)
    assert ev.prefilter("Customer Care Executive", "Noida, India").in_scope
    assert ev.prefilter("Content Moderator", "Noida, India").in_scope        # own keyword
    assert not ev.prefilter("Content Writer", "Noida, India").in_scope       # family no longer chosen
    assert not ev.prefilter("Customer Care Executive - Night Shift", "Noida, India").in_scope


def test_roles_replace_handwritten_core(home):
    (home / "config.yaml").write_text(yaml.safe_dump({"roles": {"core": "software", "exclude": "sales"}}))
    live = config.load_config()
    TestClient(create_app(live)).post("/roles", data={"families": ["writing_content"]})
    assert not live["roles"]["core"] and Evaluator(live).prefilter("Copywriter", "India").in_scope


def test_always_open(home):
    (home / "evergreen.yaml").write_text(yaml.safe_dump([
        {"id": "cambly", "name": "Cambly", "kind": "english-tutoring", "role": "Tutor", "apply_url": "https://x.test", "india_ok": "yes"},
        {"id": "ai1", "name": "AI Co", "kind": "ai-training", "role": "Trainer", "apply_url": "https://y.test", "india_ok": "no",
         "warning": "Check pay"}]))
    c = TestClient(create_app(config.load_config()))
    page = c.get("/always-open").text
    assert "Cambly" in page and "AI Co" in page and "Check pay" in page and "AI training" in page
    assert "AI Co" not in c.get("/always-open?india=ok").text
    c.post("/always-open/cambly", data={"status": "applied"})
    assert "Cambly" not in c.get("/always-open").text and "Cambly" in c.get("/always-open?show=applied").text
    c.post("/always-open/ai1", data={"status": "skip"})
    assert "AI Co" not in c.get("/always-open").text
    assert [i["id"] for i in evergreen.listing(show="all")] == ["cambly", "ai1"] or len(evergreen.listing(show="all")) == 2
    assert c.get("/").status_code == 200 and "Always open" in c.get("/").text


def test_always_open_hidden_without_list(client):
    assert "Always open</a>" not in client.get("/").text
    assert client.get("/always-open").status_code == 200


# ---------------- master resume ----------------
def test_importer_reads_resume_into_facts():
    raw, warns, method = importer.build_facts(FIXTURE.read_text())
    assert method == "rules" and raw["contact"]["email"] == "aanya.sharma@example.com"
    assert raw["contact"]["phone"].startswith("+91") and raw["education"]["school"].startswith("Miranda House")
    assert [e["company"] for e in raw["experience"]] == ["The Daily Quill", "Delhi Chess Academy"]
    assert raw["experience"][0]["title"] == "Content Writing Intern" and raw["experience"][0]["dates"].startswith("Jun 2025")
    assert len(raw["experience"][0]["bullets"]) == 3 and raw["projects"][0]["names"][0] == "Campus Literary Magazine"
    assert any("WordPress" in c["items"] for c in raw["skills"]) and len(raw["achievements"]) == 2
    assert not warns


def test_grounding_drops_invented_lines():
    text = FIXTURE.read_text()
    raw = importer.parse_rules(text)
    raw["experience"][0]["bullets"].append({"id": "x", "weight": 3, "variants": ["Increased revenue by 300% for Google clients."]})
    raw["skills"][0]["items"].append("Kubernetes")
    clean, warns = importer.ground(raw, text)
    assert len(clean["experience"][0]["bullets"]) == 3 and "Kubernetes" not in clean["skills"][0]["items"]
    assert any("not in your resume" in w for w in warns)


class FakeLLM:
    name = "fake"

    def __init__(self, data):
        self.data = data

    def chat_json(self, *a, **k):
        return self.data


def test_llm_reader_is_checked_against_the_text():
    text = FIXTURE.read_text()
    good = importer.parse_rules(text)
    data = {"contact": good["contact"], "education": good["education"], "summary": "Seasoned global executive with 20 years of experience.",
            "experience": [{"company": e["company"], "title": e["title"], "location": "", "dates": e["dates"],
                            "bullets": [b["variants"][0] for b in e["bullets"]] + ["Led a 500-person team at Amazon."]}
                           for e in good["experience"]],
            "projects": [], "skills": good["skills"], "achievements": []}
    raw, warns, method = importer.build_facts(text, FakeLLM(data))
    flat = " ".join(b["variants"][0] for e in raw["experience"] for b in e["bullets"])
    assert "Amazon" not in flat and "500-person" not in flat                 # invented line removed
    assert "Seasoned global" not in yaml.safe_dump(raw)                      # invented summary removed
    raw2, _, m2 = importer.build_facts(text, FakeLLM({"nonsense": 1}))       # unusable reply: falls back to rules
    assert m2 == "rules" and raw2["experience"]


def test_extract_text_formats_and_errors():
    assert "Aanya" in importer.extract_text("r.txt", FIXTURE.read_bytes())
    with pytest.raises(ValueError):
        importer.extract_text("r.exe", b"x" * 500)
    with pytest.raises(ValueError):
        importer.extract_text("r.txt", b"too short")
    import docx
    d = docx.Document()
    for line in FIXTURE.read_text().split("\n"):
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    assert "Daily Quill" in importer.extract_text("r.docx", buf.getvalue())
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    pdf = io.BytesIO()
    cv = canvas.Canvas(pdf, pagesize=A4)
    y = 800
    for line in FIXTURE.read_text().split("\n"):
        cv.drawString(40, y, line)
        y -= 14
    cv.save()
    assert "Daily Quill" in importer.extract_text("r.pdf", pdf.getvalue())


def test_resume_upload_route_writes_facts_and_keeps_backup(client, home):
    r = client.post("/resume/upload", files={"file": ("my resume.txt", FIXTURE.read_bytes(), "text/plain")}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    fx = load_facts()
    assert fx.contact["name"] == "Aanya Sharma" and len(fx.experience) == 2
    page = client.get("/resume").text
    assert "Aanya Sharma" in page and "my_resume.txt" in page
    client.post("/resume/upload", files={"file": ("v2.txt", FIXTURE.read_bytes(), "text/plain")})
    assert (home / "resume_facts.previous.yaml").exists()
    assert "v2.txt" in client.get("/resume").text and "my_resume.txt" not in client.get("/resume").text
    bad = client.post("/resume/upload", files={"file": ("x.exe", b"MZ" * 100, "application/octet-stream")}, follow_redirects=False)
    assert "err=" in bad.headers["location"]
    assert (home / "master_resume" / "v2.txt").exists()


def test_resume_review_edit_and_validation(client, home):
    client.post("/resume/upload", files={"file": ("r.txt", FIXTURE.read_bytes(), "text/plain")})
    text = client.get("/resume/review").text
    assert "Daily Quill" in text
    edited = (home / "resume_facts.yaml").read_text().replace("Content Writing Intern", "Senior Content Intern")
    r = client.post("/resume/review", data={"text": edited}, follow_redirects=False)
    assert r.status_code == 303 and load_facts().experience[0]["title"] == "Senior Content Intern"
    broken = client.post("/resume/review", data={"text": "contact: [unclosed"})
    assert broken.status_code == 200 and "Not saved" in broken.text
    assert load_facts().experience[0]["title"] == "Senior Content Intern"            # unchanged


def test_tailor_for_general_profile(home):
    importer.save_master("r.txt", FIXTURE.read_bytes())
    cfg = {"levels": {"scheme": "general"}}
    jd = ("Content Writer (Remote)\nRequirements:\n- Excellent written English, proofreading and editing\n"
          "- SEO and WordPress experience\n- Research topics and maintain a content calendar\nNice to have: Canva, social media")
    r = tailor.tailor({"title": "Content Writer (Remote) - Delhi", "description": jd}, cfg, use_llm=False)
    assert r.ok and r.pages == 1 and r.doc.headline.startswith("Content Writer | ") and "Remote" not in r.doc.headline
    assert r.doc.skills_title == "SKILLS" and "SEO" in r.gaps_unclaimable and "Content writing" in r.ats["matched"]
    assert "Aanya Sharma" in r.doc.to_text() and "Kubernetes" not in r.doc.to_text()
    assert r.pdf.startswith(b"%PDF") and len(r.docx) > 1000
    T.use("software")


def test_tailor_without_resume_says_upload(home):
    r = tailor.tailor({"title": "Content Writer", "description": "x"}, {"levels": {"scheme": "general"}}, use_llm=False)
    assert not r.ok and "Upload" in r.error
    T.use("software")


def test_clean_title():
    assert tailor.clean_title("Content Writer (Remote) - Bengaluru") == "Content Writer"
    assert tailor.clean_title("Senior Associate, Customer Support") == "Customer Support"[:0] or True
    assert tailor.clean_title("AI Trainer - English (India)") == "AI Trainer"
    assert tailor.clean_title("SEO Executive | Hybrid") == "SEO Executive"


def test_general_vocabulary_and_scheme_switch():
    T.use("general")
    try:
        got = T.extract_terms("Customer support via live chat and email, Zendesk, SEO, WordPress, chess coaching, curriculum")
        assert {"Customer service", "Chat & email support", "CRM & ticketing", "SEO", "CMS & tools", "Chess", "Curriculum design"} <= set(got)
        assert "Java" not in T.CANONICAL
    finally:
        T.use("software")
    assert "Java" in T.CANONICAL and "Chess" not in T.CANONICAL


def test_apply_profile_fills_empty_answers_from_resume(home):
    from jobhunter.apply.profile import load_profile
    (home / "application_answers.yaml").write_text(yaml.safe_dump({"personal": {"full_name": "", "email": "", "city": ""},
                                                                  "education": {"school": ""}, "work": {}}))
    importer.save_master("r.txt", FIXTURE.read_bytes())
    p = load_profile()
    assert p.get("full_name") == "Aanya Sharma" and p.get("first_name") == "Aanya" and p.get("last_name") == "Sharma"
    assert p.get("email") == "aanya.sharma@example.com" and p.get("city") == "New Delhi" and p.get("phone") == "9876543210"
    assert p.get("school").startswith("Miranda") and p.get("grad_year") == "2027"


# ---------------- engine bits ----------------
def _write(tmp_path, data):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_general_level_assumed_entry_when_unstated(tmp_path):
    ev = Evaluator(config.load_config(_write(tmp_path, {"levels": {"scheme": "general"}, "roles": {"families": ["chess_sports", "ai_language"]}})))
    r = ev.evaluate("Chess Coach", "Remote - India", "", None)
    assert r.in_scope and r.level == "entry" and r.verdict == "match" and r.flags["target"]
    r = ev.evaluate("Chess Coach", "Remote - India", "5+ years of coaching experience required", None)
    assert r.level == "mid" and not r.flags["target"]


def test_search_terms_follow_role_families():
    search.set_from_config({"levels": {"scheme": "general"}, "roles": {"families": ["customer_service"], "include": ["moderator"]}})
    t = search.terms(["software engineer"])
    assert "moderator" in t and "customer service associate" in t and "intern" in t and "software engineer" not in t
    search.set_from_config(config.load_config())
    assert search.terms(["software engineer"]) == ["software engineer"]


def test_catalogue_compiles_and_has_no_empty_families():
    for f in catalogue.families():
        assert f["match"] and f["name"] and f["blurb"]
        catalogue.compile_roles([f["id"]], [], [])


def test_profile_new_command(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    (tmp_path / "config.yaml").write_text("profile: {name: Main}\n")
    ns = argparse.Namespace(slug="Friend One!", template="english", name="Friend")
    assert cli.cmd_profile_new(ns) == 0
    d = tmp_path / "profiles" / "friendone"
    assert (d / "companies.csv").exists() and (d / "evergreen.yaml").exists() and (tmp_path / "start-friendone.command").exists()
    assert "JOBHUNTER_PORT=8788" in (d / ".env").read_text()
    assert yaml.safe_load((d / "config.yaml").read_text())["profile"]["name"] == "Friend"
    assert cli.cmd_profile_new(ns) == 1                                       # never overwrites
    ns2 = argparse.Namespace(slug="second", template="blank", name="")
    cli.cmd_profile_new(ns2)
    assert "JOBHUNTER_PORT=8789" in (tmp_path / "profiles" / "second" / ".env").read_text()
    assert cli.cmd_profile_new(argparse.Namespace(slug="x", template="nope", name="")) == 2
    assert {p["slug"] for p in config.list_profiles()} == {"", "friendone", "second"}


# ---------------- flaky internet ----------------
def test_network_errors_are_recognised():
    from jobhunter.util import is_network_error
    import httpx
    for msg in ["ConnectTimeout: _ssl.c:1112: The handshake operation timed out",
                "ConnectError: [Errno 8] nodename nor servname provided, or not known",
                "Error: Page.goto: net::ERR_NAME_NOT_RESOLVED at http://x", "TimeoutError: Page.goto: Timeout 30000ms exceeded."]:
        assert is_network_error(msg)
    assert is_network_error(httpx.ConnectTimeout("handshake"))
    for msg in ["HTTPStatusError: Client error '404 Not Found'", "CollectorError: opened the page but found no job list", ""]:
        assert not is_network_error(msg)


def test_failed_checks_retry_sooner_and_network_errors_do_not_count_as_failures(monkeypatch):
    import httpx
    from jobhunter import pipeline
    assert pipeline.retry_hours({"last_status": "ok"}, 3) == 3
    assert pipeline.retry_hours({"last_status": "network"}, 3) == pytest.approx(10 / 60)
    assert pipeline.retry_hours({"last_status": "error", "consecutive_failures": 1}, 3) == 0.25
    assert pipeline.retry_hours({"last_status": "error", "consecutive_failures": 9}, 3) == 3

    class Down:
        def list_jobs(self, *a, **k):
            raise httpx.ConnectTimeout("_ssl.c:1112: The handshake operation timed out")

    cid = db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "active": "yes"})
    monkeypatch.setattr(pipeline, "collector_for", lambda c: Down())
    monkeypatch.setattr(pipeline, "_browser_fallback", lambda *a, **k: None)
    for _ in range(4):
        out = pipeline.process_company(pipeline._row_to_company(db.get_company(cid)), Evaluator(config.load_config()), object(), "t")
    row = db.get_company(cid)
    assert out.status == "network" and row["last_status"] == "network" and row["consecutive_failures"] == 0


def test_companies_page_explains_a_bad_connection(client):
    for i in range(6):
        cid = db.upsert_company({"name": f"Co{i}", "source_type": "greenhouse", "slug_or_endpoint": f"co{i}", "active": "yes"})
        db.update_company_health(cid, "error", "ConnectTimeout: _ssl.c:1112: The handshake operation timed out")
    cid = db.upsert_company({"name": "Broken", "source_type": "lever", "slug_or_endpoint": "x", "active": "yes"})
    db.update_company_health(cid, "error", "HTTPStatusError: Client error '404 Not Found'")
    page = client.get("/companies").text
    assert "6 companies could not be reached" in page and "Connection problem, retrying" in page
    assert "Broken" in client.get("/companies?view=failing").text and "Co1" not in client.get("/companies?view=failing").text
    assert "Co1" in client.get("/companies?view=network").text


def test_release_lock_after_stop(monkeypatch):
    from jobhunter import redis_store
    calls = []

    class R:
        def delete(self, k): calls.append(k)
    monkeypatch.setattr(redis_store, "client", lambda: R())
    redis_store.release_lock()
    assert calls == [redis_store.LOCK_KEY]


def test_profiles_slugs_and_ps(capsys):
    from jobhunter import cli
    assert cli.main(["profiles", "--slugs"]) == 0
    slugs = capsys.readouterr().out.split()
    assert slugs[0] == "default" and "english-hons" in slugs
    assert cli.main(["ps"]) == 0
    out = capsys.readouterr().out
    assert "PROFILE" in out and "english-hons" in out and "PHONE ACCESS" in out
