"""Browser collector tests. Pure-helper tests always run; the end-to-end ones need Playwright + a Chrome and are
skipped otherwise (set JOBHUNTER_CHROME_PATH / JOBHUNTER_NO_SANDBOX in odd environments)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from jobhunter.collectors import browser as B

API_JOBS = [
    {"id": 101, "title": "Software Engineer I", "location": {"city": "Bengaluru", "country": "India"}, "applyUrl": "/apply/101"},
    {"id": 102, "title": "Senior Backend Engineer", "location": "Pune, India"},
    {"id": 103, "title": "Product Designer", "location": "Remote"},
]


def test_find_job_list_in_nested_json():
    payload = {"data": {"meta": {"total": 3}, "departments": [{"id": 1, "name": "Engineering"}, {"id": 2, "name": "Sales"}],
                        "results": {"jobs": API_JOBS}}}
    jobs, endpoint = B.jobs_from_json_responses([("https://x.com/api/search", payload)], "https://x.com/careers")
    assert [j.title for j in jobs] == ["Software Engineer I", "Senior Backend Engineer", "Product Designer"]
    assert jobs[0].location == "Bengaluru, India" and jobs[0].url == "https://x.com/apply/101"
    assert jobs[1].location == "Pune, India" and jobs[1].external_id == "102"


def test_department_lists_are_not_jobs():
    payload = {"departments": [{"id": 1, "name": "Engineering"}, {"id": 2, "name": "Sales"}, {"id": 3, "name": "Finance"}]}
    assert B.jobs_from_json_responses([("https://x.com/api/d", payload)], "https://x.com/careers")[0] == []


def test_paginated_feed_pages_are_merged():
    p1 = {"items": [{"jobId": i, "jobTitle": f"Software Engineer {i}", "city": "Pune"} for i in range(1, 4)]}
    p2 = {"items": [{"jobId": i, "jobTitle": f"Software Engineer {i}", "city": "Pune"} for i in range(3, 6)]}
    jobs, _ = B.jobs_from_json_responses([("https://x.com/api?page=1", p1), ("https://x.com/api?page=2", p2)], "https://x.com/c")
    assert sorted(j.external_id for j in jobs) == ["1", "2", "3", "4", "5"]


def test_dom_links():
    anchors = [
        {"href": "/jobs/55-sde-1", "text": "Software Development Engineer I\nBengaluru, India", "ctx": ""},
        {"href": "/careers", "text": "Careers", "ctx": ""},
        {"href": "/jobs/", "text": "View all jobs", "ctx": ""},
        {"href": "https://linkedin.com/company/x/jobs/1234", "text": "Software Engineer", "ctx": ""},
        {"href": "/openings?jobId=77", "text": "Backend Intern", "ctx": "Backend Intern\nHyderabad"},
        {"href": "/about", "text": "About us", "ctx": ""},
    ]
    jobs = B.jobs_from_dom_links(anchors, "https://acme.com/careers")
    assert [(j.external_id, j.title, j.location) for j in jobs] == [
        ("55-sde-1", "Software Development Engineer I", "Bengaluru, India"), ("77", "Backend Intern", "Hyderabad")]


def test_follow_links_finds_the_job_list_page():
    anchors = [
        {"href": "/about", "text": "About us"},
        {"href": "/jobs/", "text": "View All Jobs"},
        {"href": "https://hccz.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2/my-profile/sign-in", "text": "LOGIN TO PROFILE"},
        {"href": "https://linkedin.com/company/x/jobs", "text": "View jobs on LinkedIn"},
        {"href": "https://other.com/x", "text": "Explore our roles"},
        {"href": "/privacy", "text": "Privacy"},
        {"href": "/careers/openings", "text": ""},
        {"href": "mailto:hr@x.com", "text": "Current openings"},
    ]
    got = B.follow_links(anchors, "https://acme.com/careers")
    assert got[0] == "https://acme.com/jobs/"
    assert "https://acme.com/careers/openings" in got and "https://other.com/x" in got
    assert not any("linkedin" in u or "sign-in" in u or "privacy" in u or "mailto" in u for u in got)
    assert B.follow_links(anchors, "https://acme.com/careers", tried=["https://acme.com/jobs"])[0] != "https://acme.com/jobs/"


def test_platform_detection_from_urls():
    hit = B.detect_platform_in_urls(["https://x.com/a.js", "https://boards-api.greenhouse.io/v1/boards/acme/jobs"])
    assert hit == {"source_type": "greenhouse", "slug_or_endpoint": "acme"}
    assert B.detect_platform_in_urls(["https://x.com/a.js"]) is None


# ---------------- end-to-end against local fake sites ----------------
PAGE_API = """<html><body><h1>Careers</h1><div id=app>Loading…</div><button id=more onclick="more()">Load more</button>
<script>
let page=0;
async function more(){ page++; const r=await fetch('/api/jobs?page='+page); const d=await r.json();
 if(!d.jobs.length){document.getElementById('more').remove();return}
 document.getElementById('app').innerText += ' ' + d.jobs.length; }
