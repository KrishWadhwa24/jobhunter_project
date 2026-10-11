"""Collectors are tested against response shapes from each platform's public API.
`make check` is what proves them against the live sites."""
from jobhunter.collectors.ashby import Ashby
from jobhunter.collectors.custom import Atlassian, for_host
from jobhunter.collectors.greenhouse import Greenhouse
from jobhunter.collectors.jibe import Jibe
from jobhunter.collectors.kula import Kula, account_of
from jobhunter.collectors.lever import Lever
from jobhunter.collectors.registry import collector_for
from jobhunter.collectors.base import Unsupported
from jobhunter.collectors.smartrecruiters import SmartRecruiters
from jobhunter.collectors.workday import Workday, parse_base
import pytest


class FakeClient:
    def __init__(self, get=None, post=None):
        self.get_map, self.post_map, self.calls = get or {}, post or {}, []

    def _find(self, m, url):
        for k, v in m.items():
            if k in url:
                return v
        raise AssertionError(f"unexpected URL {url}")

    def get_json(self, url, **kw):
        self.calls.append(("GET", url, kw))
        v = self._find(self.get_map, url)
        return v(kw) if callable(v) else v

    def post_json(self, url, **kw):
        self.calls.append(("POST", url, kw))
        v = self._find(self.post_map, url)
        return v(kw) if callable(v) else v


def test_greenhouse():
    c = FakeClient(get={"boards/stripe/jobs": {"jobs": [
        {"id": 1, "title": "Software Engineer", "absolute_url": "https://x/1", "location": {"name": "Bengaluru"},
         "content": "&lt;p&gt;Need &lt;b&gt;0-2 years&lt;/b&gt; of experience&lt;/p&gt;", "updated_at": "2026-10-01"}]}})
    r = Greenhouse().list_jobs({"slug_or_endpoint": "stripe"}, c)
    assert r.jobs[0].location == "Bengaluru" and "0-2 years" in r.jobs[0].description and "<" not in r.jobs[0].description


def test_greenhouse_eu():
    c = FakeClient(get={"boards-api.eu.greenhouse.io/v1/boards/groww/jobs": {"jobs": []}})
    assert Greenhouse().list_jobs({"slug_or_endpoint": "eu:groww"}, c).jobs == []


def test_lever():
    c = FakeClient(get={"postings/cred": [{"id": "a", "text": "SDE 1", "hostedUrl": "https://l/a",
                                            "categories": {"location": "Bengaluru"}, "descriptionPlain": "Hello",
                                            "lists": [{"text": "Req", "content": "<li>3 years</li>"}]}]})
    j = Lever().list_jobs({"slug_or_endpoint": "cred"}, c).jobs[0]
    assert j.title == "SDE 1" and "3 years" in j.description


def test_ashby():
    c = FakeClient(get={"job-board/atlan": {"jobs": [{"id": "z", "title": "Backend Engineer", "location": "Bengaluru",
                                                       "isRemote": True, "descriptionPlain": "x", "jobUrl": "https://a/z"}]}})
    j = Ashby().list_jobs({"slug_or_endpoint": "atlan"}, c).jobs[0]
    assert "Remote" in j.location


def test_smartrecruiters_paginates_and_detail():
    page = lambda kw: {"totalFound": 120, "content": [{"id": str(kw["params"]["offset"] + i), "name": "SWE",
                       "location": {"city": "Bengaluru", "country": "in"}} for i in range(100 if kw["params"]["offset"] == 0 else 20)]}
    c = FakeClient(get={"/postings?": page, "companies/Grab/postings": page})
    r = SmartRecruiters().list_jobs({"slug_or_endpoint": "Grab"}, c)
    assert len(r.jobs) == 120 and r.jobs[0].location.endswith("IN")


