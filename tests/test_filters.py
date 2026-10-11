import copy
import pytest
from jobhunter import config
from jobhunter.filters import Evaluator, level_from_title, years_required, batch_signals, india_status


@pytest.fixture
def cfg():
    return config.load_config()


def ev(cfg, title, loc="Bengaluru, India", desc="", tier="T2"):
    return Evaluator(cfg).evaluate(title, loc, desc, tier)


def test_levels_from_title():
    assert level_from_title("Software Engineer (2027 Graduate)") == "sde1"
    assert level_from_title("SDE-1 Backend") == "sde1"
    assert level_from_title("Software Development Engineer I") == "sde1"
    assert level_from_title("Software Engineer II") == "sde2"
    assert level_from_title("SDE 3") == "sde3"
    assert level_from_title("Senior Software Engineer") == "sde3"
    assert level_from_title("Staff Software Engineer") == "staff"
    assert level_from_title("Software Engineer Intern") == "intern"
    assert level_from_title("Associate Software Engineer") == "sde1"
    assert level_from_title("Software Engineer") is None
    assert level_from_title("Software Engineer, Android") is None


def test_india():
    assert india_status("Bengaluru, Karnataka") is True
    assert india_status("Pune") is True
    assert india_status("Indianapolis, IN") is False
    assert india_status("New York") is False
    assert india_status("2 Locations") is None
    assert india_status("Remote - India") is True
    assert india_status("") is None


def test_years():
    assert years_required("Requires 3+ years of experience in Java")[0] == 3
    assert years_required("0-2 years of experience")[0] == 0
    y, ni = years_required("1+ years of non-internship professional software development experience")
    assert y == 1 and ni
    assert years_required("We were founded 10 years ago")[0] is None


def test_batch_signals():
    b = batch_signals("Software Engineer (2027 Graduate)", "", 2027)
    assert b["accepts_grad_year"] is True
    b = batch_signals("SDE", "Open to 2024, 2025 and 2026 graduates only", 2027)
    assert b["accepts_grad_year"] is False
    b = batch_signals("Engineer", "Eligible batches: 2024-2028 batch", 2027)
    assert b["accepts_grad_year"] is True
    b = batch_signals("Software Engineer UHR-FY27 (Code with Cisco)", "", 2027)
    assert b["accepts_grad_year"] is True
    assert batch_signals("SDE", "Immediate joiners preferred", 2027)["immediate_joiner"]


def test_graviton_2027_is_match(cfg):
    e = ev(cfg, "Software Engineer (2027 Graduate)", "Gurugram, Haryana", "2027 graduates. CGPA 8.5+", "T1")
    assert e.verdict == "match" and e.level == "sde1" and e.score >= 85


def test_batch_2026_only_rejected(cfg):
    e = ev(cfg, "Software Development Engineer I", desc="Open to 2024, 2025 and 2026 batch graduates")
    assert e.verdict == "reject"


def test_senior_is_in_scope_but_not_a_target_by_default(cfg):
    e = ev(cfg, "Senior Software Engineer")
    assert e.in_scope and e.level == "sde3" and e.flags["target"] is False
    assert e.verdict == "match"                      # the verdict does not depend on which levels you target


def test_targets_are_configurable(cfg):
    c2 = copy.deepcopy(cfg)
    c2["targets"]["levels"] = ["sde2", "sde3"]
    assert ev(c2, "Senior Software Engineer").flags["target"] is True
    assert ev(c2, "Software Engineer II").flags["target"] is True
    assert ev(c2, "Software Engineer Intern").flags["target"] is False


