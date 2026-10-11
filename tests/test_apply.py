import re
from pathlib import Path

import pytest

from jobhunter.apply import matching, page as apage
from jobhunter.apply.matching import Question, resolve
from jobhunter.apply.profile import load_profile

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def prof():
    return load_profile()


def q(label, kind="text", options=None, required=True, **kw):
    return Question(id="x", kind=kind, label=label, options=[{"label": o, "id": str(i)} for i, o in enumerate(options or [])],
                    required=required, **kw)


def test_basic_fields(prof):
    assert resolve(q("First Name *"), prof).value == "Krish"
    assert resolve(q("Last name"), prof).value == "Wadhwa"
    assert resolve(q("Full Name"), prof).value == "Krish Wadhwa"
    assert resolve(q("Email address"), prof).value == "krishwadhwa2005@gmail.com"
    assert resolve(q("Mobile number"), prof).value == "7017111529"
    assert "linkedin.com/in/" in resolve(q("LinkedIn URL"), prof).value
    assert resolve(q("Company name"), prof).action == "ask"                 # not mistaken for the candidate's name
    assert resolve(q("Expected CTC (in LPA)"), prof).value.startswith("As per")


def test_unknowns_and_money(prof):
    a = resolve(q("Current CTC"), prof)
    assert a.action == "ask"                                                  # never guessed
    assert resolve(q("Current CTC", required=False), prof).action == "skip"
    assert resolve(q("Why do you want to work here?", "textarea"), prof).action == "ask"
    assert resolve(q("Expected CTC", input_type="number"), prof).action == "ask"   # text answer, numeric box


def test_options(prof):
    a = resolve(q("Legally authorized to work in India?", "radio", ["Yes", "No"]), prof)
    assert (a.action, a.value) == ("choose", "Yes")
    a = resolve(q("Will you require visa sponsorship?", "radio", ["Yes", "No"]), prof)
    assert a.value == "No"
    a = resolve(q("Degree", "select", ["Select...", "High School", "Bachelor's Degree"]), prof)
    assert a.value == "Bachelor's Degree"
    a = resolve(q("How did you hear about us?", "select", ["Please select", "LinkedIn", "Company careers page"]), prof)
    assert a.value == "Company careers page"
    a = resolve(q("Country", "select", ["Select", "United States", "India"]), prof)
    assert a.value == "India"
    assert resolve(q("Willing to relocate?", "radio", ["Yes", "No"]), prof).value == "Yes"
    assert resolve(q("Are you at least 18 years of age?", "radio", ["Yes", "No"]), prof).value == "Yes"


def test_demographics_and_consent(prof):
    a = resolve(q("Gender", "select", ["Male", "Female", "Decline to self identify"], required=False), prof)
    assert a.action == "choose" and a.value.startswith("Decline")
    assert resolve(q("Race", "select", ["A", "B"], required=False), prof).action == "skip"
    assert resolve(q("Veteran status", "select", ["Yes", "No"]), prof).action == "ask"      # required, no decline option
    c = resolve(q("I agree to the privacy policy and terms", "checkbox"), prof)
    assert c.action == "ask" and "consent" in c.note
    assert resolve(q("I agree to the privacy policy", "checkbox", required=False), prof).action == "skip"


def test_custom_rule_wins(prof):
    assert resolve(q("Preferred work location"), prof).value == "Bengaluru"


def test_files(prof):
    assert resolve(q("Resume/CV", "file"), prof).action == "upload"
    assert resolve(q("Cover letter", "file", required=False), prof).action == "cover_upload"
    assert resolve(q("Transcript", "file"), prof).action == "ask"


def test_entry_urls():
    assert apage.entry_url_guess("https://jobs.lever.co/acme/123?lever-source=x") == "https://jobs.lever.co/acme/123/apply"
    assert apage.entry_url_guess("https://jobs.ashbyhq.com/acme/abc") == "https://jobs.ashbyhq.com/acme/abc/application"
    assert apage.entry_url_guess("https://careers.acme.com/job/1") == ""