more();
</script></body></html>"""
PAGE_DOM = """<html><body><ul id=l></ul><script>
const jobs=[['Software Engineer','Bengaluru, India',11],['Platform Engineer','Hyderabad, India',12],['Account Executive','Mumbai',13]];
for(const [t,l,i] of jobs){ const li=document.createElement('li'); li.innerHTML='<a href="/jobs/'+i+'-x">'+t+'<br><span>'+l+'</span></a>'; document.getElementById('l').appendChild(li);}
</script></body></html>"""
PAGE_ATS = """<html><body><h1>Join us</h1><script>fetch('https://boards-api.greenhouse.io/v1/boards/acme/jobs').catch(()=>{});</script></body></html>"""
PAGE_LANDING = "<html><body><h1>Join our team</h1><a href='/about'>About</a><a href='/jobs/'>Current openings</a></body></html>"
PAGE_LIST = "<html><body><h1>Openings</h1><ul><li><a href='/jobs/21-writer'>Content Writer<br>Noida, India</a></li><li><a href='/jobs/22-tutor'>English Tutor<br>Remote</a></li></ul></body></html>"
PAGE_EMPTY = "<html><body><h1>No openings right now</h1><a href='/about'>About</a></body></html>"
JOB_PAGE = "<html><body><main><h1>Software Engineer</h1><p>Bengaluru, India. 0-1 years. 2027 graduates welcome.</p></main></body></html>"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/jobs":
            page = int(parse_qs(u.query).get("page", ["1"])[0])
            jobs = [{"id": 200 + page * 10 + i, "title": f"Software Engineer {page}-{i}", "location": "Pune, India"} for i in range(3)] if page <= 2 else []
            body, ct = json.dumps({"jobs": jobs}), "application/json"
        else:
            body, ct = {"/api": PAGE_API, "/dom": PAGE_DOM, "/ats": PAGE_ATS, "/empty": PAGE_EMPTY,
                      "/landing": PAGE_LANDING, "/jobs/": PAGE_LIST}.get(u.path, JOB_PAGE), "text/html"
        data = body.encode()
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser_ok():
    ok, msg = B.available()
    if not ok:
        pytest.skip(f"browser not available: {msg}")


def _company(url, st="browser"):
    return {"id": 1, "name": "Fake", "source_type": st, "careers_url": url, "slug_or_endpoint": url}


def test_reads_data_feed_with_load_more(site, browser_ok):
    res = B.BrowserCollector().list_jobs(_company(site + "/api"), client=None)
    assert len(res.jobs) == 6 and not res.complete        # page 1 and page 2 both loaded
    assert all(j.title.startswith("Software Engineer") for j in res.jobs)


def test_reads_rendered_links(site, browser_ok):
    res = B.BrowserCollector().list_jobs(_company(site + "/dom"), client=None)
    got = {j.title: j.location for j in res.jobs}
    assert got["Software Engineer"] == "Bengaluru, India" and got["Platform Engineer"] == "Hyderabad, India"


def test_empty_page_raises_clear_error(site, browser_ok):
    from jobhunter.collectors.base import CollectorError
    with pytest.raises(CollectorError, match="no job list"):
        B.BrowserCollector().list_jobs(_company(site + "/empty"), client=None)


def test_detail_text(site, browser_ok):
    from jobhunter.models import Job
    txt = B.BrowserCollector().fetch_detail(Job("1", "SE", site + "/jobs/1-x"), _company(site + "/dom"), None)
    assert "2027 graduates" in txt


def test_platform_switch(site, browser_ok, monkeypatch):
    switched = {}
    monkeypatch.setattr(B, "_upgrade_company", lambda comp, hint: switched.update(hint) or None)
    from jobhunter.collectors.base import CollectorError
    with pytest.raises(CollectorError):
        B.BrowserCollector().list_jobs(_company(site + "/ats"), client=None)
    assert switched == {"source_type": "greenhouse", "slug_or_endpoint": "acme"}


def test_landing_page_followed_to_the_job_list(site, browser_ok):
    res = B.BrowserCollector().list_jobs(_company(site + "/landing"), client=None)
    assert sorted(j.title for j in res.jobs) == ["Content Writer", "English Tutor"]
    assert "after opening" in res.note and res.complete is False


def test_empty_page_error_mentions_nothing_followed(site, browser_ok):
    with pytest.raises(Exception) as e:
        B.BrowserCollector().list_jobs(_company(site + "/empty"), client=None)
    assert "no job list" in str(e.value)
