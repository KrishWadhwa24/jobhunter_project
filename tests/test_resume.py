import io
import json

import pytest

from jobhunter import config, db
from jobhunter.resume import ats, guard, jd as jdmod, render, tailor
from jobhunter.resume import terms as T
from jobhunter.resume.facts import load_facts

BACKEND_JD = """About us
We are Acme.

Responsibilities
- Build REST APIs in Java and Spring Boot
- Work with Kafka event-driven pipelines

Requirements:
- Strong knowledge of data structures and algorithms
- Experience with PostgreSQL or MySQL, Redis, Docker
- Unit testing

Nice to have:
- Kubernetes, AWS
""" + ("We value ownership and curiosity. " * 8)

GO_JD = """Responsibilities
- Build low-latency trading backends in Go with PostgreSQL and Redis
Requirements:
- Golang, REST APIs, Docker, WebSockets, real-time alerting systems
"""

FRONTEND_JD = """Full-Stack Developer
Requirements:
- Angular, TypeScript, HTML, CSS, REST APIs, Java, Spring Boot
""" + ("Join a friendly product team. " * 10)


class FakeLLM:
    name = "Fake"
    model = "fake"

    def __init__(self, reply):
        self.reply = reply

    def chat_json(self, system, user, **kw):
        return self.reply(user) if callable(self.reply) else self.reply

    def chat(self, *a, **k):
        return "ok"


@pytest.fixture(scope="module")
def facts():
    return load_facts()


def run(job_title, text, facts, **kw):
    return tailor.tailor({"title": job_title, "description": text}, {}, facts=facts, use_llm=False, **kw)


def test_facts_file_loads_and_is_consistent(facts):
    assert facts.contact["phone"] == "+91 7017111529" and facts.contact["email"] == "krishwadhwa2005@gmail.com"
    stack_text = " ".join(" ".join(b.variants) for b in facts.experience[0]["bullets"])
    assert "Spring Boot" in stack_text and "Express" not in stack_text      # MoveInSync APIs are Java/Spring Boot
    assert "Azure Table Storage" in stack_text and "Angular" in stack_text


def test_builds_one_page_pdf_and_docx_with_readable_text(facts):
    r = run("Software Engineer - Backend", BACKEND_JD, facts)
    assert r.ok and r.pages == 1
    text = render.pdf_text(r.pdf)
    for must in ("SUMMARY", "EXPERIENCE", "TECHNICAL SKILLS", "EDUCATION", "krishwadhwa2005@gmail.com", "7017111529"):
        assert must in text
    assert r.docx[:2] == b"PK"


def test_nothing_is_invented(facts):
    for title, jd in (("Backend Engineer", BACKEND_JD), ("Go Engineer", GO_JD), ("Full-Stack Developer", FRONTEND_JD)):
        r = run(title, jd, facts)
        d = r.doc
        content = [d.summary] + [b for e in d.experience + d.projects for b in e.bullets] + d.achievements \
            + [e.heading for e in d.projects] + [i for _, items in d.skills for i in items]
        problems = guard.violations("\n".join(content), facts.corpus)
        assert problems == [], problems
        assert set(i for _, items in d.skills for i in items) <= set(facts.all_skill_items())


def test_gaps_are_reported_not_claimed(facts):
    r = run("Software Engineer - Backend", BACKEND_JD, facts)
    assert "Kubernetes" in r.gaps_unclaimable and "AWS" in r.gaps_unclaimable
    assert "kubernetes" not in r.doc.to_text().lower()
    assert 90 <= r.score < 100


def test_target_reached_when_the_job_matches_your_stack(facts):
    r = run("Backend Engineer", "Requirements:\n- Java, Spring Boot, Kafka, PostgreSQL, Redis, REST APIs, Docker\n" + "x " * 150, facts)
    assert r.target_reached and r.score >= 95


def test_content_follows_the_job(facts):
    go = run("Backend Engineer (Go)", GO_JD, facts)
    assert go.doc.projects[0].heading.startswith("TradeNexus")
    fs = run("Full-Stack Developer", FRONTEND_JD, facts)
    assert "Full-Stack" in fs.doc.headline
    assert any("Angular" in b for e in fs.doc.experience for b in e.bullets)


def test_term_reader():
    t = T.extract_terms("Java and JavaScript, Go, Node.js; Go to market. Springboot, k8s, CI/CD, Postgres")
    assert {"Java", "JavaScript", "Golang", "Node.js", "Spring Boot", "Kubernetes", "CI/CD", "PostgreSQL"} <= set(t)
    assert "Java" in T.extract_terms("Java developer") and "Java" not in T.extract_terms("JavaScript developer")
    assert "Golang" not in T.extract_terms("Go to market strategy")


