"""Browser collector: reads careers pages that build their job list in the browser.

How it works, per company:
 1. Open the careers page in a headless Chrome and record every JSON response the page loads.
 2. Scroll / press "Load more" a few times so lazy lists and pagination are triggered.
 3. Look for a job list inside the recorded JSON (the page's own data feed). This is the most reliable route.
 4. If there is none, read the job links from the rendered page instead.
 5. If the page turns out to use a hiring platform we already have an API collector for (Greenhouse, Lever,
    Ashby, ...), the company is switched to that collector, which is faster and complete.

Chrome is only started when a browser-type company is due and is closed again after a couple of idle minutes.
Playwright's sync API is single-threaded, so one dedicated thread owns the browser and companies are read one by one.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlparse

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, CollectorError, FetchResult, Unsupported

log = logging.getLogger("jobhunter.browser")

NAV_TIMEOUT_MS = 45000          # slow connections: give a page longer, and try once more if it drops
IDLE_CLOSE_SECONDS = 120
TASK_TIMEOUT_SECONDS = 150
MAX_JSON_BYTES = 6_000_000
MAX_LOAD_MORE_ROUNDS = 8
MAX_HOPS = 2                    # landing page -> 'View all jobs' page -> (once more)

MAC_BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

# ----------------------------------------------------------------------------------------------
# Pure helpers (no browser needed; unit tested)
# ----------------------------------------------------------------------------------------------
STRONG_TITLE_KEYS = ["title", "jobTitle", "job_title", "jobtitle", "positionTitle", "position_title", "postingTitle",
                     "posting_title", "requisitionTitle", "jobName", "job_name", "position", "designation",
                     "externalTitle", "displayName"]
WEAK_TITLE_KEYS = ["name", "text", "role", "label", "heading"]
ID_KEYS = ["id", "jobId", "job_id", "jobID", "jid", "requisitionId", "requisition_id", "reqId", "req_id", "reqID",
           "jobReqId", "jobCode", "job_code", "externalId", "external_id", "postingId", "posting_id", "uuid", "slug",
           "positionId", "position_id", "openingId", "_id", "refNumber", "ref", "code", "jobNumber"]
URL_KEYS = ["url", "absolute_url", "applyUrl", "apply_url", "applyLink", "jobUrl", "job_url", "jobLink", "link",
            "href", "canonicalUrl", "detailsUrl", "hostedUrl", "externalPath", "path", "careerPageUrl", "jobPostingUrl",
            "shareUrl"]
LOCATION_KEYS = ["location", "locations", "jobLocation", "job_location", "city", "office", "offices", "locationName",
                 "location_name", "primaryLocation", "workLocation", "workLocations", "cities", "place", "country",
                 "locationsText", "jobLocations"]
DESC_KEYS = ["description", "jobDescription", "job_description", "content", "summary", "responsibilities",
             "descriptionHtml", "details", "shortDescription"]
DATE_KEYS = ["postedOn", "posted_at", "postedAt", "datePosted", "date_posted", "createdAt", "created_at",
             "publishedAt", "published_at", "postedDate", "posting_date", "updated_at", "postDate"]

JOB_WORDS = re.compile(
    r"engineer|developer|\bsde\b|\bswe\b|software|intern|analyst|manager|designer|scientist|architect|associate|lead|"
    r"specialist|executive|officer|consultant|administrator|programmer|tester|\bqa\b|devops|\bsre\b|director|head of|"
    r"researcher|technician|trainee|apprentice|coordinator|representative|\bagent\b|writer|editor|content|tutor|teacher|"
    r"coach|moderator|reviewer|annotator|reporter|producer|journalist|strategist|support|instructor|trainer|rater|"
    r"evaluator|curator|communications", re.I)

_KEY_NORM = {}


def _lc_index(keys: List[str]) -> Dict[str, str]:
    return {k.lower(): k for k in keys}


_TITLE_STRONG_LC = _lc_index(STRONG_TITLE_KEYS)
_TITLE_WEAK_LC = _lc_index(WEAK_TITLE_KEYS)
_ID_LC = _lc_index(ID_KEYS)
_URL_LC = _lc_index(URL_KEYS)
_LOC_LC = _lc_index(LOCATION_KEYS)
_DESC_LC = _lc_index(DESC_KEYS)
_DATE_LC = _lc_index(DATE_KEYS)


def _first_key(d: Mapping[str, Any], lc_map: Dict[str, str]) -> Optional[str]:
    low = {k.lower(): k for k in d.keys()}
    for lc in lc_map:                       # preserves the priority order of the list
        if lc in low:
            return low[lc]
    return None


def _as_text(v: Any, depth: int = 0) -> str:
    """Flatten strings / lists / small dicts (e.g. locations) to readable text."""
    if v is None or depth > 3:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, list):
        parts = [_as_text(x, depth + 1) for x in v[:8]]
        return "; ".join(p for p in parts if p)
    if isinstance(v, dict):
        order = ["name", "label", "displayName", "text", "city", "state", "region", "country", "countryName",
                 "address", "locationName", "title", "value"]
        parts, seen = [], set()
        low = {k.lower(): k for k in v.keys()}
        for k in order:
            if k.lower() in low:
                t = _as_text(v[low[k.lower()]], depth + 1)
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    parts.append(t)
        return ", ".join(parts)
    return ""


def _title_of(d: Mapping[str, Any]) -> Tuple[str, bool]:
    k = _first_key(d, _TITLE_STRONG_LC)
    strong = True
    if k is None:
        k = _first_key(d, _TITLE_WEAK_LC)
        strong = False
    if k is None:
        return "", False
    v = d[k]
    if isinstance(v, str) and 3 <= len(v.strip()) <= 160:
        return v.strip(), strong
    return "", False


def _score_list(items: List[Any]) -> Tuple[float, int]:
    """Score how much a list of dicts looks like a list of job postings. Returns (score, n_valid)."""
    dicts = [x for x in items if isinstance(x, dict)]
    if len(dicts) < 1 or len(dicts) < 0.8 * len(items):
        return 0.0, 0
    valid = strong = with_loc = with_url = job_words = 0
    for d in dicts[:200]:
        t, is_strong = _title_of(d)
        if not t:
            continue
        has_id = _first_key(d, _ID_LC) is not None
        has_url = _first_key(d, _URL_LC) is not None
        if not (has_id or has_url):
            continue
        valid += 1
        strong += 1 if is_strong else 0
        with_loc += 1 if _first_key(d, _LOC_LC) is not None else 0
        with_url += 1 if has_url else 0
        job_words += 1 if JOB_WORDS.search(t) else 0
    n = min(len(dicts), 200)
    if valid == 0 or valid < 0.6 * n:
        return 0.0, 0
    ratio_words = job_words / valid
    pts = 0.0
    pts += 2 if strong / valid >= 0.6 else 0
    pts += 1 if with_loc / valid >= 0.5 else 0
    pts += 1 if with_url / valid >= 0.5 else 0
    pts += 2 if ratio_words >= 0.25 else 0
    if pts < 3:
        return 0.0, 0
    return pts, valid


def _walk_lists(obj: Any, depth: int = 0):
    if depth > 7:
        return
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj[:5]):
            yield obj
        for x in obj[:50]:
            if isinstance(x, (dict, list)):
                yield from _walk_lists(x, depth + 1)
    elif isinstance(obj, dict):
        for v in list(obj.values())[:80]:
            if isinstance(v, (dict, list)):
                yield from _walk_lists(v, depth + 1)


def find_job_lists(payload: Any) -> List[Tuple[float, List[Dict[str, Any]]]]:
    """All lists inside a JSON payload that look like job postings, best first."""
    out = []
    for lst in _walk_lists(payload):
        score, n = _score_list(lst)
        if score:
            out.append((score * (1 + min(n, 100) / 100.0), lst))
    out.sort(key=lambda t: -t[0])
    return out


def _signature(lst: List[Dict[str, Any]]) -> Tuple[str, ...]:
    return tuple(sorted(lst[0].keys()))[:30]


def record_to_job(d: Mapping[str, Any], base_url: str, source_url: str = "") -> Optional[Job]:
    title, _ = _title_of(d)
    if not title:
        return None
    idk = _first_key(d, _ID_LC)
    urlk = _first_key(d, _URL_LC)
    raw_url = d[urlk] if urlk and isinstance(d[urlk], str) else ""
    url = ""
    if raw_url:
        if raw_url.startswith(("http://", "https://")):
            url = raw_url
        elif raw_url.startswith("/") or "/" in raw_url:
            url = urljoin(base_url, raw_url)
    ext_id = _as_text(d[idk]) if idk else ""
    if not ext_id:
        ext_id = url or hashlib.sha1((title + _as_text(d.get(_first_key(d, _LOC_LC) or "", ""))).encode()).hexdigest()[:16]
    if not url:
        # no direct link in the data: point at the careers page, with the id so each job stays distinct
        url = base_url + ("#" + ext_id if ext_id and "#" not in base_url else "")
    lk = _first_key(d, _LOC_LC)
    location = _as_text(d[lk]) if lk else ""
    dk = _first_key(d, _DESC_LC)
    desc = ""
    if dk and isinstance(d[dk], str) and len(d[dk]) > 80:
        desc = html_to_text(d[dk])
    pk = _first_key(d, _DATE_LC)
    posted = _as_text(d[pk])[:40] if pk else None
    return Job(ext_id, title, url, location[:200], desc, posted)


def jobs_from_json_responses(responses: List[Tuple[str, Any]], page_url: str) -> Tuple[List[Job], str]:
    """Pick the best job list among the page's JSON responses (merging paginated calls of the same feed)."""
    groups: Dict[Tuple[str, Tuple[str, ...]], List[Tuple[float, List[Dict[str, Any]], str]]] = {}
    for resp_url, payload in responses:
        for score, lst in find_job_lists(payload):
            key = (urlparse(resp_url).netloc + urlparse(resp_url).path, _signature(lst))
            groups.setdefault(key, []).append((score, lst, resp_url))
    best: Optional[Tuple[float, List[Job], str]] = None
    for (endpoint, _sig), entries in groups.items():
        merged: Dict[str, Job] = {}
        for _score, lst, resp_url in entries:
            for d in lst:
                j = record_to_job(d, page_url, resp_url)
                if j and j.external_id not in merged:
                    merged[j.external_id] = j
        if not merged:
            continue
        score = max(e[0] for e in entries) * (1 + min(len(merged), 300) / 100.0)
        if best is None or score > best[0]:
            best = (score, list(merged.values()), endpoint)
    if not best:
        return [], ""
    return best[1], best[2]


