#!/usr/bin/env python3

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

from flask import Flask, Response, jsonify, make_response, render_template, request

from client import SolvoCardClient, parse_curl_command, repair_supabase_cookie
from panel_state import (
    append_log as persist_log_entry,
    apply_rate_limit,
    get_amount,
    get_cookies,
    load_logs,
    load_state,
    retry_settings_from_state,
    save_state,
    set_amount,
    set_cookies,
    storage_info,
)
from worker import TopupManager

app = Flask(__name__)
log_queue: queue.Queue = queue.Queue(maxsize=500)
manager: TopupManager | None = None
client: SolvoCardClient | None = None
state_lock = threading.Lock()
dashboard_cache: dict[str, Any] = {"updated_at": None, "cards": [], "transactions": []}


def emit_log(message: str, extra: dict | None = None) -> None:
    payload = {"ts": time.time(), "message": message, **(extra or {})}
    try:
        log_queue.put_nowait(payload)
    except queue.Full:
        try:
            log_queue.get_nowait()
        except queue.Empty:
            pass
        log_queue.put_nowait(payload)
    try:
        persist_log_entry(payload)
    except OSError:
        pass


def rebuild_runtime(stop_workers: bool = False) -> None:
    global client, manager
    if stop_workers and manager:
        manager.stop()
    state = load_state()
    client = SolvoCardClient.from_config({"cookies": get_cookies(state), "impersonate": "chrome146"})
    manager = TopupManager(client, retry_settings=retry_settings_from_state(state), on_log=emit_log)


def sync_workers(cards: list[dict[str, Any]]) -> None:
    if not manager:
        return
    active = {c["id"] for c in cards}
    for card in cards:
        manager.register_card(card["id"], card.get("label", card["id"][:8]), int(card["amount_cents"]))
    for card_id in list(manager.workers.keys()):
        if card_id not in active:
            manager.stop(card_id)


def attach_worker_snapshots(cards: list[dict[str, Any]], active_manager: TopupManager | None) -> None:
    if not active_manager:
        return
    snap_map = {w["card_id"]: w for w in active_manager.snapshot()}
    for card in cards:
        if snap := snap_map.get(card["id"]):
            card["worker"] = snap


def invalidate_dashboard_cache() -> None:
    global dashboard_cache
    dashboard_cache = {"updated_at": None, "cards": [], "transactions": []}


def workers_snapshot() -> list[dict[str, Any]]:
    return manager.snapshot() if manager else []


