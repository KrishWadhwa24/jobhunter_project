"""One cycle: fetch every due company, evaluate jobs, store them, report what to alert."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import db
from .collectors.base import Unsupported
from .collectors.registry import collector_for
from .filters import Evaluator
from .models import Job
from .util import PoliteClient, is_network_error, parse_iso, utcnow

log = logging.getLogger("jobhunter.pipeline")
DETAIL_FETCH_CAP = 40      # per company per cycle


@dataclass
class CompanyOutcome:
    company: str
    status: str = "ok"          # ok | error | skipped
    error: str = ""
    fetched: int = 0
    in_scope: int = 0
    new_in_scope: int = 0
    relevant: int = 0           # verdict == match


@dataclass
class CycleStats:
    started_at: str = ""
    companies_checked: int = 0
    ok: int = 0
    errors: int = 0
    skipped: int = 0
    new_jobs: int = 0
    health_warnings: List[str] = field(default_factory=list)
    outcomes: List[CompanyOutcome] = field(default_factory=list)


def _row_to_company(row: Any) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _store_eval(c: Any, job_id: int, ev: Any) -> None:
    c.execute(
        "UPDATE jobs SET in_scope=?, level=?, verdict=?, score=?, reasons=?, flags=? WHERE id=?",
        (1 if ev.in_scope else 0, ev.level, ev.verdict, ev.score, json.dumps(ev.reasons),
         json.dumps(ev.flags, default=str), job_id))


def _where(e: BaseException) -> str:
    """For a programming error (not a network or site problem): where in our code it happened, so it can be fixed."""
    if not isinstance(e, (TypeError, AttributeError, KeyError, ValueError, IndexError)):
        return ""
    import traceback
    from pathlib import Path
    frames = [f for f in traceback.extract_tb(e.__traceback__) if "jobhunter" in f.filename.replace("\\", "/")]
    if not frames:
        return ""
    f = frames[-1]
    return f" [at {Path(f.filename).name}:{f.lineno} in {f.name}]"


def _browser_fallback(company: Dict[str, Any], client: PoliteClient, err: Exception) -> Optional[Any]:
    """An API source failed (wrong slug, moved platform, blocked): try the careers page in the browser, which can
    also discover the right platform/slug and switch the company to it."""
    st = (company.get("source_type") or "").lower()
    url = (company.get("careers_url") or "").strip()
    if st in ("browser", "html", "rss", "linkedin_only") or not url.startswith("http"):
        return None
    try:
        from .collectors.browser import BrowserCollector
        bc = BrowserCollector()
        res = bc.list_jobs(company, client)
        res.note = (f"{st} API failed ({type(err).__name__}); " + (res.note or "")).strip()
        return res, bc
    except Exception as e2:
        log.info("%s: API failed (%s) and browser fallback failed (%s)", company.get("name"), err, e2)
        return None


def process_company(company: Dict[str, Any], evaluator: Evaluator, client: PoliteClient,
                    cycle_start: str) -> CompanyOutcome:
    out = CompanyOutcome(company["name"])
    cid = company["id"]
    prev_count = company.get("last_job_count") or 0
    prev_status = company.get("last_status")
    try:
        collector = collector_for(company)
        res = collector.list_jobs(company, client)
    except Unsupported as e:
        out.status, out.error = "skipped", str(e)
        db.update_company_health(cid, "skipped", str(e))
        return out
    except Exception as e:  # network, parsing, 404 slug...
        res = _browser_fallback(company, client, e)
        if res is None:
            out.error = f"{type(e).__name__}: {e}{_where(e)}"[:300]
            if isinstance(e, (TypeError, AttributeError, KeyError, ValueError, IndexError)):
                log.warning("unexpected %s while reading %s", type(e).__name__, company["name"], exc_info=e)
            out.status = "network" if is_network_error(e) else "error"
            db.update_company_health(cid, out.status, out.error)
            return out
        collector = res[1]
        res = res[0]

    out.fetched = len(res.jobs)
    tier = company.get("pay_tier", "")
    detail_budget = DETAIL_FETCH_CAP
    try:
        # 1) network work first, with no database transaction open (a slow site must never block the dashboard)
        with db.connect() as c0:
            known = {r["external_id"] for r in c0.execute(
                "SELECT external_id FROM jobs WHERE company_id=?", (cid,)).fetchall()}
        details: Dict[str, Any] = {}
        for job in res.jobs:
            if not job.external_id or not job.title or job.external_id in known or job.description:
                continue
            pre = evaluator.prefilter(job.title, job.location)
            if not pre.in_scope or not pre.needs_detail or detail_budget <= 0:
                continue
            detail_budget -= 1
            try:
                details[job.external_id] = collector.fetch_detail(job, company, client) or ""
            except Exception as e:
                log.warning("detail fetch failed for %s / %s: %s", company["name"], job.title, e)
        # 2) then store everything in one short transaction
        with db.connect() as c:
            for job in res.jobs:
                if not job.external_id or not job.title:
                    continue
                row = c.execute("SELECT id, in_scope, detail_fetched, description FROM jobs "
                                "WHERE company_id=? AND external_id=?", (cid, job.external_id)).fetchone()
                if row:
                    c.execute("UPDATE jobs SET last_seen_at=?, is_open=1, title=?, url=?, "
                              "location=CASE WHEN ?<>'' THEN ? ELSE location END WHERE id=?",
                              (utcnow(), job.title, job.url or "", job.location, job.location, row["id"]))
                    if row["in_scope"]:
                        out.in_scope += 1
                    continue
                pre = evaluator.prefilter(job.title, job.location)
                if not pre.in_scope:
                    continue
                desc = job.description or details.get(job.external_id, "")
                fetched_detail = 1 if (not job.description and desc) else 0
                ev = evaluator.evaluate(job.title, job.location, desc, tier)
                if not ev.in_scope:      # location refined by detail fetch
                    continue
                now = utcnow()
                new_id = c.insert(
                    "INSERT INTO jobs(company_id, external_id, title, url, location, description, posted_at, "
                    "first_seen_at, last_seen_at, detail_fetched) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cid, job.external_id, job.title, job.url or "", job.location, desc[:20000], job.posted_at,
                     now, now, 1 if (job.description or fetched_detail) else 0))
                _store_eval(c, new_id, ev)
                out.in_scope += 1
                out.new_in_scope += 1
                if ev.verdict == "match":
                    out.relevant += 1
            if res.complete and res.jobs:
                seen_ids = {j.external_id for j in res.jobs}
                open_rows = c.execute("SELECT id, external_id FROM jobs WHERE company_id=? AND is_open=1",
                                      (cid,)).fetchall()
                gone = [r["id"] for r in open_rows if r["external_id"] not in seen_ids]
                for i in range(0, len(gone), 500):
                    chunk = gone[i:i + 500]
                    c.execute("UPDATE jobs SET is_open=0 WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)
            relevant_total = c.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE company_id=? AND is_open=1 AND in_scope=1 AND verdict='match'",
                (cid,)).fetchone()["n"]
        out.relevant = relevant_total
    except Exception as e:
        out.status, out.error = "error", f"store failed: {type(e).__name__}: {e}"[:300]
        db.update_company_health(cid, "error", out.error)
        return out

    db.update_company_health(cid, "ok", res.note, out.fetched, out.relevant)
    out.status = "ok"
    # A previously healthy source that suddenly returns nothing probably broke.
    if prev_status == "ok" and prev_count > 0 and out.fetched == 0:
        out.error = "returned 0 jobs (previously %d)" % prev_count
    return out


def _fill_missing_details(evaluator: Evaluator, client: PoliteClient, companies: Dict[int, Dict[str, Any]],
                          budget: int = 60) -> None:
    """Jobs stored without a description (e.g. Workday) whose level is a target or unknown."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT j.*, co.pay_tier AS tier FROM jobs j JOIN companies co ON co.id=j.company_id "
            "WHERE j.in_scope=1 AND j.is_open=1 AND j.detail_fetched=0 AND j.description='' "
            "ORDER BY j.first_seen_at DESC LIMIT ?", (budget * 3,)).fetchall()
    done = 0
    for r in rows:
        if done >= budget:
            break
        if r["level"] not in ("", "unknown") and r["level"] not in evaluator.targets:
            continue
        comp = companies.get(r["company_id"])
        if not comp:
            continue
        try:
            collector = collector_for(comp)
            job = Job(r["external_id"], r["title"], r["url"], r["location"],
                      extra={"path": _workday_path(r["url"], comp)})
            desc = collector.fetch_detail(job, comp, client)
        except Exception:
            desc = ""
        done += 1
        with db.connect() as c:
            c.execute("UPDATE jobs SET detail_fetched=1, description=? WHERE id=?", (desc[:20000], r["id"]))
            if desc:
                ev = evaluator.evaluate(r["title"], r["location"], desc, r["tier"])
                _store_eval(c, r["id"], ev)