# ---- DOM fallback ----
JOB_HREF = re.compile(
    r"(?i)(/(jobs?|careers?|positions?|openings?|opportunit(?:y|ies)|vacanc(?:y|ies)|requisitions?|postings?|"
    r"job-?details?|job-?posting|roles?|apply)/[^/?#\s]{3,}|[?&](job_?id|jid|gh_jid|req_?id|requisition_?id|"
    r"position_?id|opening_?id|jobreqid)=)")
NOT_JOB_TEXT = re.compile(
    r"(?i)^(apply( now)?|careers?|jobs?|view (all )?(jobs|openings|roles)|see (all )?(jobs|openings|roles)|"
    r"open (positions|roles)|learn more|read more|know more|join us|join our team|sign in|log ?in|register|"
    r"back|next|previous|home|about( us)?|contact( us)?|privacy|terms|search|filter|share|save|email|"
    r"life at .*|our (culture|values|team)|benefits|blog|press|faq)$")
INDIA_CITIES = re.compile(
    r"(?i)\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|gurugram|gurgaon|noida|delhi|new delhi|chennai|kolkata|"
    r"ahmedabad|jaipur|kochi|chandigarh|indore|coimbatore|thiruvananthapuram|mohali|vadodara|nagpur|remote)\b")


def jobs_from_dom_links(anchors: List[Dict[str, Any]], page_url: str) -> List[Job]:
    """anchors: [{href, text, ctx}] from the rendered page. Keeps links that look like individual postings."""
    page_host = urlparse(page_url).netloc
    seen: Dict[str, Job] = {}
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absu = urljoin(page_url, href).split("#")[0]
        pu = urlparse(absu)
        if any(h in pu.netloc for h in ("linkedin.com", "facebook.com", "twitter.com", "instagram.com", "youtube.com",
                                         "glassdoor", "ambitionbox", "play.google", "apps.apple")):
            continue
        if absu.rstrip("/") == page_url.split("#")[0].rstrip("/"):
            continue
        if not JOB_HREF.search(absu):
            continue
        lines = [ln.strip() for ln in re.split(r"[\n\r]+", (a.get("text") or "")) if ln.strip()]
        ctx_lines = [ln.strip() for ln in re.split(r"[\n\r]+", (a.get("ctx") or "")) if ln.strip()]
        title = ""
        for ln in lines:
            if 4 <= len(ln) <= 140 and re.search(r"[A-Za-z]{3}", ln) and not NOT_JOB_TEXT.match(ln):
                title = ln
                break
        if not title:
            continue
        if not JOB_WORDS.search(title) and len(lines) < 2 and not re.search(r"(?i)/jobs?/", absu):
            continue
        loc = ""
        for ln in (lines[1:] + ctx_lines):
            if ln != title and len(ln) <= 90 and INDIA_CITIES.search(ln):
                loc = ln
                break
        ext_id = pu.path.rstrip("/").split("/")[-1] or absu
        if pu.query:
            m = re.search(r"(?i)(?:job_?id|jid|gh_jid|req_?id|requisition_?id|position_?id|opening_?id)=([\w\-]+)", pu.query)
            if m:
                ext_id = m.group(1)
        key = absu
        if key not in seen:
            seen[key] = Job(ext_id, title, absu, loc)
    # same external id twice under different URLs: keep first
    uniq: Dict[str, Job] = {}
    for j in seen.values():
        uniq.setdefault(j.external_id, j)
    return list(uniq.values())