def test_jd_weights_required_over_preferred():
    j = jdmod.analyze_rules(BACKEND_JD, "Software Engineer - Backend")
    assert j.weights["PostgreSQL"] > j.weights["Kubernetes"] and j.weights["Kubernetes"] == 1.0


def test_guard_blocks_new_claims():
    old = "Built a FastAPI service using Redis to match buyers within 20km."
    assert guard.violations("Developed a FastAPI service using Redis, matching buyers within 20km.", old) == []
    bad = guard.violations("Built a FastAPI service on Kubernetes serving 5000 users at Google.", old)
    assert any("Kubernetes" in p for p in bad) and any("5000" in p for p in bad) and any("Google" in p for p in bad)


def test_good_llm_rewrite_is_used_and_bad_one_is_rejected(facts):
    def good(user):
        data = json.loads(user.split("(JSON id -> text):\n")[1].split("\n\nReturn JSON")[0])
        key = "exp:0:api"
        return {key: data[key].replace("Designed and developed", "Engineered")}

    r = tailor.tailor({"title": "Backend Engineer", "description": BACKEND_JD}, {}, facts=facts, use_llm=True,
                      analyzer=None, rewriter=FakeLLM(good))
    assert r.llm["rewrite"]["accepted"] == 1
    assert any(b.startswith("Engineered 2 production-grade REST APIs") for e in r.doc.experience for b in e.bullets)

    def evil(user):
        data = json.loads(user.split("(JSON id -> text):\n")[1].split("\n\nReturn JSON")[0])
        return {k: v + " Deployed on Kubernetes and AWS for 9000 users." for k, v in data.items()}

    r2 = tailor.tailor({"title": "Backend Engineer", "description": BACKEND_JD}, {}, facts=facts, use_llm=True,
                       analyzer=None, rewriter=FakeLLM(evil))
    assert r2.llm["rewrite"]["accepted"] == 0 and r2.llm["rewrite"]["rejected"] > 0
    assert "kubernetes" not in r2.doc.to_text().lower() and "9000" not in r2.doc.to_text()


def test_llm_failure_falls_back_to_rules(facts):
    class Broken(FakeLLM):
        def chat_json(self, *a, **k):
            raise RuntimeError("rate limited")

    r = tailor.tailor({"title": "Backend Engineer", "description": BACKEND_JD}, {}, facts=facts, use_llm=True,
                      analyzer=Broken(None), rewriter=Broken(None))
    assert r.ok and r.score > 90


def test_ats_score_parts():
    j = jdmod.analyze_rules("Requirements: Java, Kafka", "Backend Engineer")
    full = ats.score("SUMMARY EXPERIENCE EDUCATION TECHNICAL SKILLS a@b.co 7017111529 2023 2027 2026 2025 Java Kafka Backend Engineer",
                     j, pages=1)
    assert full.coverage == 1.0 and full.score > 95
    none = ats.score("SUMMARY EXPERIENCE EDUCATION TECHNICAL SKILLS", j, pages=2)
    assert none.coverage == 0.0 and none.score < 30


def test_web_card_and_downloads(tmp_path, monkeypatch, facts):
    from fastapi.testclient import TestClient
    from jobhunter.web.app import create_app
    cid = db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "active": "yes"})
    with db.connect() as c:
        jid = c.insert("INSERT INTO jobs(company_id, external_id, title, url, first_seen_at, last_seen_at, in_scope, level, verdict, score) "
                       "VALUES(?,?,?,?,?,?,1,'sde1','match',80)", (cid, "1", "Software Engineer", "https://acme/1", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"))
    r = tailor.tailor({"title": "Software Engineer", "description": BACKEND_JD}, {}, facts=facts, use_llm=False)
    pdf, docx = tmp_path / "r.pdf", tmp_path / "r.docx"
    pdf.write_bytes(r.pdf); docx.write_bytes(r.docx)
    rid = db.resume_start(jid)
    db.resume_finish(rid, "ready", r.score, r.target_reached, {"ats": r.ats, "notes": r.notes}, str(pdf), str(docx))
    c = TestClient(create_app(config.load_config()))
    page = c.get(f"/job/{jid}").text
    assert "Tailored resume" in page and "Download PDF" in page and f"{r.score:.1f}" in page
    assert c.get(f"/job/{jid}/resume.pdf").content[:4] == b"%PDF"
    assert c.get(f"/job/{jid}/resume.docx").content[:2] == b"PK"
    assert c.post(f"/job/{jid}/tailor", follow_redirects=False).status_code == 303