def test_module_never_submits():
    src = Path(apage.__file__).read_text()
    clicks = re.findall(r"\.click\(([^)]*)\)", src)
    assert all("submit" not in c.lower() for c in clicks)
    assert apage.FORBIDDEN_CLICK.search("Submit application") and apage.FORBIDDEN_CLICK.search("Next")


# ---- real browser, local fixture pages ----
@pytest.fixture(scope="module")
def pw_page():
    pw_mod = pytest.importorskip("playwright.sync_api")
    pw = pw_mod.sync_playwright().start()
    try:
        import os
        kw = {}
        path = os.environ.get("JOBHUNTER_CHROME_PATH", "")
        if path:
            kw["executable_path"] = path
        b = pw.chromium.launch(headless=True, args=["--no-sandbox"], **kw)
    except Exception as e:
        pw.stop()
        pytest.skip(f"no browser: {e}")
    ctx = b.new_context()
    yield ctx
    ctx.close(); b.close(); pw.stop()


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "Krish_Wadhwa_Acme.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return str(p)


def test_fills_basic_form(pw_page, prof, pdf):
    page = pw_page.new_page()
    rep = apage.run_on_page(page, (FIX / "form_basic.html").as_uri(), prof, pdf)
    assert rep["fields_seen"] >= 8 and rep["resume_uploaded"]
    assert page.input_value("#fn") == "Krish" and page.input_value("#em") == "krishwadhwa2005@gmail.com"
    assert page.input_value("#ph") == "7017111529"
    assert page.eval_on_selector("#deg", "e => e.options[e.selectedIndex].text") == "Bachelor's Degree"
    assert page.eval_on_selector("#src", "e => e.options[e.selectedIndex].text") == "Company careers page"
    assert page.eval_on_selector("#g", "e => e.options[e.selectedIndex].text").startswith("Decline")
    assert page.is_checked("input[name=auth][value=y]") and page.is_checked("input[name=spons][value=n]")
    assert page.eval_on_selector("#rs", "e => e.files.length") == 1
    # what is left for the human: unknown required fields and the consent box
    needs = " | ".join(n["label"] for n in rep["needs"]).lower()
    assert "current ctc" in needs and "why do you want" in needs and "privacy" in needs
    assert rep["status"] == "needs_you"
    assert not page.is_checked("input[name=consent]")           # consent is never ticked
    assert page.title() != "SUBMITTED"                           # and nothing was submitted
    page.close()


def test_apply_button_leads_to_form(pw_page, prof, pdf):
    page = pw_page.new_page()
    rep = apage.run_on_page(page, (FIX / "job_page.html").as_uri(), prof, pdf)
    assert rep["fields_seen"] >= 8 and page.input_value("#fn") == "Krish"
    page.close()


def test_combobox(pw_page, prof, pdf):
    page = pw_page.new_page()
    rep = apage.run_on_page(page, (FIX / "form_combo.html").as_uri(), prof, pdf)
    assert page.input_value("#country") == "India", rep
    assert rep["status"] == "ready", rep
    page.close()


def test_no_form(pw_page, prof, pdf, tmp_path):
    f = tmp_path / "nothing.html"
    f.write_text("<html><body><h1>Nope</h1><p>Closed</p></body></html>")
    page = pw_page.new_page()
    rep = apage.run_on_page(page, f.as_uri(), prof, pdf)
    assert rep["status"] == "failed" and "Could not find" in " ".join(rep["notes"])
    page.close()


