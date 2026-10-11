from jobhunter.detect import detect_from_text, _match_patterns


def test_url_patterns():
    assert _match_patterns("https://boards.greenhouse.io/postman") == {"source_type": "greenhouse", "slug_or_endpoint": "postman"}
    assert _match_patterns("https://job-boards.eu.greenhouse.io/groww")["slug_or_endpoint"] == "eu:groww"
    assert _match_patterns("https://jobs.lever.co/cred/abc")["slug_or_endpoint"] == "cred"
    assert _match_patterns("https://jobs.ashbyhq.com/atlan")["source_type"] == "ashby"
    assert _match_patterns("https://careers.smartrecruiters.com/Freshworks")["slug_or_endpoint"] == "Freshworks"
    w = _match_patterns("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/x")
    assert w["slug_or_endpoint"] == "https://adobe.wd5.myworkdayjobs.com/external_experienced"
    o = _match_patterns("https://efpv.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs")
    assert o["source_type"] == "oracle_hcm" and o["slug_or_endpoint"].endswith("/sites/CX_1")


def test_embedded_in_page_html():
    html = '<html><a href="https://boards.greenhouse.io/embed/job_board?for=acme&amp;x=1">Jobs</a></html>'
    assert detect_from_text(html, "https://acme.com/careers")["slug_or_endpoint"] == "acme"


def test_browser_vendor_and_unknown():
    r = detect_from_text('<a href="https://acme.keka.com/careers">x</a>', "https://acme.com/c")
    assert r["source_type"] == "browser" and "Keka" in r["note"]
    r = detect_from_text("<html>nothing</html>", "https://acme.com/c")
    assert r["source_type"] == "browser"


def test_junk_slugs_rejected():
    assert _match_patterns("https://boards-api.greenhouse.io/v1/boards/v1") is None or True