_FOLLOW_TEXT = re.compile(
    r"(?i)(view|see|browse|explore|search|find|check|show)\b.{0,25}\b(jobs?|roles?|openings?|positions?|opportunit(?:y|ies)|vacanc(?:y|ies))"
    r"|^(current |open |all |job )?(openings?|vacanc(?:y|ies)|positions?|opportunit(?:y|ies)|jobs?|roles?)$"
    r"|(current|open|latest) (job )?(openings?|vacanc(?:y|ies)|positions?|roles?)"
    r"|careers? (opportunit(?:y|ies)|openings?)|job (search|board|listings?|openings?)|apply (here|now)$")
_FOLLOW_HREF = re.compile(r"(?i)/(jobs?|careers?|openings?|vacanc(?:y|ies)|positions?|job-?search|search-?jobs?|opportunit(?:y|ies)|current-?openings?)/?(\?.*)?$")
_FOLLOW_SKIP = re.compile(r"(?i)(linkedin|facebook|twitter|instagram|youtube|glassdoor|ambitionbox|naukri|indeed|google\.|apple\.com|"
                          r"\.(pdf|docx?|zip|png|jpe?g)$|privacy|terms|cookie|login|sign-?in|register|my-profile|talent-?community)")


def follow_links(anchors: List[Dict[str, Any]], page_url: str, tried: Any = (), limit: int = 3) -> List[str]:
    """Links on a careers landing page that probably lead to the actual job list ('View all jobs', '/jobs/', ...)."""
    page_host = urlparse(page_url).netloc.lower()
    here = page_url.split("#")[0].rstrip("/")
    scored: List[Tuple[int, str]] = []
    seen = set(tried) | {here}
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absu = urljoin(page_url, href).split("#")[0]
        key = absu.rstrip("/")
        pu = urlparse(absu)
        if key in seen or pu.scheme not in ("http", "https") or _FOLLOW_SKIP.search(absu):
            continue
        text = " ".join((a.get("text") or "").split())[:80]
        by_text = bool(text and _FOLLOW_TEXT.search(text))
        by_href = bool(_FOLLOW_HREF.search(pu.path + ("?" + pu.query if pu.query else "")))
        if not (by_text or by_href):
            continue
        same = pu.netloc.lower() == page_host or detect_platform_in_urls([absu]) is not None
        if not same and not by_text:
            continue
        score = (3 if by_text else 0) + (2 if by_href else 0) + (1 if same else 0)
        seen.add(key)
        scored.append((score, absu))
    scored.sort(key=lambda t: -t[0])
    return [u for _, u in scored[:limit]]