def test_worker_end_to_end(monkeypatch, tmp_path, pdf):
    """Apply -> resume ready -> form filled in the apply window -> report stored -> Telegram told."""
    import os
    from jobhunter import config, db, notifier
    from jobhunter.apply import browser, worker
    from jobhunter.resume import runner
    monkeypatch.setenv("JOBHUNTER_APPLY_HEADLESS", "1")
    monkeypatch.setenv("JOBHUNTER_NO_SANDBOX", "1")
    if os.environ.get("JOBHUNTER_CHROME_PATH"):
        monkeypatch.setattr(browser, "MAC_BROWSERS", [])
    pytest.importorskip("playwright.sync_api")
    db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "pay_tier": "T1", "active": "yes"})
    cid = db.list_companies()[0]["id"]
    with db.connect() as c:
        jid = c.insert("INSERT INTO jobs(company_id, external_id, title, url, location, first_seen_at, last_seen_at) "
                       "VALUES(?,?,?,?,?,?,?)", (cid, "1", "SDE 1", (FIX / "form_basic.html").as_uri(), "Bengaluru", "now", "now"))
    monkeypatch.setattr(runner, "ensure_resume", lambda job_id, cfg: {"pdf_path": pdf, "status": "ready"})
    sent = []

    class FakeTg:
        configured = True
        def send(self, text, keyboard=None, **kw): sent.append((text, keyboard))
    monkeypatch.setattr(notifier, "from_env", lambda: FakeTg())
    app_id = db.application_start(jid)
    try:
        worker.process(jid, app_id, {})
    finally:
        browser._browser._reset()
    app = db.get_application(jid)
    assert app["status"] == "needs_you" and app["report"]["resume_uploaded"]
    assert any("Needs you" in t and "Current CTC" in t for t, _ in sent)
    assert sent[0][1]["inline_keyboard"][0][0]["callback_data"] == f"a:applied:{jid}"


def test_form_inside_iframe(pw_page, prof, pdf):
    page = pw_page.new_page()
    rep = apage.run_on_page(page, (FIX / "job_iframe.html").as_uri(), prof, pdf)
    assert rep["fields_seen"] >= 8 and rep["resume_uploaded"], rep
    fr = [f for f in page.frames if f != page.main_frame][0]
    assert fr.locator("#fn").input_value() == "Krish"
    page.close()


def test_apply_always_tailors_a_new_resume(monkeypatch, tmp_path):
    """Pressing Apply must tailor against the current posting, even when an older resume exists."""
    from jobhunter import db
    from jobhunter.resume import runner
    db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "pay_tier": "T1", "active": "yes"})
    cid = db.list_companies()[0]["id"]
    with db.connect() as c:
        jid = c.insert("INSERT INTO jobs(company_id, external_id, title, url, location, first_seen_at, last_seen_at) "
                       "VALUES(?,?,?,?,?,?,?)", (cid, "1", "SDE 1", "http://x", "Bengaluru", "now", "now"))
    old = tmp_path / "old.pdf"; old.write_bytes(b"%PDF old")
    db.resume_finish(db.resume_start(jid), "ready", 90, False, {}, str(old), "")
    calls = []

    def fake_work(job_id, rid, cfg, notify):
        calls.append(job_id)
        new = tmp_path / "new.pdf"; new.write_bytes(b"%PDF new")
        db.resume_finish(rid, "ready", 96, True, {}, str(new), "")
        runner._active.pop(job_id, None)
    monkeypatch.setattr(runner, "_work", fake_work)
    r = runner.ensure_resume(jid, {})
    assert calls == [jid] and r["pdf_path"].endswith("new.pdf")