def _workday_path(url: str, comp: Dict[str, Any]) -> str:
    if comp.get("source_type") != "workday":
        return ""
    from .collectors.workday import parse_base
    _, _, site = parse_base(comp["slug_or_endpoint"])
    marker = f"/{site}/"
    i = url.find(marker)
    return url[i + len(site) + 1:] if i >= 0 else ""


def _min_hours(company: Dict[str, Any], cfg: Dict[str, Any]) -> float:
    sch = cfg["schedule"]
    if (company.get("source_type") or "").lower() in ("browser", "html", "rss", "linkedin_only"):
        return float(sch.get("browser_min_hours", 6))
    return float(sch["per_company_min_hours"])


def retry_hours(company: Dict[str, Any], hours: float) -> float:
    """How long to wait before checking again. A failed check is retried sooner than a good one: connection problems after
    10 minutes, other errors after 15 min, 30 min, 1 h ... backing off up to the normal wait."""
    status = company.get("last_status")
    if status == "network":
        return min(hours, 10 / 60)
    if status == "error":
        fails = max(1, int(company.get("consecutive_failures") or 1))
        return min(hours, 0.25 * 2 ** min(fails - 1, 5))
    return hours


def due(company: Dict[str, Any], hours: float, force: bool) -> bool:
    if force or not company.get("last_fetch_at"):
        return True
    last = parse_iso(company["last_fetch_at"])
    return not last or datetime.now(timezone.utc) - last >= timedelta(hours=retry_hours(company, hours))


