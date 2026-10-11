"""Command line: make targets call these."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import httpx

from . import config, db, importer, notifier, pipeline, redis_store
from .util import parse_iso


def _pid() -> Optional[int]:
    try:
        pid = int(config.PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def _url() -> str:
    return f"http://{config.HOST}:{config.PORT}"


def cmd_setup(_: argparse.Namespace) -> int:
    config.ensure_dirs()
    db.init_db()
    if not db.list_companies():
        csv_path = config.PROFILE_DIR / "companies.csv"
        if not csv_path.exists():
            csv_path = config.DATA_DIR / "companies.csv"
        if csv_path.exists():
            n, warns = importer.import_csv_file(str(csv_path))
            print(f"Imported {n} companies from {csv_path.name}")
            for w in warns:
                print("  warning:", w)
    print(f"Database ready: {len(db.list_companies())} companies.")
    return 0


def cmd_start(_: argparse.Namespace) -> int:
    if _pid():
        print(f"Already running (pid {_pid()}). Dashboard: {_url()}")
        return 0
    _kill_strays()
    config.ensure_dirs()
    db.init_db()
    log = open(config.LOG_DIR / "service.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "jobhunter.service", f"--profile={config.PROFILE or 'default'}"], cwd=str(config.ROOT), stdout=log,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    config.PID_FILE.write_text(str(proc.pid))
    for _i in range(40):
        time.sleep(0.25)
        if proc.poll() is not None:
            config.PID_FILE.unlink(missing_ok=True)
            print("The service exited immediately. Last log lines:")
            os.system(f"tail -n 15 '{config.LOG_DIR / 'service.log'}'")
            return 1
        try:
            if httpx.get(_url() + "/healthz", timeout=1).status_code == 200:
                print(f"Started (pid {proc.pid}). Dashboard: {_url()}")
                print("The first check of all companies runs now; follow it with `make logs`.")
                return 0
        except httpx.HTTPError:
            continue
    print(f"Started (pid {proc.pid}) but the dashboard is not answering yet. See `make logs`.")
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    pid = _pid()
    if not pid:
        _kill_strays()
        print("Not running.")
        config.PID_FILE.unlink(missing_ok=True)
        redis_store.release_lock()
        return 0
    os.kill(pid, signal.SIGTERM)
    for _i in range(40):
        time.sleep(0.25)
        if not _pid():
            break
    else:
        os.kill(pid, signal.SIGKILL)
    config.PID_FILE.unlink(missing_ok=True)
    _kill_strays()
    redis_store.release_lock()
    print("Stopped.")
    return 0


def _kill_strays() -> None:
    """A service whose pid file was lost (for example after an interrupted start) must not keep running."""
    try:
        tag = config.PROFILE or "default"
        # only this profile's service (a profile's service must never stop somebody else's)
        pattern = r"jobhunter\.service( --profile=default)?$" if tag == "default" else rf"jobhunter\.service --profile={tag}$"
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    except Exception:
        return
    for p in out:
        try:
            if int(p) != os.getpid():
                os.kill(int(p), signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass


def cmd_status(_: argparse.Namespace) -> int:
    pid = _pid()
    print(f"Service:    {'running (pid %d)' % pid if pid else 'stopped'}")
    if pid:
        started = config.PID_FILE.stat().st_mtime
        mins = int((time.time() - started) / 60)
        print(f"Uptime:     {mins} min")
        print(f"Dashboard:  {_url()}")
    if db.backend() == "sqlite" and not config.DB_PATH.exists():
        print("Database:   not created yet. Run `make setup`.")
        return 0
    print(f"Database:   {db.describe()}")
    print(f"Redis:      {redis_store.ping()}")
    try:
        comps = db.list_companies()
    except Exception as e:
        print(f"Cannot read the database: {type(e).__name__}: {e}")
        return 1
    by = {s: sum(1 for c in comps if c["last_status"] == s) for s in ("ok", "error", "skipped", "never", "network")}
    counts = db.job_counts()
    last = parse_iso(db.kv_get("last_cycle_at"))
    print(f"Last check: {last.astimezone().strftime('%Y-%m-%d %H:%M') if last else 'never'}")
    print(f"Companies:  {len(comps)} total · {by['ok']} working · {by['error']} failing · "
          f"{by['network']} connection problems (retried soon) · {by['skipped']} need the browser step · {by['never']} not checked yet")
    print(f"Jobs:       {counts['in_scope']} open roles tracked · {counts['new_matches']} new matches · "
          f"{counts['referral']} in referral · {counts['applied']} applied")
    tg = notifier.from_env()
    print(f"Telegram:   {'ready' if tg.configured else ('token set, chat id missing (run make chat-id)' if tg.has_token else 'not configured')}")
    print(f"Logs:       {config.LOG_DIR / 'service.log'}")
    return 0


def cmd_logs(_: argparse.Namespace) -> int:
    path = config.LOG_DIR / "service.log"
    if not path.exists():
        print("No log yet. Start the service first.")
        return 1
    try:
        os.execvp("tail", ["tail", "-n", "60", "-f", str(path)])
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_import(ns: argparse.Namespace) -> int:
    db.init_db()
    n, warns = importer.import_csv_file(ns.file)
    print(f"Imported {n} companies.")
    for w in warns:
        print("  warning:", w)
    return 0


def cmd_check(ns: argparse.Namespace) -> int:
    """Fetch every active company now and print a health table. Sends no Telegram alerts."""
    db.init_db()
    cfg = config.load_config()
    print("Checking sources (this takes a few minutes)...")
    with redis_store.cycle_lock() as got:
        if not got:
            print("The service is running a check right now. Wait for it to finish, or run `make service-stop` first.")
            return 1
        stats = pipeline.run_cycle(cfg, force=True, only=ns.only)
    order = {"error": 0, "skipped": 1, "ok": 2}
    rows = sorted(stats.outcomes, key=lambda o: (order.get(o.status, 3), o.company.lower()))
    print(f"\n{'company':32} {'status':8} {'jobs':>5} {'in scope':>8} {'matches':>7}  note")
    for o in rows:
        if ns.problems and o.status == "ok" and not o.error:
            continue
        print(f"{o.company[:32]:32} {o.status:8} {o.fetched:5d} {o.in_scope:8d} {o.relevant:7d}  {o.error[:90]}")
    print(f"\n{stats.ok} working, {stats.errors} failing, {stats.skipped} need the browser step. "
          f"{stats.new_jobs} new in-scope jobs stored.")
    return 0


def cmd_probe(ns: argparse.Namespace) -> int:
    """Open one careers page in the browser reader and show what it sees (for pages that report 'no job list')."""
    from .collectors import browser as bc
    url = ns.url
    if not url.startswith("http"):
        url = "https://" + url
    shot = str(config.DATA_DIR / "probe.png") if hasattr(config, "DATA_DIR") else "probe.png"
    print(f"Opening {url} (waits about {ns.wait} s)...")
    try:
        r = bc._runner.submit(lambda b: bc.probe_page(b, url, shot, int(ns.wait * 1000), not ns.full), timeout=ns.wait + 120)
    finally:
        bc._runner.shutdown()
    print(f"\nfinal address : {r['final_url']}\ntitle         : {r['title']!r}\npage text     : {r['text_len']} characters"
          f"\nlinks         : {r['anchors']}\nframes        : {r['frames']}")
    if r["errors"]:
        print("page errors   :", *r["errors"], sep="\n  ")
    print("\n--- first text on the page ---\n" + (r["text"] or "(blank)"))
    print(f"html size     : {r['html_len']}   browser: webdriver={r['env']['webdriver']}  {r['env']['ua'][:90]}")
    if r["failed"]:
        print("\n--- requests that failed ---", *r["failed"], sep="\n  ")
    if r["console"]:
        print("\n--- browser console ---", *r["console"], sep="\n  ")
    print("\n--- JSON the page loaded ---")
    for j in r["json"]:
        print(f"  {j['size']:>8} bytes  {j['url']}")
    print("\n--- requests (page, scripts, xhr/fetch) ---")
    for q in r["requests"]:
        print(f"  {q['status']} {q['method']:4} {q['type']:8} {q['url']}")
    print("\n--- first links ---")
    for t, h in r["sample_links"]:
        print(f"  {t!r:64} {h}")
    if r.get("screenshot"):
        print("\nscreenshot saved to", r["screenshot"])
    return 0


def cmd_reevaluate(_: argparse.Namespace) -> int:
    db.init_db()
    r = pipeline.reevaluate_all(config.load_config())
    print(f"Re-evaluated {r['total']} stored jobs; {r['changed']} changed.")
    return 0


def _write_env(key: str, value: str) -> None:
    p = config.PROFILE_DIR / ".env"
    lines = p.read_text().splitlines() if p.exists() else []
    out, done = [], False
    for ln in lines:
        if ln.startswith(f"{key}="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    p.write_text("\n".join(out) + "\n")


def cmd_chat_id(_: argparse.Namespace) -> int:
    tg = notifier.from_env()
    if not tg.has_token:
        print("Put TELEGRAM_BOT_TOKEN in .env first.")
        return 1
    me = tg.me()
    print(f"Open Telegram, find @{me['username']} and send it /start. Waiting up to 90 seconds...")
    end, offset = time.time() + 90, None
    while time.time() < end:
        for u in tg.get_updates(offset, timeout=5):
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("id"):
                _write_env("TELEGRAM_CHAT_ID", str(chat["id"]))
                print(f"Found it and saved TELEGRAM_CHAT_ID={chat['id']} to .env")
                notifier.Telegram(tg.token, str(chat["id"])).send("Connected. You will get job alerts here.")
                return 0
    print("Did not receive a message. Send /start to the bot and run this again.")
    return 1


def cmd_test_telegram(_: argparse.Namespace) -> int:
    tg = notifier.from_env()
    if not tg.configured:
        print("Telegram is not fully configured (token and chat id needed).")
        return 1
    tg.send("Test message from Jobhunter.")
    print("Sent.")
    return 0


def cmd_db_create(_: argparse.Namespace) -> int:
    """Create the 'jobhunter' database inside your existing Postgres server, if it does not exist."""
    url = config.database_url()
    if db.backend() != "pg":
        print("DATABASE_URL is not set to a postgresql:// URL; the app will use a local SQLite file instead.")
        return 0
    from urllib.parse import urlparse, urlunparse
    import psycopg
    u = urlparse(url)
    name = u.path.lstrip("/") or "jobhunter"
    admin = urlunparse(u._replace(path="/postgres"))
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=8) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            if exists:
                print(f"Database '{name}' already exists.")
            else:
                conn.execute(f'CREATE DATABASE "{name}" ENCODING \'UTF8\' TEMPLATE template0')
                print(f"Created database '{name}'.")
    except Exception as e:
        print(f"Could not reach Postgres at {config.redact(url)}\n  {type(e).__name__}: {e}")
        print("Check the host, port, user and password in DATABASE_URL, and that the container is running (`docker ps`).")
        return 1
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    """Check every dependency and say what to fix."""
    ok = True
    print(f"Python:     {sys.version.split()[0]}")
    print(f"Database:   {db.describe()}")
    try:
        db.init_db()
        n = len(db.list_companies())
        print(f"            reachable, {n} companies")
    except Exception as e:
        ok = False
        print(f"            NOT reachable: {type(e).__name__}: {e}")
        print("            Run `make db-create` after setting DATABASE_URL in .env, and check `docker ps`.")
    print(f"Redis:      {redis_store.ping()}")
    try:
        from .collectors import browser as _browser
        ok_b, msg_b = _browser.available()
        print(f"Browser:    {msg_b}")
    except Exception as e:
        print(f"Browser:    unavailable ({type(e).__name__}: {e})")
    from .resume import llm as _llm
    cfg = config.load_config()
    print(f"Groq:       {_llm.ping(_llm.groq(cfg))[1]}")
    print(f"GitHub:     {_llm.ping(_llm.github(cfg))[1]}")
    tg = notifier.from_env()
    if tg.has_token:
        try:
            print(f"Telegram:   bot @{tg.me()['username']}, chat id {'set' if tg.chat_id else 'MISSING (run make chat-id)'}")
        except Exception as e:
            ok = False
            print(f"Telegram:   token rejected ({type(e).__name__}); create a new one with @BotFather")
    else:
        print("Telegram:   no token in .env")
    from . import auth as _auth
    n = _auth.count_for(config.PROFILE)
    print("Phone:      " + ((f"{n} user(s) can sign in; public address {config.PUBLIC_URL}" if config.PUBLIC_URL else
                            f"{n} user(s) can sign in; JOBHUNTER_PUBLIC_URL not set (Telegram links stay local)") if n else
                           "no users: the site only opens on this computer. Add one with `./jh users add EMAIL --profile NAME`"))
    return 0 if ok else 1


def cmd_serve(_: argparse.Namespace) -> int:
    from .service import main
    main()
    return 0


TEMPLATES = Path(__file__).parent / "profile_templates"
LAUNCHER = """#!/bin/bash
# Double-click in Finder: sets this profile up (first time) and starts it. The profile is the part after "start-".
cd "$(dirname "$0")" || exit 1
slug=$(basename "$0" .command); slug=${slug#start-}
./jh "$slug" setup || { read -r -p "Setup failed. Press Enter to close"; exit 1; }
./jh "$slug" restart
sleep 2
./jh "$slug" open
echo; echo "Running. Stop it with:  ./jh $slug stop"
read -r -p "Press Enter to close this window (it keeps running)"
"""


def cmd_profile_new(ns: argparse.Namespace) -> int:
    """Create profiles/<slug>/ from a template: its own config, companies, answers, port and (later) database."""
    import re
    import shutil
    slug = re.sub(r"[^a-z0-9_-]", "", ns.slug.lower())
    if not slug:
        print("Give the profile a short name using letters and numbers, e.g. english-hons")
        return 2
    src = TEMPLATES / ns.template
    if not src.exists():
        print("Templates: " + ", ".join(sorted(d.name for d in TEMPLATES.iterdir() if d.is_dir())))
        return 2
    dest = config.PROFILES_DIR / slug
    if dest.exists():
        print(f"profiles/{slug} already exists; nothing changed.")
        return 1
    shutil.copytree(src, dest)
    used = {p["port"] for p in config.list_profiles()}
    port = max([8787] + [u for u in used if u]) + 1
    (dest / ".env").write_text(f"JOBHUNTER_PORT={port}\n", encoding="utf-8")
    if ns.name:
        cfg_path = dest / "config.yaml"
        import yaml
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        data.setdefault("profile", {})["name"] = ns.name
        cfg_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    launcher = config.ROOT / f"start-{slug}.command"       # double-click launcher for Finder
    launcher.write_text(LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)
    print(f"Created profiles/{slug} (template: {ns.template}), dashboard port {port}.")
    print(f"Double-click start-{slug}.command in Finder to set up and start it.")
    print(f"Next:  ./jh up      (sets up and starts every profile, including this one)   then open http://127.0.0.1:{port}")
    print(f"For phone access:  ./jh users add her@email.com --profile {slug}")
    return 0


def _gw_pid() -> Optional[int]:
    try:
        pid = int((config.GATEWAY_DIR / "gateway.pid").read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None
    except PermissionError:
        return pid


def cmd_gateway_stop(_: argparse.Namespace) -> int:
    pid = _gw_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _i in range(30):
            time.sleep(0.2)
            if not _gw_pid():
                break
        else:
            os.kill(pid, signal.SIGKILL)
        print("Gateway stopped.")
    (config.GATEWAY_DIR / "gateway.pid").unlink(missing_ok=True)
    return 0


def cmd_gateway_start(_: argparse.Namespace) -> int:
    """The single public port: sign-in page plus a forwarder to each person's dashboard."""
    from .web import security
    if _gw_pid():
        cmd_gateway_stop(_)
    config.GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    security.gateway_secret()
    log = open(config.GATEWAY_DIR / "gateway.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "jobhunter.gateway"], cwd=str(config.ROOT), stdout=log,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    (config.GATEWAY_DIR / "gateway.pid").write_text(str(proc.pid))
    url = f"http://127.0.0.1:{config.GATEWAY_PORT}"
    for _i in range(40):
        time.sleep(0.25)
        if proc.poll() is not None:
            print("The gateway exited immediately. Last log lines:")
            os.system(f"tail -n 12 '{config.GATEWAY_DIR / 'gateway.log'}'")
            return 1
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                print(f"Gateway started (pid {proc.pid}). Point the tunnel at {url}")
                return 0
        except httpx.HTTPError:
            continue
    print(f"Gateway started (pid {proc.pid}) but is not answering yet. See {config.GATEWAY_DIR / 'gateway.log'}")
    return 0


def cmd_users(ns: argparse.Namespace) -> int:
    """Who can sign in to which profile's dashboard (the shared user table, auth.db)."""
    import getpass
    from . import auth
    from .web import security
    act = ns.action or "list"
    if act == "list":
        rows = auth.list_users()
        if not rows:
            print("No users yet. Add one:  ./jh users add EMAIL --profile NAME")
            return 0
        print(f"{'EMAIL':<34} {'PROFILE':<16} {'LAST SIGN-IN':<20}")
        for u in rows:
            print(f"{u['email']:<34} {u['profile'] or '(default)':<16} {(u['last_login_at'] or 'never')[:19]:<20}")
        return 0
    if act in ("add", "passwd"):
        if not ns.email:
            print(f"Usage: ./jh users {act} EMAIL" + (" [--profile NAME]" if act == "add" else ""))
            return 2
        existing = auth.get_user(ns.email)
        if act == "passwd" and not existing:
            print("No such user. Add them with:  ./jh users add EMAIL --profile NAME")
            return 1
        profile = existing["profile"] if (act == "passwd" or (existing and ns.profile is None)) else \
            (ns.profile if ns.profile is not None else config.PROFILE)
        if profile and not (config.PROFILES_DIR / profile / "config.yaml").exists():
            print(f"There is no profile called '{profile}'. Profiles: ./jh profiles")
            return 1
        pw = ns.password
        if not pw:
            pw = getpass.getpass(f"Password for {ns.email} (at least {security.MIN_LENGTH} characters): ")
            if getpass.getpass("Type it again: ") != pw:
                print("The two did not match; nothing changed.")
                return 2
        try:
            u = auth.add_user(ns.email, pw, profile, ns.name or "")
        except ValueError as e:
            print(e)
            return 2
        print(f"{'Updated' if existing else 'Added'} {u['email']} for profile {u['profile'] or '(default)'}. No restart needed.")
        return 0
    if act == "remove":
        print("Removed." if ns.email and auth.remove_user(ns.email) else "No such user.")
        return 0
    return 2


def cmd_profiles(ns: argparse.Namespace) -> int:
    if getattr(ns, "slugs", False):                 # machine-readable, used by ./jh up / down / all
        for p in config.list_profiles():
            print(p["slug"] or "default")
        return 0
    for p in config.list_profiles():
        print(f"{p['slug'] or '(default)':<16} {p['name']:<28} port {p['port'] or '-'}")
    return 0


def cmd_ps(_: argparse.Namespace) -> int:
    """One line per profile: is it running, on which port, is a phone password set."""
    from dotenv import dotenv_values
    rows = []
    for p in config.list_profiles():
        d = Path(p["dir"])
        env = dotenv_values(d / ".env") if (d / ".env").exists() else {}
        data = Path(os.environ["JOBHUNTER_DATA"]) if os.environ.get("JOBHUNTER_DATA") and not p["slug"] else d / "data"
        state = "stopped"
        try:
            pid = int((data / "service.pid").read_text().strip())
            os.kill(pid, 0)
            state = f"running ({pid})"
        except (FileNotFoundError, ValueError, ProcessLookupError):
            pass
        except PermissionError:
            state = "running"
        port = p["port"] or int(env.get("JOBHUNTER_PORT") or 0)
        if state.startswith("running") and port:
            try:
                state = state if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.5).status_code == 200 else "starting"
            except httpx.HTTPError:
                state = "not answering"
        from . import auth
        n = auth.count_for(p["slug"])
        phone = f"{n} user{'s' if n != 1 else ''} can sign in" if n else "no users: this Mac only"
        rows.append((p["slug"] or "(default)", p["name"], str(port or "-"), state, phone))
    gpid = _gw_pid()
    gstate = "stopped"
    if gpid:
        try:
            gstate = f"running ({gpid})" if httpx.get(f"http://127.0.0.1:{config.GATEWAY_PORT}/healthz", timeout=1.5).status_code == 200 else "starting"
        except httpx.HTTPError:
            gstate = "not answering"
    rows.append(("(gateway)", "public sign-in door", str(config.GATEWAY_PORT), gstate, "point Cloudflare here"))
    head = ("PROFILE", "NAME", "PORT", "STATE", "PHONE ACCESS")
    widths = [max(len(r[i]) for r in rows + [head]) for i in range(5)]
    for r in [head] + rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="jobhunter")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in [("setup", cmd_setup), ("service-start", cmd_start), ("service-stop", cmd_stop),
                     ("service-status", cmd_status), ("logs", cmd_logs), ("reevaluate", cmd_reevaluate),
                     ("chat-id", cmd_chat_id), ("test-telegram", cmd_test_telegram), ("serve", cmd_serve),
                     ("db-create", cmd_db_create), ("doctor", cmd_doctor), ("ps", cmd_ps),
                     ("gateway-start", cmd_gateway_start), ("gateway-stop", cmd_gateway_stop)]:
        sub.add_parser(name).set_defaults(fn=fn)
    s = sub.add_parser("profiles"); s.add_argument("--slugs", action="store_true"); s.set_defaults(fn=cmd_profiles)
    s = sub.add_parser("import"); s.add_argument("file"); s.set_defaults(fn=cmd_import)
    s = sub.add_parser("profile-new"); s.add_argument("slug"); s.add_argument("--template", default="blank")
    s.add_argument("--name", default=""); s.set_defaults(fn=cmd_profile_new)
    s = sub.add_parser("users"); s.add_argument("action", nargs="?", choices=["list", "add", "passwd", "remove"])
    s.add_argument("email", nargs="?"); s.add_argument("--profile", default=None); s.add_argument("--name", default="")
    s.add_argument("--password", default=None); s.set_defaults(fn=cmd_users)
    s = sub.add_parser("probe"); s.add_argument("url"); s.add_argument("--wait", type=float, default=15)
    s.add_argument("--full", action="store_true", help="also load images, fonts and media (the reader normally skips them)")
    s.set_defaults(fn=cmd_probe)
    s = sub.add_parser("check"); s.add_argument("--only"); s.add_argument("--problems", action="store_true")
    s.set_defaults(fn=cmd_check)
    ns = p.parse_args(argv)
    try:
        return int(ns.fn(ns) or 0)
    except KeyboardInterrupt:
        return 130
    finally:
        db.close_pools()


if __name__ == "__main__":
    sys.exit(main())