def detect_platform_in_urls(urls: List[str]) -> Optional[Dict[str, str]]:
    from ..detect import _match_patterns
    for u in urls:
        hit = _match_patterns(u)
        if hit:
            return hit
    return None


# ----------------------------------------------------------------------------------------------
# Browser plumbing
# ----------------------------------------------------------------------------------------------
class _Runner:
    """Owns Playwright and Chrome on a single thread; other threads submit work and wait."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Tuple[Callable[[Any], Any], Future]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pw: Any = None
        self._browser: Any = None
        self.launch_error: Optional[str] = None

    def submit(self, fn: Callable[[Any], Any], timeout: float = TASK_TIMEOUT_SECONDS) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="browser-runner", daemon=True)
                self._thread.start()
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result(timeout=timeout)

    def _loop(self) -> None:
        while True:
            try:
                fn, fut = self._q.get(timeout=IDLE_CLOSE_SECONDS)
            except queue.Empty:
                self._close()
                continue
            try:
                if not fut.set_running_or_notify_cancel():
                    continue
                browser = self._ensure()
                fut.set_result(fn(browser))
            except BaseException as e:       # noqa: BLE001 - hand every failure back to the caller
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if "closed" in str(e).lower() or "crash" in str(e).lower():
                    self._close()
                try:
                    fut.set_exception(e)
                except Exception:
                    pass

    def _ensure(self) -> Any:
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return self._browser
            except Exception:
                pass
            self._close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise Unsupported("Playwright is not installed. Run `make browser-setup`, then restart the service.")
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--disable-gpu"]
        if os.environ.get("JOBHUNTER_NO_SANDBOX"):
            args.append("--no-sandbox")
        attempts: List[Dict[str, Any]] = []
        env_path = os.environ.get("JOBHUNTER_CHROME_PATH", "").strip()
        if env_path:
            attempts.append({"executable_path": env_path})
        for p in MAC_BROWSERS:
            if os.path.exists(p):
                attempts.append({"executable_path": p})
        attempts.append({"channel": "chrome"})
        attempts.append({})                  # Playwright's own Chromium, if installed
        last = ""
        for kw in attempts:
            try:
                self._browser = self._pw.chromium.launch(headless=True, args=args, **kw)
                log.info("browser started (%s)", kw or "bundled chromium")
                return self._browser
            except Exception as e:           # try the next option
                last = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        self._stop_pw()
        raise Unsupported("No Chrome found for the browser collector. Install Google Chrome or run "
                          "`make browser-setup`. (%s)" % last)

    def _stop_pw(self) -> None:
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    def _close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._stop_pw()

    def shutdown(self) -> None:
        if self._thread and self._thread.is_alive():
            try:
                self.submit(lambda _b: self._close(), timeout=15)
            except Exception:
                pass


_runner = _Runner()
atexit.register(_runner.shutdown)

_GATE = threading.Semaphore(1)

_ANCHOR_JS = """
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    const href = a.href;
    if (!href || seen.has(href)) return;
    seen.add(href);
    let ctx = '';
    let el = a.closest('li, tr, article, [class*="card" i], [class*="job" i], [class*="position" i], [class*="opening" i]');
    if (el && el.innerText) ctx = el.innerText.slice(0, 400);
    out.push({href: href, text: (a.innerText || a.textContent || '').trim().slice(0, 400), ctx: ctx});
  });
  return out;
}
"""

_LOAD_MORE_JS = """
() => {
  const re = /^(load|show|view|see|get)\\s+(more|all)|^more\\s+(jobs|roles|openings|results)|^next(\\s+page)?$|^›$|^»$/i;
  const els = Array.from(document.querySelectorAll('button, a, [role=button]'));
  for (const el of els) {
    const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
    if (!t || t.length > 30 || !re.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    el.scrollIntoView({block: 'center'});
    el.click();
    return t;
  }
  return '';
}
"""


def _new_page(browser: Any, block: bool = True) -> Tuple[Any, Any]:
    ctx = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 900},
                              java_script_enabled=True)

    def _route(route: Any) -> None:
        if route.request.resource_type in ("image", "media", "font"):
            route.abort()
        else:
            route.continue_()

    if block:
        ctx.route("**/*", _route)
    page = ctx.new_page()
    page.set_default_timeout(15000)
    return ctx, page


_RETRY_NAV = ("Timeout", "ERR_NAME_NOT_RESOLVED", "ERR_NETWORK_CHANGED", "ERR_CONNECTION", "ERR_HTTP2",
              "ERR_EMPTY_RESPONSE", "ERR_TIMED_OUT", "ERR_INTERNET_DISCONNECTED")


def _load(page: Any, url: str) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception as e:
        if not any(k in str(e) for k in _RETRY_NAV):
            raise
        page.wait_for_timeout(2500)                      # a dropped connection often works on the second try
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def _safe_eval(page: Any, js: str, default: Any = None) -> Any:
    """page.evaluate that survives a redirect in progress ('Execution context was destroyed') and sites whose own
    scripts break evaluation; the page is simply read as empty in that case."""
    for attempt in range(3):
        try:
            return page.evaluate(js)
        except Exception as e:
            if "Execution context was destroyed" in str(e) or "navigation" in str(e).lower():
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                continue
            log.debug("page.evaluate failed: %s", str(e)[:160])
            return default
    return default


def scrape_page(browser: Any, url: str) -> Dict[str, Any]:
    """Open the page, trigger lazy loading, and return everything the pure helpers need."""
    ctx, page = _new_page(browser)
    responses: List[Tuple[str, Any]] = []
    req_urls: List[str] = []
    frame_urls: List[str] = []

    def on_request(req: Any) -> None:
        req_urls.append(req.url)

    def on_response(resp: Any) -> None:
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            cl = int(resp.headers.get("content-length") or 0)
            if cl > MAX_JSON_BYTES:
                return
            responses.append((resp.url, resp.json()))
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        _load(page, url)
        final_url = page.url
        last_total = -1
        for _ in range(MAX_LOAD_MORE_ROUNDS):
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                clicked = page.evaluate(_LOAD_MORE_JS)
            except Exception:
                clicked = ""
            page.wait_for_timeout(1200)
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            jobs_now, _ = jobs_from_json_responses(responses, final_url)
            dom_now = len(jobs_from_dom_links(_safe_eval(page, _ANCHOR_JS, []) or [], final_url)) if not jobs_now else 0
            total = len(jobs_now) + dom_now
            if not clicked and total == last_total:
                break
            if total == last_total and clicked:
                break
            last_total = total
        anchors = _safe_eval(page, _ANCHOR_JS, []) or []
        for fr in page.frames:
            frame_urls.append(fr.url)
        try:
            title = page.title()
        except Exception:
            title = ""
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return {"final_url": final_url, "responses": responses, "anchors": anchors, "req_urls": req_urls,
            "frame_urls": frame_urls, "title": title}


def probe_page(browser: Any, url: str, shot: str = "", wait_ms: int = 15000, block: bool = True) -> Dict[str, Any]:
    """Diagnostic for `jh probe`: open the page like the collector does, wait longer, and report what it saw."""
    ctx, page = _new_page(browser, block)
    reqs: List[Dict[str, str]] = []
    js: List[Dict[str, Any]] = []
    errors: List[str] = []
    failed: List[str] = []
    console: List[str] = []
    page.on("requestfailed", lambda r: failed.append(f"{r.resource_type} {r.url[:130]} -> {(r.failure or '')}"))
    page.on("console", lambda m: console.append(f"{m.type}: {m.text[:160]}") if m.type in ("error", "warning") else None)

    def on_response(resp: Any) -> None:
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            rt = resp.request.resource_type
            if rt in ("xhr", "fetch", "document", "script") or "json" in ct:
                reqs.append({"status": str(resp.status), "type": rt, "method": resp.request.method, "url": resp.url[:200]})
            if "json" in ct:
                try:
                    body = resp.json()
                    js.append({"url": resp.url[:200], "size": len(json.dumps(body)) if body is not None else 0})
                except Exception:
                    pass
        except Exception:
            pass

    page.on("response", on_response)
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    try:
        _load(page, url)
        page.wait_for_timeout(wait_ms)
        text = page.evaluate("() => (document.body && document.body.innerText) || ''")
        anchors = page.evaluate(_ANCHOR_JS)
        out = {"final_url": page.url, "title": page.title(), "text_len": len(text), "text": text[:1200],
               "anchors": len(anchors), "sample_links": [(a["text"][:60], a["href"][:120]) for a in anchors[:15]],
               "requests": reqs[:60], "json": js[:30], "frames": [f.url for f in page.frames][:10], "errors": errors[:5],
               "failed": failed[:15], "console": console[:12],
               "env": page.evaluate("() => ({webdriver: navigator.webdriver, ua: navigator.userAgent})"),
               "html_len": len(page.content())}
        if shot:
            try:
                page.screenshot(path=shot, full_page=False)
                out["screenshot"] = shot
            except Exception:
                pass
        return out
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def page_text(browser: Any, url: str) -> str:
    ctx, page = _new_page(browser)
    try:
        _load(page, url)
        txt = page.evaluate("() => (document.querySelector('main') || document.body).innerText || ''")
        return (txt or "")[:20000]
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def extract_jobs(scraped: Dict[str, Any]) -> Tuple[List[Job], str, Optional[Dict[str, str]]]:
    """Turn scrape_page() output into (jobs, how, platform_hint)."""
    page_url = scraped["final_url"]
    try:
        jobs, endpoint = jobs_from_json_responses(scraped["responses"], page_url)
    except (TypeError, AttributeError) as e:          # an unexpected shape in some site's JSON: fall back to the page itself
        log.warning("could not read a data feed from %s (%s); using the rendered page", page_url, e)
        jobs, endpoint = [], ""
    how = ""
    if jobs:
        how = f"read {len(jobs)} jobs from the page's data feed ({endpoint})"
    else:
        jobs = jobs_from_dom_links(scraped["anchors"], page_url)
        if jobs:
            how = f"read {len(jobs)} job links from the rendered page"
    # platform hints
    hint = detect_platform_in_urls(scraped["req_urls"] + scraped["frame_urls"])
    anchor_hint = None
    if not hint:
        anchor_hint = detect_platform_in_urls([a.get("href", "") for a in scraped["anchors"]])
    if not hint and anchor_hint and not jobs:
        hint = anchor_hint
    return jobs, how, hint


class BrowserCollector(Collector):
    name = "browser"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        url = (company.get("careers_url") or company.get("slug_or_endpoint") or "").strip()
        if not url.startswith("http"):
            raise CollectorError("no careers page URL to open")
        host = urlparse(url).netloc.lower()
        if "linkedin.com" in host:
            raise Unsupported("LinkedIn is not scraped (its terms forbid it); use the referral pack instead")
        with _GATE:
            scraped = _runner.submit(lambda b: scrape_page(b, url))
        tried = {url.split("#")[0].rstrip("/")}
        followed: List[str] = []
        for hop in range(MAX_HOPS + 1):
            jobs, how, hint = extract_jobs(scraped)
            res = self._via_platform(company, hint, jobs, client)
            if res is not None:
                return res
            if jobs:
                # A browser read is a snapshot: never use it to mark other jobs as closed.
                return FetchResult(jobs, complete=False, note=how + (f" (after opening {followed[-1]})" if followed else ""))
            if hop == MAX_HOPS:
                break
            nxt = follow_links(scraped["anchors"], scraped["final_url"], tried)
            if not nxt:
                break
            tried.add(nxt[0].rstrip("/"))
            followed.append(nxt[0])
            log.info("%s: no job list on the landing page; opening %s", company.get("name"), nxt[0])
            with _GATE:
                scraped = _runner.submit(lambda b, u=nxt[0]: scrape_page(b, u))
        raise CollectorError(
            "opened the page but found no job list (%d JSON responses, %d links%s). The site may need a custom "
            "reader, a search step, or it blocks automated browsers." % (
                len(scraped["responses"]), len(scraped["anchors"]),
                f"; also tried {', '.join(followed)}" if followed else ""))

    def _via_platform(self, company: Mapping[str, Any], hint: Optional[Dict[str, str]], jobs: List[Job],
                      client: PoliteClient) -> Optional[FetchResult]:
        """If the page clearly sits on a known hiring platform, read it with that platform's API collector."""
        if not hint or (hint["source_type"], hint["slug_or_endpoint"]) == (company.get("source_type") or "",
                                                                          company.get("slug_or_endpoint") or ""):
            return None
        upgraded = _upgrade_company(company, hint)
        if not upgraded:
            return None
        from .registry import collector_for
        try:
            res = collector_for(upgraded).list_jobs(upgraded, client)
            if res.jobs or not jobs:
                res.note = f"switched to {hint['source_type']} ({hint['slug_or_endpoint']}); " + (res.note or "")
                return res
        except Exception as e:
            log.warning("%s: detected %s but its collector failed (%s); using browser result",
                        company.get("name"), hint["source_type"], e)
        return None

    def fetch_detail(self, job: Job, company: Mapping[str, Any], client: PoliteClient) -> str:
        if not job.url.startswith("http") or "#" in job.url and job.url.split("#")[0].rstrip("/") == \
                (company.get("careers_url") or "").rstrip("/"):
            return ""
        with _GATE:
            return _runner.submit(lambda b: page_text(b, job.url), timeout=90)


def _upgrade_company(company: Mapping[str, Any], hint: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Switch the company to a faster API collector when the page clearly uses a known platform."""
    try:
        from .. import db
        with db.connect() as c:
            c.execute("UPDATE companies SET source_type=?, slug_or_endpoint=?, "
                      "notes=CASE WHEN notes='' THEN ? ELSE notes || ' | ' || ? END WHERE id=?",
                      (hint["source_type"], hint["slug_or_endpoint"], "auto-switched from browser",
                       "auto-switched from browser", company["id"]))
        new = dict(company)
        new["source_type"], new["slug_or_endpoint"] = hint["source_type"], hint["slug_or_endpoint"]
        log.info("%s: switched to %s (%s)", company.get("name"), hint["source_type"], hint["slug_or_endpoint"])
        return new
    except Exception as e:
        log.warning("could not switch %s to %s: %s", company.get("name"), hint.get("source_type"), e)
        return None


def available() -> Tuple[bool, str]:
    """For `make doctor`: can the browser collector start?"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "Playwright not installed (run `make browser-setup`)"
    try:
        _runner.submit(lambda b: b.version, timeout=60)
        return True, "ready"
    except Unsupported as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
