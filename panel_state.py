from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from client import repair_supabase_cookie

STATE_PATH = Path(__file__).with_name("panel_state.json")
LOG_PATH = Path(__file__).with_name("panel_log.json")
MAX_LOG_ENTRIES = 400

DEFAULT_RETRY = {
    "interval_seconds": 2.0,
    "max_interval_seconds": 6.0,
    "backoff_multiplier": 1.15,
    "jitter_seconds": 0.35,
    "auto": True,
}


def derive_retry_settings(rate_limit: Any | None = None) -> dict[str, float]:
    settings = dict(DEFAULT_RETRY)
    if rate_limit is None:
        return settings

    limit = getattr(rate_limit, "limit", None)
    reset = getattr(rate_limit, "reset_seconds", None)
    if limit and reset and limit > 0:
        per_request = reset / limit
        settings["interval_seconds"] = max(1.5, round(per_request * 2.5, 2))
        settings["max_interval_seconds"] = max(float(reset), settings["interval_seconds"] * 4)
        settings["backoff_multiplier"] = 1.35
        settings["jitter_seconds"] = min(2.0, max(0.5, settings["interval_seconds"] * 0.2))
    elif reset:
        settings["max_interval_seconds"] = float(reset)
        settings["interval_seconds"] = max(2.0, reset / 10)
    return settings


def _repair_cookies(cookies: dict[str, str]) -> dict[str, str]:
    return {
        k: repair_supabase_cookie(v) if k.endswith("-auth-token") and isinstance(v, str) else v
        for k, v in cookies.items()
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"cookies": {}, "topup_amounts": {}, "retry": dict(DEFAULT_RETRY), "meta": {}}
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    data.setdefault("cookies", {})
    data.setdefault("topup_amounts", {})
    data.setdefault("retry", dict(DEFAULT_RETRY))
    data.setdefault("meta", {})
    data["cookies"] = _repair_cookies(data.get("cookies", {}))
    return data


def touch_meta(state: dict[str, Any], event: str | None = None) -> None:
    meta = state.setdefault("meta", {})
    meta["updated_at"] = time.time()
    if event:
        meta["last_event"] = event


def storage_info() -> dict[str, Any]:
    state = load_state()
    cookies = state.get("cookies", {})
    amounts = state.get("topup_amounts", {})
    meta = state.get("meta", {})
    state_exists = STATE_PATH.exists()
    log_exists = LOG_PATH.exists()
    return {
        "state_path": str(STATE_PATH.resolve()),
        "log_path": str(LOG_PATH.resolve()),
        "state_exists": state_exists,
        "log_exists": log_exists,
        "cookie_count": len(cookies),
        "topup_amount_count": len(amounts),
        "connected_at": meta.get("connected_at"),
        "updated_at": meta.get("updated_at"),
        "last_event": meta.get("last_event"),
    }


def save_state(data: dict[str, Any]) -> None:
    touch_meta(data)
    STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_cookies(state: dict[str, Any] | None = None) -> dict[str, str]:
    state = state or load_state()
    return state.get("cookies", {})


def set_cookies(state: dict[str, Any], cookies: dict[str, str]) -> None:
    state["cookies"] = _repair_cookies(cookies)
    meta = state.setdefault("meta", {})
    if cookies:
        meta["connected_at"] = time.time()
    touch_meta(state, "session_updated")


def get_amount(state: dict[str, Any], card_id: str, suggested: int | None = None) -> int | None:
    saved = state.get("topup_amounts", {}).get(card_id)
    if saved is not None:
        return int(saved)
    return suggested


def set_amount(state: dict[str, Any], card_id: str, amount_cents: int) -> None:
    state.setdefault("topup_amounts", {})[card_id] = int(amount_cents)
    touch_meta(state, "topup_amount_saved")


def set_retry(state: dict[str, Any], retry: dict[str, Any]) -> None:
    current = state.setdefault("retry", dict(DEFAULT_RETRY))
    current.update(retry)


def retry_settings_from_state(state: dict[str, Any] | None = None):
    from worker import RetrySettings

    state = state or load_state()
    raw = {**DEFAULT_RETRY, **state.get("retry", {})}
    return RetrySettings(
        interval_seconds=float(raw.get("interval_seconds", 3)),
        max_interval_seconds=float(raw.get("max_interval_seconds", 45)),
        backoff_multiplier=float(raw.get("backoff_multiplier", 1.4)),
        jitter_seconds=float(raw.get("jitter_seconds", 0.75)),
    )


def apply_rate_limit(state: dict[str, Any], rate_limit: Any | None) -> dict[str, float]:
    derived = derive_retry_settings(rate_limit)
    set_retry(state, derived)
    touch_meta(state, "retry_auto_tuned")
    save_state(state)
    return derived


def load_logs(limit: int = MAX_LOG_ENTRIES) -> list[dict[str, Any]]:
    if not LOG_PATH.exists():
        return []
    try:
        data = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data[-limit:]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def append_log(entry: dict[str, Any]) -> None:
    logs = load_logs(MAX_LOG_ENTRIES - 1)
    logs.append(entry)
    LOG_PATH.write_text(json.dumps(logs[-MAX_LOG_ENTRIES:], indent=2), encoding="utf-8")
