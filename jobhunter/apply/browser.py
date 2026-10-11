"""One visible Chrome window for applications. A dedicated profile (data/apply_profile) keeps your logins and
solved CAPTCHAs between jobs. Tabs stay open after filling so you can review and press Submit yourself."""
from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config
from ..collectors.base import Unsupported
from ..collectors.browser import MAC_BROWSERS
from .page import collect, pending_fp, run_on_page_ex, watch_tick
from .profile import Profile

log = logging.getLogger("jobhunter.apply")
TASK_TIMEOUT = 300


class ApplyBrowser:
    def __init__(self) -> None:
        self._q: "queue.Queue[Tuple[Callable[[Any], Any], Future]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pw: Any = None
        self._ctx: Any = None
        self._watchers: List[Dict[str, Any]] = []

    def watch(self, w: Dict[str, Any]) -> None:
        self._watchers.append(w)

    def _tick(self) -> None:
        """Idle moments: look at pages we filled before. A new wizard step (after you log in or press Next) gets filled."""
        for w in list(self._watchers):
            if w["state"].get("done"):
                self._watchers.remove(w)
                continue
            step = watch_tick(w["page"], w["prof"], w["pdf"], w["ctx"], w["state"])
            if step is not None and w.get("on_step"):
                try:
                    w["on_step"](step)
                except Exception:
                    log.exception("on_step failed")

    def submit(self, fn: Callable[[Any], Any], timeout: float = TASK_TIMEOUT) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="apply-browser", daemon=True)
                self._thread.start()
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result(timeout=timeout)

    def _loop(self) -> None:
        while True:
            try:
                fn, fut = self._q.get(timeout=2.0)
            except queue.Empty:
                if self._watchers:
                    try:
                        self._tick()
                    except BaseException:       # noqa: BLE001 - a watcher must never kill the browser thread
                        log.exception("watcher tick failed")
                continue
            try:
                if not fut.set_running_or_notify_cancel():
                    continue
                fut.set_result(fn(self._context()))
            except BaseException as e:        # noqa: BLE001 - hand every failure back
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if "closed" in str(e).lower() or "crash" in str(e).lower():
                    self._reset()
                try:
                    fut.set_exception(e)
                except Exception:
                    pass

    def _alive(self) -> bool:
        if self._ctx is None:
            return False
        try:
            self._ctx.pages          # raises when the user closed the window
            b = self._ctx.browser
            return b is None or b.is_connected()
        except Exception:
            return False

    def _context(self) -> Any:
        if self._alive():
            return self._ctx
        self._reset()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise Unsupported("Playwright is not installed. Run ./browser-setup.sh, then restart the service.")
        self._pw = sync_playwright().start()
        headless = os.environ.get("JOBHUNTER_APPLY_HEADLESS", "").strip() in ("1", "true", "yes")
        args = ["--disable-blink-features=AutomationControlled"]
        if os.environ.get("JOBHUNTER_NO_SANDBOX"):
            args.append("--no-sandbox")
        profile_dir = config.DATA_DIR / "apply_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        attempts: List[Dict[str, Any]] = []
        env_path = os.environ.get("JOBHUNTER_CHROME_PATH", "").strip()
        if env_path:
            attempts.append({"executable_path": env_path})
        for p in MAC_BROWSERS:
            if os.path.exists(p):
                attempts.append({"executable_path": p})
        attempts.append({"channel": "chrome"})
        attempts.append({})
        last = ""
        for kw in attempts:
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(profile_dir), headless=headless, args=args, no_viewport=not headless,
                    viewport={"width": 1280, "height": 900} if headless else None, **kw)
                log.info("apply browser started (%s)", kw or "bundled chromium")
                return self._ctx
            except Exception as e:
                last = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        self._reset()
        raise Unsupported("Could not open Chrome for applications (%s). Close any Chrome window that uses the "
                          "Jobhunter profile, or set JOBHUNTER_CHROME_PATH." % last)

    def _reset(self) -> None:
        self._watchers = []
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        self._ctx = None
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    def shutdown(self) -> None:
        self._q.put((lambda _c: self._reset(), Future()))


_browser = ApplyBrowser()
atexit.register(lambda: setattr(_browser, "_ctx", None))


def fill_job(url: str, prof: Profile, resume_pdf: Optional[str], ctx: Any = None, on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
             watch_minutes: int = 45) -> Dict[str, Any]:
    """Open `url` in a new tab of the application window and fill it. Blocks until done (up to 5 minutes).
    Afterwards the tab is watched for `watch_minutes`: a new step (after you log in or press Next) is filled too."""
    def task(c: Any) -> Dict[str, Any]:
        page = c.new_page()
        rep, final = run_on_page_ex(page, url, prof, resume_pdf, ctx)
        try:
            if not final.is_closed() and (rep.get("status") != "failed" or rep.get("login")):
                state: Dict[str, Any] = {"deadline": time.time() + watch_minutes * 60, "fp": pending_fp(collect(final))}
                _browser.watch({"page": final, "prof": prof, "pdf": resume_pdf, "ctx": ctx, "state": state, "on_step": on_step})
        except Exception:
            log.info("could not start watching the page")
        return rep
    return _browser.submit(task)
