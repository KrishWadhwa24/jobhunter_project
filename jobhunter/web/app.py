from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import actions, auth, catalogue, config, db, evergreen, importer, pipeline
from ..apply import worker as apply_worker
from ..resume import importer as resume_importer
from ..resume import llm as llmmod
from ..resume import runner
from ..collectors.registry import collector_for
from ..detect import detect_source
from ..filters import Evaluator
from ..util import PoliteClient, is_network_error, parse_iso
from . import security

HERE = Path(__file__).parent
MAX_BODY = 16 * 1024 * 1024           # largest request accepted at all
MAX_RESUME = 8 * 1024 * 1024
MAX_CSV = 3 * 1024 * 1024
KNOWN_SOURCES = {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "jibe", "kula", "eightfold",
                 "oracle_hcm", "recruiterbox", "custom_json", "browser", "html", "rss", "linkedin_only"}
LEVEL_LABELS = dict(config.LEVEL_LABELS, unknown="Unclear")
TAB_STATES = {
    "new": ["new"],
    "progress": ["referral", "apply_queued"],
    "applied": ["applied"],
    "hidden": ["hidden"],
}
TAB_LABELS = [("new", "New"), ("progress", "In progress"), ("applied", "Applied"), ("hidden", "Hidden")]


def time_ago(iso: Optional[str]) -> str:
    d = parse_iso(iso)
    if not d:
        return "never"
    s = int((datetime.now(timezone.utc) - d).total_seconds())
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def _words(text: str) -> List[str]:
    return [x.strip() for x in re.split(r"[\n,;]", text or "") if x.strip()]