def _normalize_cookies(raw: dict[str, str]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        cookies[name] = repair_supabase_cookie(value) if name.endswith("-auth-token") else value
    return cookies


def _cookies_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    if payload.get("cookies"):
        return _normalize_cookies(payload["cookies"])
    curl = payload.get("curl", "").strip()
    if curl.startswith("{"):
        return _normalize_cookies(json.loads(curl))
    if curl:
        return _normalize_cookies(parse_curl_command(curl))
    return {}


def _connect_session(cookies: dict[str, str]) -> dict[str, Any]:
    global client, manager
    if not cookies:
        raise ValueError("No cookies provided")

    with state_lock:
        state = load_state()
        set_cookies(state, cookies)
        save_state(state)
        rebuild_runtime(stop_workers=True)
        active_client = client
        active_manager = manager

    data = refresh_dashboard(active_client, active_manager)
    emit_log(f"Session connected — {data.get('card_count', 0)} card(s) loaded", {"level": "success"})
    return data


def refresh_dashboard(
    active_client: SolvoCardClient | None = None,
    active_manager: TopupManager | None = None,
) -> dict[str, Any]:
    global dashboard_cache
    state = load_state()
    cookies = get_cookies(state)
    active_client = active_client or client
    active_manager = active_manager or manager
    auth_ok = bool(active_client and active_client._access_token()) if active_client else False
    load_error = ""

    if not active_client or not cookies:
        dashboard_cache = {
            "updated_at": time.time(),
            "auth_ok": False,
            "has_cookies": bool(cookies),
            "load_error": "Connect your session in the Session tab." if not cookies else "Invalid session — missing auth token.",
            "card_count": 0,
            "cards": [],
            "transactions": [],
            "panel": state,
            "storage": storage_info(),
        }
        return dashboard_cache

    if not auth_ok:
        load_error = "Auth token missing or invalid — paste fresh cookies from solvocard.com."

    page = active_client._request("GET", "/dashboard/cards", json_body=False)
    if page.rate_limit.limit or page.rate_limit.reset_seconds:
        apply_rate_limit(state, page.rate_limit)
        state = load_state()

    if not page.ok:
        load_error = load_error or f"Dashboard returned HTTP {page.status_code}. Cookies may be expired — copy a fresh curl from DevTools."

    card_ids = active_client.discover_card_ids() if page.ok else []
    if page.ok and not card_ids:
        load_error = load_error or "No cards found. Make sure cf_clearance and sb-*-auth-token are included."

    cards_out: list[dict[str, Any]] = []
    all_transactions: list[dict[str, Any]] = []

    for card_id in card_ids:
        details_resp = active_client.get_card_details(card_id)
        if details_resp.rate_limit.limit or details_resp.rate_limit.reset_seconds:
            apply_rate_limit(state, details_resp.rate_limit)
        parsed = SolvoCardClient.parse_card(details_resp)
        worker_snap = (
            next((w for w in active_manager.snapshot() if w["card_id"] == card_id), None) if active_manager else None
        )
        cards_out.append(
            {
                "id": card_id,
                "label": parsed.get("label") or card_id[:8],
                "amount_cents": None,
                "api_ok": details_resp.ok,
                "api_status": details_resp.status_code,
                "worker": worker_snap,
                **{k: v for k, v in parsed.items() if k != "transactions"},
            }
        )
        all_transactions.extend(
            SolvoCardClient.normalize_transactions(
                parsed.get("transactions", []),
                card_id=card_id,
                card_label=parsed.get("label") or card_id[:8],
            )
        )

    cards_out.sort(key=lambda c: (c.get("balance_cents") is None, c.get("balance_cents") or 0))
    for idx, card in enumerate(cards_out, start=1):
        card["rank"] = idx
        card["is_lowest_balance"] = idx == 1
        last4 = card.get("last4")
        if not card.get("label"):
            card["label"] = f"•••• {last4}" if last4 else card["id"][:8]
        suggested = 500 if card["is_lowest_balance"] else 2500
        card["suggested_amount_cents"] = suggested
        saved = get_amount(state, card["id"])
        card["amount_cents"] = saved if saved is not None else suggested
        if worker_snap := card.get("worker"):
            card["amount_cents"] = worker_snap["amount_cents"]

    label_by_id = {card["id"]: card["label"] for card in cards_out}
    for tx in all_transactions:
        cid = tx.get("card_id")
        if cid in label_by_id:
            tx["card_label"] = label_by_id[cid]

    if active_manager:
        sync_workers(cards_out)
        attach_worker_snapshots(cards_out, active_manager)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for tx in sorted(all_transactions, key=lambda t: str(t.get("date") or ""), reverse=True):
        raw = tx.get("raw") or {}
        fp = json.dumps(
            [tx.get("card_id"), tx.get("date"), tx.get("amount_cents"), tx.get("merchant"), raw.get("id")],
            sort_keys=True,
            default=str,
        )
        if fp in seen:
            continue
        seen.add(fp)
        deduped.append(tx)

    dashboard_cache = {
        "updated_at": time.time(),
        "auth_ok": auth_ok and bool(card_ids),
        "has_cookies": bool(cookies),
        "load_error": load_error,
        "card_count": len(cards_out),
        "cards": cards_out,
        "transactions": deduped[:300],
        "panel": state,
        "storage": storage_info(),
    }
    return dashboard_cache


with state_lock:
    rebuild_runtime()


@app.get("/")
def index():
    resp = make_response(render_template("panel.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/api/dashboard")
def dashboard():
    force = request.args.get("force") == "1"
    with state_lock:
        cookies = get_cookies(load_state())
        cached = dashboard_cache
        running_workers = any(w.get("running") for w in workers_snapshot())
    if (
        not force
        and not running_workers
        and cached.get("updated_at")
        and time.time() - float(cached["updated_at"]) < 8
        and cached.get("has_cookies")
        and cached.get("card_count", 0) > 0
    ):
        return jsonify({"cookies": cookies, **cached})
    data = refresh_dashboard()
    return jsonify({"cookies": cookies, **data})


@app.get("/api/workers")
def workers():
    with state_lock:
        snap = workers_snapshot()
    return jsonify({"workers": snap})


@app.get("/api/logs")
def logs():
    return jsonify({"logs": load_logs()})


@app.get("/api/storage")
def storage():
    return jsonify(storage_info())


@app.get("/api/events")
def events():
    def stream():
        while True:
            try:
                item = log_queue.get(timeout=25)
                yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'message': 'heartbeat', 'level': 'info', 'ts': time.time()})}\n\n"

    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/start/<card_id>")
def start_card(card_id: str):
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force"))
    with state_lock:
        ok = manager.start(card_id, force=force) if manager else False
        snap = workers_snapshot()
    invalidate_dashboard_cache()
    return jsonify({"ok": ok, "workers": snap})


@app.post("/api/start-all")
def start_all():
    with state_lock:
        manager.start_all() if manager else None
        snap = workers_snapshot()
    invalidate_dashboard_cache()
    return jsonify({"ok": True, "workers": snap})


@app.post("/api/stop/<card_id>")
def stop_card(card_id: str):
    with state_lock:
        manager.stop(card_id) if manager else None
        snap = workers_snapshot()
    invalidate_dashboard_cache()
    return jsonify({"ok": True, "workers": snap})


@app.post("/api/stop-all")
def stop_all():
    with state_lock:
        manager.stop() if manager else None
        snap = workers_snapshot()
    invalidate_dashboard_cache()
    return jsonify({"ok": True, "workers": snap})


