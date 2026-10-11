"""Settings: .env secrets + config.yaml preferences + file locations.

Profiles: one install can serve several people. Set JOBHUNTER_PROFILE=<name> and everything personal comes from
profiles/<name>/ instead of the project root: config.yaml, settings.yaml (written by the dashboard), resume_facts.yaml,
application_answers.yaml, its own .env (Telegram, database, port) and its own data folder. The default profile (no
variable set) is the project root, exactly as before. A profile never inherits another person's database or Telegram bot.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import dotenv_values, load_dotenv

ROOT = Path(os.environ.get("JOBHUNTER_HOME", Path(__file__).resolve().parent.parent))
PROFILE = re.sub(r"[^a-z0-9_-]", "", os.environ.get("JOBHUNTER_PROFILE", "").strip().lower())
PROFILES_DIR = ROOT / "profiles"
PROFILE_DIR = (PROFILES_DIR / PROFILE) if PROFILE else ROOT

load_dotenv(ROOT / ".env")
if PROFILE:
    _own = dotenv_values(PROFILE_DIR / ".env") if (PROFILE_DIR / ".env").exists() else {}
    for _k, _v in _own.items():
        if _v is not None:
            os.environ[_k] = _v
    # never fall back to somebody else's database, bot or port
    for _k in ("DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "JOBHUNTER_PORT", "JOBHUNTER_REQUIRE_LOGIN"):
        if _k not in _own:
            os.environ.pop(_k, None)

DATA_DIR = Path(os.environ.get("JOBHUNTER_DATA", PROFILE_DIR / "data"))
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "jobhunter.db"
PID_FILE = DATA_DIR / "service.pid"
CONFIG_PATH = PROFILE_DIR / "config.yaml"
SETTINGS_PATH = PROFILE_DIR / "settings.yaml"      # written by the dashboard's Settings page, overrides config.yaml
FACTS_PATH = PROFILE_DIR / "resume_facts.yaml"
ANSWERS_PATH = PROFILE_DIR / "application_answers.yaml"
EVERGREEN_PATH = PROFILE_DIR / "evergreen.yaml"

HOST = "127.0.0.1"
PORT = int(os.environ.get("JOBHUNTER_PORT", "8787"))
# Sign-in users for every profile live in one small shared file (see auth.py).
AUTH_DB = Path(os.environ.get("JOBHUNTER_AUTH_DB", ROOT / "auth.db"))
# Normally the login is only asked for when the site is reached through a tunnel; set JOBHUNTER_REQUIRE_LOGIN=1 in a
# profile's .env to ask on the Mac too.
REQUIRE_LOGIN = os.environ.get("JOBHUNTER_REQUIRE_LOGIN", "").strip().lower() in ("1", "true", "yes")
# The one public port: a small gateway that signs people in and forwards each person to their own profile's dashboard.
GATEWAY_PORT = int(os.environ.get("JOBHUNTER_GATEWAY_PORT", "8780"))
GATEWAY_DIR = ROOT / ".gateway"
# The address phones use (e.g. https://mac.tailnet.ts.net). Only changes the links in Telegram alerts.
PUBLIC_URL = os.environ.get("JOBHUNTER_PUBLIC_URL", "").strip().rstrip("/")

# Level schemes. Internal keys stay stable so stored data never needs migrating.
SCHEMES: Dict[str, Dict[str, Any]] = {
    "software": {"levels": ["intern", "sde1", "sde2", "sde3", "staff"], "entry": ["intern", "sde1"], "main_entry": "sde1",
                 "bands": {"sde1": [0, 1], "sde2": [2, 4], "sde3": [5, 8], "staff": [9, 99]}, "max_entry_years": 1,
                 "default_targets": ["intern", "sde1"]},
    "general": {"levels": ["intern", "entry", "mid", "senior", "lead"], "entry": ["intern", "entry"], "main_entry": "entry",
                "bands": {"entry": [0, 2], "mid": [3, 6], "senior": [7, 12], "lead": [13, 99]}, "max_entry_years": 2,
                "default_targets": ["intern", "entry"]},
}
LEVEL_LABELS = {"intern": "Intern", "sde1": "SDE-1", "sde2": "SDE-2", "sde3": "SDE-3", "staff": "Staff+",
                "entry": "Entry level", "mid": "Mid level", "senior": "Senior", "lead": "Lead / Head"}
LEVELS = SCHEMES["software"]["levels"]          # kept for older imports


def scheme_name(cfg: Dict[str, Any]) -> str:
    n = ((cfg.get("levels") or {}).get("scheme") or "software").lower()
    return n if n in SCHEMES else "software"


def level_scheme(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return SCHEMES[scheme_name(cfg)]


def database_url() -> str:
    """DATABASE_URL=postgresql://... uses Postgres; empty means a local SQLite file."""
    return os.environ.get("DATABASE_URL", "").strip()


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "").strip()


