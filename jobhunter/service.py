"""The background service: scheduler + Telegram listener + dashboard, in one process."""
from __future__ import annotations

import logging
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import config, db, notifier, pipeline, redis_store, telegram_bot
from .util import parse_iso

log = logging.getLogger("jobhunter.service")


class Service:
    def __init__(self, cfg: Dict[str, Any], tg: Optional[notifier.Telegram] = None) -> None:
        self.cfg = cfg
        self.tg = tg if tg is not None else notifier.from_env()
        self.stop_evt = threading.Event()
        self._cycle_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self.base_url = config.PUBLIC_URL or f"http://{config.HOST}:{config.PORT}"

    # ---- cycle ----
    def cycle_running(self) -> bool:
        return self._cycle_lock.locked()

    def run_cycle_now(self, force: bool = False) -> bool:
        """Start a cycle in the background unless one is already running. force=True re-checks every company
        now, ignoring the per-company wait (used after Settings change what you are looking for)."""
        if self.cycle_running():
            return False
        threading.Thread(target=self._do_cycle, kwargs={"force": force}, name="cycle", daemon=True).start()
        return True

    def _do_cycle(self, force: bool = False) -> None:
        if not self._cycle_lock.acquire(blocking=False):
            return
        try:
            with redis_store.cycle_lock() as got:
                if not got:
                    log.info("another process is running a cycle; skipping this one")
                    return
                prev = parse_iso(db.kv_get("last_cycle_at"))
                log.info("cycle started")
                stats = pipeline.run_cycle(self.cfg, force=force)
                log.info("cycle done: %d checked, %d ok, %d errors, %d skipped, %d new in-scope jobs",
                         stats.companies_checked, stats.ok, stats.errors, stats.skipped, stats.new_jobs)
                self._send_alerts(prev, stats)
        except Exception:
            log.exception("cycle crashed")
        finally:
            self._cycle_lock.release()

    def _send_alerts(self, prev: Optional[datetime], stats: pipeline.CycleStats) -> None:
        if not self.tg.configured:
            if stats.new_jobs:
                log.info("Telegram not configured; matches are on the dashboard only")
            return
        try:
            rows = pipeline.pending_alerts(self.cfg)
            cap = int(self.cfg["alerts"]["max_individual_alerts"])
            gap = (datetime.now(timezone.utc) - prev).total_seconds() if prev else None
            if rows and gap and gap > 3 * 3600:
                hours = int(gap // 3600)
                self.tg.send(notifier.esc(f"Back online after {hours} h. {len(rows)} new match(es) since your last check."))
            for r in rows[:cap]:
                self.tg.send_job(r, self.base_url)
                pipeline.mark_alerted(r["id"])
                time.sleep(0.4)
            rest = rows[cap:]
            if rest:
                self.tg.send(notifier.esc(f"{len(rest)} more match(es) are on the dashboard: {self.base_url}"))
                for r in rest:
                    pipeline.mark_alerted(r["id"])
            for w in stats.health_warnings:
                self.tg.send(notifier.esc("Source problem: " + w))
        except Exception:
            log.exception("sending alerts failed (will retry next cycle)")

    def _scheduler(self) -> None:
        minutes = float(self.cfg["schedule"]["cycle_minutes"])
        while not self.stop_evt.is_set():
            last = parse_iso(db.kv_get("last_cycle_at"))
            due = last is None or (datetime.now(timezone.utc) - last).total_seconds() >= minutes * 60
            if due and not self.cycle_running():
                self._do_cycle()
            # Wall-clock based, so a laptop waking from sleep catches up within ~30 s.
            self.stop_evt.wait(30)

    def start(self) -> None:
        db.init_db()
        log.info("database: %s", db.describe())
        log.info("redis: %s", redis_store.ping())
        self._threads = [threading.Thread(target=self._scheduler, name="scheduler", daemon=True)]
        if self.tg.has_token and self.tg.configured:
            self._threads.append(threading.Thread(
                target=telegram_bot.poll_forever, args=(self.tg, self.cfg, self.stop_evt, self.base_url),
                name="telegram", daemon=True))
        elif self.tg.has_token:
            log.warning("TELEGRAM_CHAT_ID is empty: run `make chat-id` to finish Telegram setup")
            self._threads.append(threading.Thread(
                target=telegram_bot.poll_forever, args=(self.tg, self.cfg, self.stop_evt, self.base_url),
                name="telegram-setup", daemon=True))
        else:
            log.warning("No TELEGRAM_BOT_TOKEN: alerts disabled, dashboard only")
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self.stop_evt.set()


def build_app() -> Any:
    from .web.app import create_app
    cfg = config.load_config()
    svc = Service(cfg)

    @asynccontextmanager
    async def lifespan(app: Any):
        svc.start()
        yield
        svc.stop()

    return create_app(cfg, svc.run_cycle_now, svc.cycle_running, lifespan=lifespan)


def main() -> None:
    import uvicorn
    config.ensure_dirs()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full URLs, and Telegram's URL contains the bot token: keep it out of the log file.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    log.info("starting on http://%s:%s", config.HOST, config.PORT)
    uvicorn.run(build_app(), host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