def test_workday_parse_and_list():
    assert parse_base("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced") == (
        "adobe.wd5.myworkdayjobs.com", "adobe", "external_experienced")
    post = lambda kw: {"total": 1, "jobPostings": [{"title": "Software Engineer", "externalPath": "/job/India-Noida/SE_R1",
                                                    "locationsText": "India, Noida", "bulletFields": ["R1"]}]} if kw["json"]["offset"] == 0 else {"total": 1, "jobPostings": []}
    c = FakeClient(post={"/wday/cxs/adobe/external_experienced/jobs": post})
    r = Workday().list_jobs({"slug_or_endpoint": "https://adobe.wd5.myworkdayjobs.com/external_experienced"}, c)
    assert len(r.jobs) == 1 and r.jobs[0].external_id == "R1" and r.jobs[0].url.endswith("/external_experienced/job/India-Noida/SE_R1")
    assert r.complete is False


def test_jibe():
    c = FakeClient(get={"/api/jobs": {"totalCount": 1, "jobs": [{"data": {"title": "SWE", "req_id": "9", "location_name": "Hyderabad, India",
                                                                          "apply_url": "https://icims/9", "description": "<p>hi</p>"}}]}})
    j = Jibe().list_jobs({"slug_or_endpoint": "https://careers.amd.com/api/jobs?location=India&page=1"}, c).jobs[0]
    assert j.external_id == "9" and "Hyderabad" in j.location


def test_atlassian_and_dispatch():
    c = FakeClient(get={"careers/listings": [{"id": 1, "title": "SWE", "locations": ["Bengaluru, India"], "applyUrl": "https://a/1"}]})
    assert Atlassian().list_jobs({}, c).jobs[0].location.startswith("Bengaluru")
    assert for_host("https://www.atlassian.com/endpoint/careers/listings").name == "atlassian"
    with pytest.raises(Unsupported):
        for_host("https://example.com/jobs.json")


def test_browser_sources_use_browser_collector_but_linkedin_is_skipped():
    from jobhunter.collectors.browser import BrowserCollector
    assert isinstance(collector_for({"source_type": "browser", "slug_or_endpoint": "", "careers_url": "https://a.com/c"}),
                      BrowserCollector)
    with pytest.raises(Unsupported):
        collector_for({"source_type": "linkedin_only", "slug_or_endpoint": "", "careers_url": "https://www.linkedin.com/company/x/jobs"})


def test_kula():
    item = lambda i, title, **kw: {"id": i, "title": title, "listed": True, "is_confidential": False, "end_at": None,
                                    "launch_at": "2026-07-09T10:56:21.000Z",
                                    "ats_job": {"job_description": "<p>Need <b>0-1 years</b></p>", "workplace": "office",
                                                "employment_type": kw.get("et", "full_time"),
                                                "offices": [{"location": "Bengaluru, Karnataka, India"}]}}
    data = {"data": [item(50594, "SDE 2 - Backend"), item(5, "Content Writer", et="internship"),
                     dict(item(6, "Hidden"), listed=False), dict(item(7, "Old"), end_at="2020-01-01T00:00:00Z")],
            "meta": {"count": 4, "page": 1, "pages": 1}, "errors": []}
    c = FakeClient(get={"ats_job_posts": data})
    jobs = Kula().list_jobs({"slug_or_endpoint": "https://careers.kula.ai/slice?jobs=true"}, c).jobs
    assert [j.title for j in jobs] == ["SDE 2 - Backend", "Content Writer (Internship)"]
    assert jobs[0].url == "https://careers.kula.ai/slice/50594-sde-2-backend?jobs=true"
    assert jobs[0].location == "Bengaluru, Karnataka, India" and "0-1 years" in jobs[0].description
    assert c.calls[0][2]["params"]["accountName"] == "slice"
    assert account_of("slice") == "slice" and account_of("https://careers.kula.ai/api/internal/ats_job_posts?accountName=acko&page=1") == "acko"
    assert collector_for({"source_type": "kula", "careers_url": "", "slug_or_endpoint": "slice"}).name == "kula"


def test_kula_detected_from_urls():
    from jobhunter.detect import _match_patterns
    assert _match_patterns("https://careers.kula.ai/acko?jobs=true") == {"source_type": "kula", "slug_or_endpoint": "acko"}
    assert _match_patterns("https://careers.kula.ai/api/internal/ats_job_posts?accountName=slice&page=1")["slug_or_endpoint"] == "slice"