def test_general_scheme_levels_and_roles():
    import yaml
    from jobhunter import config
    c = config.load_config()
    c["levels"] = {"scheme": "general"}
    c["roles"] = {"families": ["customer_service", "writing_content", "editorial_publishing"]}
    c["targets"] = {"levels": ["intern", "entry"]}
    c["locations"] = {"mode": "india_or_remote"}
    c = yaml.safe_load(yaml.safe_dump(c))
    e = Evaluator(c)
    r = e.evaluate("Customer Service Associate", "Gurugram, India", "Fresher welcome. Excellent English.", "")
    assert r.in_scope and r.level == "entry" and r.flags["target"] and r.verdict == "match"
    assert e.evaluate("Content Writer", "Remote - Worldwide", "", "").in_scope
    assert not e.evaluate("Content Writer", "New York, United States", "", "").in_scope
    assert not e.evaluate("Backend Software Engineer", "Bengaluru", "", "").in_scope          # not her roles
    assert not e.evaluate("Video Editor", "Mumbai", "", "").in_scope
    assert e.evaluate("Senior Content Strategist", "Mumbai", "", "").level == "senior"
    assert e.evaluate("Editorial Intern", "Delhi", "", "").level == "intern"
    assert e.evaluate("Management Trainee - Editorial", "Delhi", "", "").level == "entry"


def test_unlevelled_uses_years(cfg):
    c2 = copy.deepcopy(cfg)
    c2["targets"]["levels"] = ["sde2"]
    e = ev(c2, "Software Engineer", desc="3+ years of experience building backend services")
    assert e.level == "sde2" and e.verdict == "match"
    e = ev(cfg, "Software Engineer", desc="0-1 years of experience")
    assert e.level == "sde1"
    e = ev(cfg, "Software Engineer", desc="nothing about experience")
    assert e.level == "sde1" and e.verdict == "match" and e.flags["level_source"] == "assumed"   # shown, not hidden
    assert "no level stated" in " ".join(e.reasons) and e.score < ev(cfg, "Software Engineer I", desc="nothing").score


def test_non_internship_year_downgrades(cfg):
    e = ev(cfg, "SDE I FTC", "Hyderabad, India", "1+ years of non-internship professional software development experience")
    assert e.verdict == "maybe"


def test_excluded_and_foreign(cfg):
    assert not ev(cfg, "Sales Engineer").in_scope
    assert not ev(cfg, "Engineering Manager").in_scope
    assert not ev(cfg, "Software Engineer", "San Francisco, CA").in_scope
    assert not ev(cfg, "Account Executive").in_scope


def test_goldman_analyst_title(cfg):
    e = ev(cfg, "2027 | India | Engineering | New Analyst", "Bengaluru", "2027 graduates", "T2")
    assert e.in_scope


def test_intern_verdict(cfg):
    e = ev(cfg, "Software Engineer Intern", desc="Pursuing B.Tech. 6 months internship")
    assert e.level == "intern" and e.verdict == "match"


def test_other_language_jobs_are_hidden():
    import yaml
    c = config.load_config()
    c["levels"] = {"scheme": "general"}
    c["roles"] = {"families": ["customer_service", "writing_content", "teaching_education"]}
    c["targets"] = {"levels": ["intern", "entry"]}
    c["locations"] = {"mode": "india_or_remote"}
    e = Evaluator(yaml.safe_load(yaml.safe_dump(c)))
    for t in ["Japanese Content Writer", "Spanish Customer Service Associate", "Chinese Language Teacher",
              "Customer Support - French Speaking", "Mandarin Tutor", "Tamil Content Writer", "Hindi Content Writer"]:
        r = e.evaluate(t, "Remote - India", "", "")
        assert not r.in_scope, t
    assert e.evaluate("English Tutor", "Remote - India", "", "").in_scope
    r = e.evaluate("Customer Service Associate", "Gurugram, India", "Must be fluent in Japanese and English.", "")
    assert r.verdict == "reject" and "Japanese" in r.reasons[0]
    r = e.evaluate("Customer Service Associate", "Gurugram, India", "Excellent English. Spanish is a plus.", "")
    assert r.verdict == "match"
    r = e.evaluate("Customer Service Associate", "Gurugram, India", "We serve customers worldwide. Fresher welcome.", "")
    assert r.verdict == "match"
    c["languages"] = {"known": ["english", "hindi", "spanish"]}
    assert Evaluator(yaml.safe_load(yaml.safe_dump(c))).evaluate("Spanish Tutor", "Remote - India", "", "").in_scope