def test_posting_is_reread_at_tailor_time(monkeypatch):
    from jobhunter import db
    from jobhunter.collectors import registry
    from jobhunter.resume import runner
    db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "pay_tier": "T1", "active": "yes"})
    cid = db.list_companies()[0]["id"]
    with db.connect() as c:
        jid = c.insert("INSERT INTO jobs(company_id, external_id, title, url, location, description, first_seen_at, last_seen_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (cid, "1", "SDE 1", "http://x", "B", "OLD text", "now", "now"))

    class C:
        def fetch_detail(self, job, company, client): return "NEW text with Kafka"
    monkeypatch.setattr(registry, "collector_for", lambda comp: C())
    assert runner._fill_description(dict(db.get_job(jid))) == "NEW text with Kafka"
    assert db.get_job(jid)["description"] == "NEW text with Kafka"

    class Broken:
        def fetch_detail(self, job, company, client): raise RuntimeError("site down")
    monkeypatch.setattr(registry, "collector_for", lambda comp: Broken())
    assert runner._fill_description(dict(db.get_job(jid))) == "NEW text with Kafka"      # falls back to stored text


# ---------------- drafts, cover letter, dates, widgets, wizard ----------------
@pytest.fixture
def actx(tmp_path):
    from jobhunter.apply.writer import ApplyContext
    job = {"title": "Software Engineer (2027 Graduate)", "description":
           "We build distributed backend services in Java and Spring Boot with Kafka and PostgreSQL. You will design REST APIs."}
    return ApplyContext(job, "Acme Corp", {}, out_dir=tmp_path / "out", use_llm=False)


def test_drafts_use_only_facts(actx):
    from jobhunter.resume import guard
    text, how = actx.draft("Why do you want to work at this company?", 900)
    assert how == "template" and "Acme Corp" in text and len(text) <= 900
    from jobhunter.apply.writer import _sentences_lower_start
    assert guard.violations(_sentences_lower_start(text), actx.allowed) == []
    about, _ = actx.draft("Tell us about yourself", 0)
    assert "Computer Science" in about
    assert actx.draft("Describe your favourite colour", 0) is None            # not a question we draft
    assert actx.draft("Any additional information?", 0) is None


def test_llm_text_with_invented_facts_is_rejected(tmp_path):
    from jobhunter.apply.writer import ApplyContext

    class Liar:
        name, model = "FakeLLM", "m"
        def chat(self, system, user, **kw):
            return "I led a team of 40 engineers and built everything in Rust and Terraform at Google."
    ctx = ApplyContext({"title": "SDE", "description": "Java"}, "Acme", {}, out_dir=tmp_path, llm=Liar())
    text, how = ctx.draft("Why do you want to work here?", 900)
    assert how == "template" and "Rust" not in text and "40" not in text

    class Honest:
        name, model = "FakeLLM", "m"
        def chat(self, system, user, **kw):
            return "I build backend services in Java and Spring Boot, and I want to keep doing that at Acme."
    ctx2 = ApplyContext({"title": "SDE", "description": "Java"}, "Acme", {}, out_dir=tmp_path, llm=Honest())
    assert ctx2.draft("Why do you want to work here?", 900)[1] == "FakeLLM"


def test_cover_letter_pdf(actx):
    text, pdf_path, how = actx.cover_letter()
    assert text.startswith("Dear Hiring Team at Acme Corp") and "Krish Wadhwa" in text
    data = Path(pdf_path).read_bytes()
    assert data.startswith(b"%PDF")
    from jobhunter.resume.render import pdf_text
    assert "Acme Corp" in pdf_text(data)


def test_dates_and_widgets_and_drafts(pw_page, prof, pdf, actx):
    page = pw_page.new_page()
    rep = apage.run_on_page(page, (FIX / "form_long.html").as_uri(), prof, pdf, actx)
    assert page.input_value("#sd") == "2027-06-01"                           # <input type=date>
    assert page.input_value("#gd") == "05/2027"                              # MM/YYYY placeholder
    assert page.eval_on_selector("#gm", "e => e.options[e.selectedIndex].text") == "May"
    assert page.get_attribute("[role=radio]:nth-of-type(1)", "aria-checked") == "true"      # custom radio: Yes
    assert page.inner_text("#cbtn") == "India"                               # button-style dropdown
    assert "Acme Corp" in page.input_value("#why") and len(page.input_value("#why")) <= 900
    assert page.input_value("#ab")                                           # optional 'about yourself' drafted
    assert page.eval_on_selector("#cv", "e => e.files.length") == 1          # cover letter PDF attached
    assert rep["cover_letter"] and any("Cover letter" in d["label"] for d in rep["drafts"])
    assert rep["status"] == "ready", rep
    page.close()


def test_resume_is_uploaded_before_other_fields(pw_page, prof, pdf, actx):
    page = pw_page.new_page()
    page.set_content('''<form><label for="fn">Full name *</label><input id="fn">
      <label for="em">Email *</label><input id="em"><label for="ph">Phone *</label><input id="ph">
      <label for="rs">Resume *</label><input id="rs" type="file"
      onchange="document.getElementById('fn').value='PARSED FROM RESUME'"></form>''')
    qs = apage.collect(page)
    r = apage.new_report()
    apage.fill_page(page, prof, pdf, actx, r, qs)
    assert page.input_value("#fn") == "PARSED FROM RESUME"                   # what the site pre-filled is left alone
    assert page.input_value("#em") == "krishwadhwa2005@gmail.com"
    page.close()


def test_never_touches_a_password_page(pw_page, prof, pdf, actx):
    page = pw_page.new_page()
    rep = apage.run_on_page(page, (FIX / "login.html").as_uri(), prof, pdf, actx)
    assert page.input_value("#em") == "" and page.input_value("#pw") == ""
    assert rep["login"] and rep["status"] == "needs_you"
    page.close()


def test_wizard_next_step_is_filled_after_you_press_next(pw_page, prof, pdf, actx):
    import time
    page = pw_page.new_page()
    rep, final = apage.run_on_page_ex(page, (FIX / "wizard.html").as_uri(), prof, pdf, actx)
    assert page.input_value("#fn") == "Krish Wadhwa" and rep["status"] == "ready"
    state = {"deadline": time.time() + 60, "fp": apage.pending_fp(apage.collect(final))}
    assert apage.watch_tick(final, prof, pdf, actx, state) is None            # nothing new yet
    page.click("#next")                                                       # the human presses Next
    step = apage.watch_tick(final, prof, pdf, actx, state)
    assert step and page.input_value("#li").startswith("https://linkedin.com")
    assert page.input_value("#sc").startswith("Jaypee")
    assert [n["label"] for n in step["needs"]] == ["Do you have a pet? *"] or "pet" in step["needs"][0]["label"].lower()
    assert apage.watch_tick(final, prof, pdf, actx, state) is None            # no repeat for the same step
    page.fill("#sc", "My own edit")
    state["fp"] = apage.pending_fp(apage.collect(final))
    apage.watch_tick(final, prof, pdf, actx, state)
    assert page.input_value("#sc") == "My own edit"                           # your edits are never overwritten
    page.close()


def test_later_steps_update_the_application(monkeypatch):
    from jobhunter import db, notifier
    from jobhunter.apply import worker
    db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "pay_tier": "T1", "active": "yes"})
    cid = db.list_companies()[0]["id"]
    with db.connect() as c:
        jid = c.insert("INSERT INTO jobs(company_id, external_id, title, url, location, first_seen_at, last_seen_at) "
                       "VALUES(?,?,?,?,?,?,?)", (cid, "1", "SDE 1", "http://x", "B", "now", "now"))
    app_id = db.application_start(jid)
    db.application_update(app_id, "needs_you", report={"filled": [{"label": "a"}], "needs": [{"label": "login"}], "drafts": [],
                                                       "login": True, "final_url": "http://x"})
    sent = []

    class FakeTg:
        configured = True
        def send(self, text, keyboard=None, **kw): sent.append(text)
    monkeypatch.setattr(notifier, "from_env", lambda: FakeTg())
    step = {"final_url": "http://x/step2", "filled": [{"label": "College"}, {"label": "LinkedIn"}], "needs": [{"label": "Pet", "why": "no saved answer"}],
            "drafts": [{"label": "Why us", "text": "t", "how": "template"}], "resume_uploaded": False}
    worker._on_step(jid, app_id, step)
    app = db.get_application(jid)
    assert app["status"] == "needs_you" and len(app["report"]["filled"]) == 3 and app["report"]["login"] is False
    assert app["report"]["steps"][0]["filled"] == 2
    assert "Next step filled" in sent[0] and "Pet" in sent[0] and "Why us" in sent[0]