def create_app(cfg: Dict[str, Any], run_cycle_now: Optional[Callable[..., bool]] = None,
               cycle_running: Optional[Callable[[], bool]] = None, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="Jobhunter", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["ago"] = time_ago
    templates.env.filters["level"] = lambda v: LEVEL_LABELS.get(v or "", v or "")
    templates.env.filters["jsonload"] = lambda s: db.loads(s, [])
    templates.env.filters["urlq"] = quote
    templates.env.filters["kind"] = lambda k: str(k).replace("-", " ").capitalize().replace("Ai ", "AI ")
    templates.env.globals["LEVEL_LABELS"] = LEVEL_LABELS

    limiter = security.LoginLimiter()
    checks_running: Dict[int, str] = {}              # company id -> name, for background "Check" runs

    @app.middleware("http")
    async def guard(request: Request, call_next):
        headers, path = request.headers, request.url.path
        via_gateway = security.from_gateway(headers)
        remote = bool(via_gateway) or security.is_remote(headers)
        request.state.remote = remote
        request.state.user = None
        gated = remote or config.REQUIRE_LOGIN
        secure = remote and security.is_https(headers, request.url.scheme)

        def done(resp: Response) -> Response:
            for k, v in security.security_headers(secure).items():
                resp.headers.setdefault(k, v)
            if not path.startswith("/static/"):
                resp.headers["Cache-Control"] = "no-store"
            return resp

        try:
            if int(headers.get("content-length") or 0) > MAX_BODY:
                return done(PlainTextResponse("That upload is too large.", status_code=413))
        except ValueError:
            return done(PlainTextResponse("Bad request.", status_code=400))
        if via_gateway:
            # The gateway has already signed this person in and checked the request's origin.
            u = auth.get_user(via_gateway)
            if not u or u["profile"] != config.PROFILE:
                return done(PlainTextResponse("This account does not have access to this tracker.", status_code=403))
            request.state.user = u
            return done(await call_next(request))
        if request.method not in ("GET", "HEAD", "OPTIONS") and not security.same_origin(headers):
            return done(PlainTextResponse("Blocked: this request did not come from the site itself.", status_code=403))
        if gated and path != "/healthz" and not path.startswith("/static/"):
            if not auth.count_for(config.PROFILE):
                return done(_message_page("Sign-in is not set up",
                                          "Nobody has been given access to this dashboard yet, so it cannot be opened from "
                                          "outside the computer it runs on. On that computer run "
                                          "./jh users add EMAIL --profile NAME, then try again.", 503))
            request.state.user = security.session_user(request.cookies.get(security.cookie_name(), ""))
            if not security.is_public_path(path):
                if not request.state.user:
                    if request.method in ("GET", "HEAD"):
                        nxt = path + (("?" + request.url.query) if request.url.query else "")
                        return done(RedirectResponse(f"/login?next={quote(nxt)}", status_code=303))
                    return done(RedirectResponse("/login", status_code=303))
        return done(await call_next(request))

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> Response:
        return _message_page("Page not found", "That page does not exist.", 404)

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception) -> Response:
        import logging
        logging.getLogger("jobhunter.web").exception("unhandled error on %s", request.url.path)
        return _message_page("Something went wrong", "That did not work. Go back and try again; if it keeps "
                             "happening, tell Krish.", 500)

    def level_keys() -> List[str]:
        return list(config.level_scheme(cfg)["levels"])

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        counts = db.job_counts()
        remote = bool(getattr(request.state, "remote", False))
        profiles = [] if remote else config.list_profiles()         # never show other people's profiles remotely
        base = {
            "request": request, "counts": counts,
            "last_cycle": db.kv_get("last_cycle_at"),
            "running": bool(cycle_running and cycle_running()),
            "targets": cfg["targets"]["levels"],
            "levels_all": level_keys() + ["unknown"],
            "profile_name": (cfg.get("profile") or {}).get("name") or "Jobhunter",
            "profile_slug": config.PROFILE,
            "profiles": profiles,
            "has_evergreen": bool(evergreen.load()),
            "has_resume": config.FACTS_PATH.exists(),
            "nav": request.url.path,
            "remote": remote,
            "gated": remote or config.REQUIRE_LOGIN,
            "user_email": (getattr(request.state, "user", None) or {}).get("email", ""),
        }
        base.update(ctx)
        return templates.TemplateResponse(request, name, base)

    def apply_settings(update: Callable[[Dict[str, Any]], None]) -> None:
        """Write the change to settings.yaml, reload the live config, re-score stored jobs and re-check companies."""
        data = config.read_settings()
        update(data)
        config.save_settings(data)
        fresh = config.load_config()
        cfg.clear()
        cfg.update(fresh)
        try:
            pipeline.reevaluate_all(cfg)
        except Exception:
            pass
        if run_cycle_now:
            try:
                run_cycle_now(force=True)
            except TypeError:
                run_cycle_now()

    # ---------------- jobs ----------------
    @app.get("/", response_class=HTMLResponse)
    def jobs(request: Request, tab: str = "new", show: str = "matches", levels: str = "", q: str = "",
             company: int = 0) -> HTMLResponse:
        tab = tab if tab in TAB_STATES else "new"
        show = show if show in ("matches", "maybe", "all") else "matches"
        valid = level_keys() + ["unknown"]
        if "levels" in request.query_params:         # present but empty means "every level"
            selected = [x for x in levels.split(",") if x in valid] or list(valid)
        else:
            selected = list(cfg["targets"]["levels"])
        verdicts = {"matches": ["match"], "maybe": ["match", "maybe"], "all": []}[show]
        rows = db.query_jobs(TAB_STATES[tab], selected, verdicts, company_id=company or None, search=q.strip())
        return page(request, "jobs.html", rows=rows, tab=tab, show=show, selected=selected, q=q,
                    tabs=TAB_LABELS)

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int) -> HTMLResponse:
        job = db.get_job(job_id)
        if not job:
            return RedirectResponse("/", status_code=303)
        from .. import referrers
        pack = referrers.build_pack(job, cfg.get("profile", {})) if job["state"] in ("referral", "apply_queued", "new") else None
        return page(request, "job.html", job=job, events=db.job_events(job_id),
                    flags=db.loads(job["flags"], {}), pack=pack, applying=request.query_params.get("applying") == "1",
                    resume=db.get_resume(job_id), resume_running=runner.is_running(job_id),
                    application=db.get_application(job_id), apply_running=apply_worker.is_running(job_id),
                    show_pack=request.query_params.get("referrers") == "1"
                    or job["state"] == "referral")

    @app.post("/job/{job_id}/tailor")
    def job_tailor(request: Request, job_id: int) -> RedirectResponse:
        if db.get_job(job_id):
            runner.start(job_id, cfg, notify=not request.state.remote)
        return RedirectResponse(f"/job/{job_id}?applying=1", status_code=303)

    @app.post("/job/{job_id}/autofill")
    def job_autofill(request: Request, job_id: int) -> RedirectResponse:
        if db.get_job(job_id) and not request.state.remote:        # auto-fill drives Chrome on the laptop: local only
            apply_worker.start(job_id, cfg)
        return RedirectResponse(f"/job/{job_id}?applying=1", status_code=303)

    @app.get("/job/{job_id}/cover.pdf")
    def job_cover(job_id: int):
        from fastapi.responses import FileResponse
        a = db.get_application(job_id)
        path = ((a or {}).get("report") or {}).get("cover_pdf") or ""
        try:
            ok = bool(path) and Path(path).resolve().is_relative_to(config.DATA_DIR.resolve()) and Path(path).exists()
        except Exception:
            ok = False
        if not ok:
            return RedirectResponse(f"/job/{job_id}", status_code=303)
        return FileResponse(path, media_type="application/pdf", filename=Path(path).name)

    @app.get("/job/{job_id}/resume.{fmt}")
    def job_resume_file(job_id: int, fmt: str):
        from fastapi.responses import FileResponse
        r = db.get_resume(job_id)
        path = (r or {}).get("pdf_path" if fmt == "pdf" else "docx_path") if fmt in ("pdf", "docx") else ""
        if not path or not Path(path).exists():
            return RedirectResponse(f"/job/{job_id}", status_code=303)
        media = "application/pdf" if fmt == "pdf" else \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return FileResponse(path, media_type=media, filename=Path(path).name)

    @app.post("/job/{job_id}/action/{action}")
    def job_action(request: Request, job_id: int, action: str, next: str = Form("/")) -> RedirectResponse:
        actions.perform(job_id, action, cfg, fill=not request.state.remote)
        if action == "referrers":
            return RedirectResponse(f"/job/{job_id}?referrers=1", status_code=303)
        if action == "apply":
            return RedirectResponse(f"/job/{job_id}?applying=1", status_code=303)
        return RedirectResponse(security.safe_next(next), status_code=303)

    # ---------------- companies ----------------
    @app.get("/companies", response_class=HTMLResponse)
    def companies(request: Request, view: str = "all", msg: str = "", checking: int = 0) -> HTMLResponse:
        refresh = False
        if checking:
            if checking in checks_running:
                msg, refresh = f"Checking {checks_running[checking]}. This page refreshes by itself.", True
            else:
                msg = db.kv_get("company_check_msg") or msg
        rows = [dict({k: r[k] for k in r.keys()},
                     net=r["last_status"] == "network" or (r["last_status"] == "error" and is_network_error(r["last_error"])))
                for r in db.list_companies()]
        checked = [r for r in rows if r["last_status"] in ("ok", "error", "network")]
        net_n = sum(1 for r in checked if r["net"])
        net_banner = net_n >= 5 and net_n >= 0.25 * len(checked)
        if view == "failing":
            rows = [r for r in rows if r["last_status"] == "error" and not r["net"]]
        elif view == "network":
            rows = [r for r in rows if r["net"]]
        elif view == "browser":
            rows = [r for r in rows if r["last_status"] == "skipped" or r["source_type"] in ("browser", "html", "rss", "linkedin_only")]
        elif view == "paused":
            rows = [r for r in rows if not r["active"]]
        return page(request, "companies.html", rows=rows, view=view, msg=msg, net_banner=net_banner, net_n=net_n,
                    refresh=refresh, checking=checking)

    @app.post("/companies/detect", response_class=HTMLResponse)
    def companies_detect(request: Request, url: str = Form(...), name: str = Form("")) -> HTMLResponse:
        ok, why = security.public_http_url(url)
        if not ok:
            return RedirectResponse(f"/companies?msg={quote(why)}", status_code=303)  # type: ignore[return-value]
        client = PoliteClient()
        try:
            found = detect_source(url, client)
            comp = {"name": name.strip() or _guess_name(url), "source_type": found["source_type"],
                    "slug_or_endpoint": found["slug_or_endpoint"], "careers_url": found.get("careers_url", url)}
            preview, err = None, ""
            try:
                collector = collector_for(comp)
                res = collector.list_jobs(comp, client)
                ev = Evaluator(cfg)
                scoped = [ev.prefilter(j.title, j.location) for j in res.jobs]
                preview = {"total": len(res.jobs), "in_scope": sum(1 for s in scoped if s.in_scope),
                           "sample": [j.title for j, s in zip(res.jobs, scoped) if s.in_scope][:6]}
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:300]
        except ValueError as e:
            return RedirectResponse(f"/companies?msg={quote(str(e))}", status_code=303)  # type: ignore[return-value]
        finally:
            client.close()
        return page(request, "detect.html", comp=comp, note=found.get("note", ""), preview=preview, err=err)

    @app.post("/companies/save")
    def companies_save(name: str = Form(...), source_type: str = Form(...), slug_or_endpoint: str = Form(""),
                       careers_url: str = Form(""), category: str = Form(""), pay_tier: str = Form("")) -> RedirectResponse:
        name, source_type = name.strip()[:120], source_type.strip().lower()
        for u in (careers_url, slug_or_endpoint if slug_or_endpoint.lower().startswith("http") else ""):
            if u:
                ok, why = security.public_http_url(u)
                if not ok:
                    return RedirectResponse(f"/companies?msg={quote(why)}", status_code=303)
        if not name or source_type not in KNOWN_SOURCES:
            return RedirectResponse(f"/companies?msg={quote('That company could not be saved: name or source type is not valid.')}",
                                    status_code=303)
        db.upsert_company({"name": name, "source_type": source_type, "slug_or_endpoint": slug_or_endpoint,
                           "careers_url": careers_url, "category": category, "pay_tier": pay_tier,
                           "verified": "no", "active": "yes", "notes": "added from dashboard"})
        return RedirectResponse(f"/companies?msg={quote('Saved ' + name)}", status_code=303)

    @app.post("/companies/import")
    async def companies_import(file: UploadFile = File(...)) -> RedirectResponse:
        raw = await file.read(MAX_CSV + 1)
        if len(raw) > MAX_CSV:
            return RedirectResponse(f"/companies?msg={quote('That file is too large to be a company list.')}", status_code=303)
        text = raw.decode("utf-8-sig", errors="replace")
        try:
            n, warns = importer.import_csv_text(text)
            msg = f"Imported {n} companies" + (f" ({len(warns)} skipped)" if warns else "")
        except ValueError as e:
            msg = str(e)
        return RedirectResponse(f"/companies?msg={quote(msg)}", status_code=303)

    @app.post("/companies/{company_id}/toggle")
    def companies_toggle(company_id: int) -> RedirectResponse:
        row = db.get_company(company_id)
        if row:
            db.set_company_active(company_id, not row["active"])
        return RedirectResponse("/companies", status_code=303)

    @app.post("/companies/{company_id}/check")
    def companies_check(company_id: int) -> RedirectResponse:
        row = db.get_company(company_id)
        if not row:
            return RedirectResponse("/companies", status_code=303)
        if company_id not in checks_running:
            checks_running[company_id] = row["name"]

            def work() -> None:
                try:
                    stats = pipeline.run_cycle(cfg, only=row["name"])
                    o = stats.outcomes[0] if stats.outcomes else None
                    msg = (f"{row['name']}: {o.status}, {o.fetched} jobs fetched, {o.in_scope} in scope"
                           + (f" ({o.error})" if o and o.error else "")) if o else f"{row['name']}: nothing to check"
                except Exception as e:
                    msg = f"{row['name']}: the check failed ({type(e).__name__})"
                try:
                    db.kv_set("company_check_msg", msg[:400])
                finally:
                    checks_running.pop(company_id, None)

            threading.Thread(target=work, name=f"check-{company_id}", daemon=True).start()
        return RedirectResponse(f"/companies?checking={company_id}", status_code=303)

    # ---------------- settings ----------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0) -> HTMLResponse:
        sch = config.scheme_name(cfg)
        return page(request, "settings.html", saved=bool(saved), scheme=sch, schemes=config.SCHEMES,
                    backgrounds=catalogue.backgrounds(), chosen_bg=(cfg.get("profile") or {}).get("backgrounds") or [],
                    loc=cfg["locations"], alerts=cfg["alerts"], batch=cfg["targets"].get("apply_batch_rules", False))

    @app.post("/settings")
    async def settings_save(request: Request) -> RedirectResponse:
        f = await request.form()
        scheme = f.get("scheme") if f.get("scheme") in config.SCHEMES else config.scheme_name(cfg)
        valid = config.SCHEMES[scheme]["levels"]
        levels = [x for x in f.getlist("levels") if x in valid] or list(config.SCHEMES[scheme]["default_targets"])
        mode = f.get("mode") if f.get("mode") in ("india", "india_or_remote", "anywhere") else "india"
        try:
            min_score = max(0, min(100, int(f.get("min_score") or 55)))
        except ValueError:
            min_score = 55
        name = (f.get("name") or "").strip()[:60]
        bgs = [x for x in f.getlist("backgrounds") if x in {b["id"] for b in catalogue.backgrounds()}]
        cities = _words(f.get("cities") or "")

        def update(d: Dict[str, Any]) -> None:
            d.setdefault("profile", {})
            if name:
                d["profile"]["name"] = name
            d["profile"]["backgrounds"] = bgs
            d.setdefault("levels", {})["scheme"] = scheme
            d.setdefault("targets", {})["levels"] = levels
            d["targets"]["apply_batch_rules"] = bool(f.get("batch")) and scheme == "software"
            d.setdefault("locations", {})["mode"] = mode
            d["locations"]["only_cities"] = cities
            d.setdefault("alerts", {})["min_score"] = min_score
            d["alerts"]["include_interns"] = bool(f.get("interns"))
        apply_settings(update)
        return RedirectResponse("/settings?saved=1", status_code=303)

    # ---------------- roles ----------------
    @app.get("/roles", response_class=HTMLResponse)
    def roles_page(request: Request, saved: int = 0) -> HTMLResponse:
        chosen = list(cfg["roles"].get("families") or [])
        bgs = set((cfg.get("profile") or {}).get("backgrounds") or [])
        fams = []
        for fam in catalogue.families():
            fam = dict(fam)
            fam["on"] = fam["id"] in chosen
            fam["suggested"] = bool(bgs & set(fam.get("suits") or []))
            fams.append(fam)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for fam in fams:
            groups.setdefault(fam.get("group") or "Other", []).append(fam)
        return page(request, "roles.html", groups=groups, saved=bool(saved), has_core=bool(cfg["roles"].get("core")),
                    chosen=chosen, include=cfg["roles"].get("include") or [], exclude=cfg["roles"].get("exclude_extra") or [],
                    any_suggested=any(f["suggested"] for f in fams))

    @app.post("/roles")
    async def roles_save(request: Request) -> RedirectResponse:
        f = await request.form()
        known = {x["id"] for x in catalogue.families()}
        fams = [x for x in f.getlist("families") if x in known]
        inc, exc = _words(f.get("include") or ""), _words(f.get("exclude") or "")

        def update(d: Dict[str, Any]) -> None:
            r = d.setdefault("roles", {})
            r["families"], r["include"], r["exclude_extra"] = fams, inc, exc
            if fams:                       # chosen families replace a hand-written pattern in config.yaml
                r["core"], r["exclude"], r["adjacent"] = "", "", ""
        apply_settings(update)
        return RedirectResponse("/roles?saved=1", status_code=303)

    # ---------------- always open ----------------
    @app.get("/always-open", response_class=HTMLResponse)
    def always_open(request: Request, kind: str = "", india: str = "", show: str = "open") -> HTMLResponse:
        show = show if show in ("open", "applied", "all") else "open"
        return page(request, "always_open.html", rows=evergreen.listing(kind, india, show), kind=kind, india=india,
                    show=show, kinds=evergreen.kinds(), total=len(evergreen.load()))

    @app.post("/always-open/{item_id}")
    def always_open_set(item_id: str, status: str = Form(""), next: str = Form("/always-open")) -> RedirectResponse:
        if item_id in {i["id"] for i in evergreen.load()}:
            evergreen.set_status(item_id, status)
        return RedirectResponse(security.safe_next(next) if next != "/" else "/always-open", status_code=303)

    # ---------------- master resume ----------------
    def _facts_overview() -> Optional[Dict[str, Any]]:
        if not config.FACTS_PATH.exists():
            return None
        try:
            from ..resume.facts import load_facts
            fx = load_facts()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"[:300]}
        return {"name": fx.contact.get("name", ""), "email": fx.contact.get("email", ""),
                "education": f"{fx.education.get('degree', '')}, {fx.education.get('school', '')}",
                "experience": [(e["company"], e["title"], len(e["bullets"])) for e in fx.experience],
                "projects": [(p["names"][0], len(p["bullets"])) for p in fx.projects],
                "skills": sum(len(c["items"]) for c in fx.skills), "achievements": len(fx.achievements),
                "summaries": len(fx.summaries)}

    @app.get("/resume", response_class=HTMLResponse)
    def resume_page(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        m = resume_importer.current_master()
        report = db.loads(db.kv_get("master_report"), None)
        return page(request, "resume.html", master=m, facts=_facts_overview(), report=report, msg=msg, err=err,
                    ai_reader=bool(llmmod.groq(cfg)))

    @app.post("/resume/upload")
    async def resume_upload(file: UploadFile = File(...)) -> RedirectResponse:
        data = await file.read(MAX_RESUME + 1)
        if len(data) > MAX_RESUME:
            return RedirectResponse(f"/resume?err={quote('That file is larger than 8 MB. Export a smaller PDF or Word file.')}",
                                    status_code=303)
        try:
            report = resume_importer.save_master(file.filename or "resume.pdf", data, llmmod.groq(cfg))
        except ValueError as e:
            return RedirectResponse(f"/resume?err={quote(str(e))}", status_code=303)
        except Exception as e:
            return RedirectResponse(f"/resume?err={quote('Could not read that file: ' + type(e).__name__)}", status_code=303)
        db.kv_set("master_report", json.dumps(report))
        return RedirectResponse("/resume?msg=" + quote("Resume read. Check the details below, then open Review if anything looks wrong."),
                                status_code=303)

    @app.get("/resume/file")
    def resume_file():
        from fastapi.responses import FileResponse
        m = resume_importer.current_master()
        if not m:
            return RedirectResponse("/resume", status_code=303)
        return FileResponse(str(m["path"]), filename=m["name"])

    @app.get("/resume/review", response_class=HTMLResponse)
    def resume_review(request: Request, saved: int = 0) -> HTMLResponse:
        text = config.FACTS_PATH.read_text(encoding="utf-8") if config.FACTS_PATH.exists() else ""
        return page(request, "resume_review.html", text=text, saved=bool(saved), error="")

    @app.post("/resume/review", response_class=HTMLResponse)
    def resume_review_save(request: Request, text: str = Form("")):
        err = resume_importer.save_yaml(text)
        if err:
            return page(request, "resume_review.html", text=text, saved=False, error=err)
        return RedirectResponse("/resume/review?saved=1", status_code=303)

    # ---------------- misc ----------------
    @app.post("/cycle/run")
    def cycle_run() -> RedirectResponse:
        if run_cycle_now:
            run_cycle_now()
        return RedirectResponse("/", status_code=303)

    # ---------------- sign-in (only used when the site is opened through a tunnel) ----------------
    def login_page(request: Request, next: str = "/", error: str = "", status: int = 200, email: str = "") -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "next": security.safe_next(next), "error": error, "email": email,
            "profile_name": (cfg.get("profile") or {}).get("name") or "Jobhunter"}, status_code=status)

    def needs_login(request: Request) -> bool:
        return (request.state.remote or config.REQUIRE_LOGIN) and bool(auth.count_for(config.PROFILE))

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request, next: str = "/"):
        if not needs_login(request):
            return RedirectResponse("/", status_code=303)
        if request.state.user:
            return RedirectResponse(security.safe_next(next), status_code=303)
        return login_page(request, next)

    @app.post("/login")
    def login_post(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/")):
        if not needs_login(request):
            return RedirectResponse("/", status_code=303)
        ip = security.client_ip(request.headers, request.client.host if request.client else "")
        em = auth.normalize(email)[:200]
        keys = (ip, "mail:" + em)
        wait = max(limiter.wait_seconds(k) for k in keys)
        if wait:
            mins = max(1, (wait + 59) // 60)
            return login_page(request, next, f"Too many wrong attempts. Try again in {mins} minute{'s' if mins != 1 else ''}.", 429, em)
        user = auth.get_user(em)
        good = bool(user) and user["profile"] == config.PROFILE and security.verify_password(password[:200], user["password_hash"])
        if not user:
            security.burn_time(password[:200])
        if not good:
            for k in keys:
                limiter.fail(k)
            return login_page(request, next, "That email and password do not match.", 401, em)
        for k in keys:
            limiter.ok(k)
        auth.touch_login(em)
        resp = RedirectResponse(security.safe_next(next), status_code=303)
        resp.set_cookie(security.cookie_name(), security.make_session(user), max_age=security.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=security.is_https(request.headers, request.url.scheme), path="/")
        return resp

    @app.post("/logout")
    def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(security.cookie_name(), path="/")
        return resp

    # ---------------- installable on a phone's home screen ----------------
    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        name = (cfg.get("profile") or {}).get("name") or "Jobhunter"
        return JSONResponse({
            "name": f"{name} · Jobhunter", "short_name": "Jobhunter", "start_url": "/", "scope": "/",
            "display": "standalone", "background_color": "#ffffff", "theme_color": "#0a0a0a",
            "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
        }, media_type="application/manifest+json")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def _guess_name(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower().replace("www.", "")
    parts = [p for p in host.split(".") if p not in ("careers", "jobs", "com", "in", "io", "co", "org", "net")]
    return (parts[0] if parts else host).capitalize()


def _message_page(title: str, text: str, status: int = 200) -> HTMLResponse:
    from html import escape
    body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><link rel="stylesheet" href="/static/style.css"></head><body>'
            f'<main><div class="wrap" style="max-width:560px"><h1>{escape(title)}</h1><p class="sub">{escape(text)}</p>'
            f'<p><a class="btn" href="/">Back to Jobhunter</a></p></div></main></body></html>')
    return HTMLResponse(body, status_code=status)