def redis_prefix() -> str:
    return f"jobhunter:{PROFILE}:" if PROFILE else "jobhunter:"


def redact(url: str) -> str:
    """Hide the password when printing a connection URL."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def secret(name: str) -> str:
    return os.environ.get(name, "").strip()


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def read_settings() -> Dict[str, Any]:
    if SETTINGS_PATH.exists():
        try:
            return yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def save_settings(data: Dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text("# Written by the dashboard's Settings page. It overrides config.yaml.\n" +
                             yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_config(path: Path | None = None) -> Dict[str, Any]:
    p = path or CONFIG_PATH
    cfg: Dict[str, Any] = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    if path is None:
        cfg = _merge(cfg, read_settings())
    cfg.setdefault("profile", {})
    cfg.setdefault("levels", {}).setdefault("scheme", "software")
    sch = level_scheme(cfg)
    t = cfg.setdefault("targets", {})
    t.setdefault("levels", list(sch["default_targets"]))
    t["levels"] = [l for l in t["levels"] if l in sch["levels"]] or list(sch["default_targets"])
    t.setdefault("apply_batch_rules", scheme_name(cfg) == "software")
    t.setdefault("max_years_for_entry", sch["max_entry_years"])
    t.setdefault("years_to_level", dict(sch["bands"]))
    loc = cfg.setdefault("locations", {})
    loc.setdefault("mode", "india")                 # india | india_or_remote | anywhere
    loc.setdefault("country", "India")
    loc.setdefault("include_remote_india", True)
    loc.setdefault("only_cities", [])
    cfg.setdefault("languages", {}).setdefault("known", ["english"])   # languages you can work in; edit config.yaml (languages: {known: [english, hindi]}) to add more
    cfg.setdefault("roles", {})
    cfg["roles"].setdefault("families", [])
    cfg["roles"].setdefault("include", [])
    cfg["roles"].setdefault("exclude_extra", [])
    cfg.setdefault("alerts", {}).setdefault("min_score", 55)
    cfg["alerts"].setdefault("include_interns", scheme_name(cfg) == "general")
    cfg["alerts"].setdefault("max_individual_alerts", 10)
    cfg.setdefault("schedule", {})
    cfg["schedule"].setdefault("cycle_minutes", 30)
    cfg["schedule"].setdefault("per_company_min_hours", 3)
    cfg["schedule"].setdefault("workers", 8)
    return cfg


def list_profiles() -> List[Dict[str, Any]]:
    """Every profile on this machine: the default one plus those under profiles/."""
    out = [{"slug": "", "name": "Default", "port": 8787, "dir": ROOT}]
    try:
        out[0]["name"] = (yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}).get("profile", {}).get("name") or "Default"
    except Exception:
        pass
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if not d.is_dir() or not (d / "config.yaml").exists():
                continue
            name, port = d.name, 0
            try:
                name = (yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8")) or {}).get("profile", {}).get("name") or d.name
                port = int(dotenv_values(d / ".env").get("JOBHUNTER_PORT") or 0)
            except Exception:
                pass
            out.append({"slug": d.name, "name": name, "port": port, "dir": d})
    return out


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