@app.post("/api/cards/<card_id>/amount")
def set_card_amount(card_id: str):
    payload = request.get_json(force=True)
    amount_cents = int(payload.get("amount_cents", 0))
    with state_lock:
        state = load_state()
        set_amount(state, card_id, amount_cents)
        save_state(state)
        if manager:
            manager.set_amount(card_id, amount_cents)
    emit_log(f"Topup amount set to ${amount_cents / 100:.2f}", {"level": "success"})
    return jsonify({"ok": True, "amount_cents": amount_cents})


@app.post("/api/import-curl")
def import_curl():
    payload = request.get_json(force=True)
    cookies = _cookies_from_payload({"curl": payload.get("curl", "")})
    return jsonify({"cookies": cookies, "count": len(cookies)})


@app.post("/api/config/cookies")
def update_cookies():
    payload = request.get_json(force=True)
    try:
        cookies = _cookies_from_payload(payload)
        if not cookies:
            return jsonify({"ok": False, "error": "No cookies found in request"}), 400
        data = _connect_session(cookies)
        return jsonify({"ok": True, "count": len(cookies), **data})
    except (json.JSONDecodeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/config/cookies/clear")
def clear_cookies():
    with state_lock:
        state = load_state()
        set_cookies(state, {})
        save_state(state)
        rebuild_runtime(stop_workers=True)
    refresh_dashboard()
    emit_log("Session cleared", {"level": "warn"})
    return jsonify({"ok": True})


@app.post("/api/config/retry")
def update_retry():
    return jsonify({"ok": True, "message": "Retry pacing is automatic from API rate limits"})


def run_panel(host: str = "127.0.0.1", port: int = 5050) -> None:
    with state_lock:
        refresh_dashboard()
    print(f"Open http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


def run_topup_cli(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Retry SolvoCard topups until success.")
    parser.add_argument("--card", action="append", help="Card UUID (optional override)")
    parser.add_argument("--amount", type=int, help="Amount in cents for --card")
    parser.add_argument("--once", action="store_true", help="Single attempt per card, no retry loop")
    args = parser.parse_args(argv)

    state = load_state()
    cookies = get_cookies(state)
    if not cookies:
        print("No cookies saved. Load session via the web panel first.", file=sys.stderr)
        print("Start the panel: run.bat", file=sys.stderr)
        sys.exit(1)

    cli_client = SolvoCardClient.from_config({"cookies": cookies, "impersonate": "chrome146"})

    def printer(message: str, extra: dict) -> None:
        level = extra.get("level", "info")
        prefix = {"success": "✓", "error": "!", "warn": "~"}.get(level, "·")
        print(f"{prefix} {message}")

    cli_manager = TopupManager(cli_client, retry_settings=retry_settings_from_state(state), on_log=printer)

    if args.card:
        amount = args.amount or 2500
        for card_id in args.card:
            cli_manager.register_card(card_id, card_id[:8], amount)
    else:
        card_ids = cli_client.discover_card_ids()
        if not card_ids:
            print("No cards discovered. Connect session in the web panel.", file=sys.stderr)
            sys.exit(1)
        cards_meta = []
        for card_id in card_ids:
            resp = cli_client.get_card_details(card_id)
            parsed = cli_client.parse_card(resp)
            cards_meta.append((card_id, parsed))
        cards_meta.sort(key=lambda x: (x[1].get("balance_cents") is None, x[1].get("balance_cents") or 0))
        for idx, (card_id, parsed) in enumerate(cards_meta, start=1):
            suggested = 500 if idx == 1 else 2500
            amount = get_amount(state, card_id, suggested) or suggested
            label = parsed.get("label") or card_id[:8]
            last4 = parsed.get("last4")
            if not parsed.get("label") and last4:
                label = f"•••• {last4}"
            cli_manager.register_card(card_id, label, amount)

    if not cli_manager.workers:
        print("No cards to top up.", file=sys.stderr)
        sys.exit(1)

    if args.once:
        for card_id, worker in cli_manager.workers.items():
            response = cli_client.topup(card_id, worker.amount_cents)
            print(f"{worker.label}: {response.status_code} {response.text[:200]}")
        return

    cli_manager.start_all()
    try:
        while any(w.running for w in cli_manager.workers.values()):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
        cli_manager.stop()


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="SolvoCard topup panel and CLI.")
    sub = parser.add_subparsers(dest="command")

    panel_parser = sub.add_parser("panel", help="Run the web panel (default)")
    panel_parser.add_argument("--host", default="127.0.0.1")
    panel_parser.add_argument("--port", type=int, default=5050)

    sub.add_parser("topup", help="Retry topups from the terminal")

    if len(sys.argv) > 1 and sys.argv[1] == "topup":
        run_topup_cli(sys.argv[2:])
        return

    args = parser.parse_args()
    if args.command in (None, "panel"):
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 5050)
        run_panel(host=host, port=port)


if __name__ == "__main__":
    main()