def run_cycle(cfg: Dict[str, Any], force: bool = False, only: Optional[str] = None,
              client: Optional[PoliteClient] = None) -> CycleStats:
    stats = CycleStats(started_at=utcnow())
    evaluator = Evaluator(cfg)
    from .collectors import search as _search
    _search.set_from_config(cfg)
    own_client = client is None
    client = client or PoliteClient()
    rows = db.list_companies(active_only=True)
    companies = {r["id"]: _row_to_company(r) for r in rows}
    todo = [c for c in companies.values()
            if (only is None or c["name"].lower() == only.lower())
            and due(c, _min_hours(c, cfg), force or only is not None)]
    workers = max(1, int(cfg["schedule"].get("workers", 8)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(process_company, c, evaluator, client, stats.started_at): c for c in todo}
            for f in as_completed(futs):
                c = futs[f]
                try:
                    o = f.result()
                except Exception as e:      # never let one company kill the cycle
                    o = CompanyOutcome(c["name"], "error", f"{type(e).__name__}: {e}")
                    db.update_company_health(c["id"], "error", o.error)
                stats.outcomes.append(o)
                stats.companies_checked += 1
                stats.new_jobs += o.new_in_scope
                if o.status == "ok":
                    stats.ok += 1
                elif o.status == "skipped":
                    stats.skipped += 1
                else:
                    stats.errors += 1
        _fill_missing_details(evaluator, client, companies)
    finally:
        if own_client:
            client.close()
    # Health warnings (once per outage): 3 consecutive failures, or a sudden empty feed.
    for o in stats.outcomes:
        crow = next((c for c in companies.values() if c["name"] == o.company), None)
        if not crow:
            continue
        fresh = db.get_company(crow["id"])
        if o.status == "error" and fresh["consecutive_failures"] >= 3 and not fresh["health_alerted"]:
            stats.health_warnings.append(f"{o.company}: failing for {fresh['consecutive_failures']} checks ({o.error[:120]})")
            db.mark_health_alerted(crow["id"])
        elif o.status == "ok" and o.error.startswith("returned 0 jobs"):
            stats.health_warnings.append(f"{o.company}: {o.error}")
    db.kv_set("last_cycle_at", utcnow())
    db.kv_set("last_cycle_summary", json.dumps({
        "checked": stats.companies_checked, "ok": stats.ok, "errors": stats.errors, "skipped": stats.skipped,
        "new_jobs": stats.new_jobs}))
    return stats


def pending_alerts(cfg: Dict[str, Any]) -> List[Any]:
    a = cfg["alerts"]
    with db.connect() as c:
        rows = c.execute(
            "SELECT j.*, co.name AS company, co.pay_tier AS tier FROM jobs j JOIN companies co ON co.id=j.company_id "
            "WHERE j.in_scope=1 AND j.is_open=1 AND j.verdict='match' AND j.state='new' AND j.alerted_at IS NULL "
            "AND j.score>=? ORDER BY j.score DESC, j.first_seen_at DESC", (int(a["min_score"]),)).fetchall()
    targets = set(cfg["targets"]["levels"])
    rows = [r for r in rows if r["level"] in targets]            # alerts only for the levels you are looking for
    if not a.get("include_interns"):
        rows = [r for r in rows if r["level"] != "intern"]
    return rows


def mark_alerted(job_id: int) -> None:
    with db.connect() as c:
        c.execute("UPDATE jobs SET alerted_at=? WHERE id=?", (utcnow(), job_id))


def reevaluate_all(cfg: Dict[str, Any]) -> Dict[str, int]:
    """Re-run the filter over stored jobs after a config change (new target levels etc.)."""
    evaluator = Evaluator(cfg)
    changed = total = 0
    with db.connect() as c:
        rows = c.execute("SELECT j.*, co.pay_tier AS tier FROM jobs j JOIN companies co ON co.id=j.company_id").fetchall()
        for r in rows:
            total += 1
            ev = evaluator.evaluate(r["title"], r["location"], r["description"], r["tier"])
            if (ev.in_scope, ev.level, ev.verdict, ev.score) != (bool(r["in_scope"]), r["level"], r["verdict"], r["score"]):
                changed += 1
            _store_eval(c, r["id"], ev)
    return {"total": total, "changed": changed}
