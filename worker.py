from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from client import ApiResponse, RateLimitInfo, SolvoCardClient, is_topup_success, new_idempotency_key, rate_limit_wait_seconds
from panel_state import derive_retry_settings


LogFn = Callable[[str, dict], None]

RATE_LIMIT_INITIAL_COOLDOWN = 60.0
RATE_LIMIT_MIN_COOLDOWN = 30.0
RATE_LIMIT_MAX_COOLDOWN = 900.0
RATE_LIMIT_GROWTH = 1.6


@dataclass
class RetrySettings:
    interval_seconds: float = 3.0
    max_interval_seconds: float = 45.0
    backoff_multiplier: float = 1.4
    jitter_seconds: float = 0.75


@dataclass
class SharedRateLimit:
    blocked_until: float = 0.0
    cooldown_seconds: float = RATE_LIMIT_INITIAL_COOLDOWN
    consecutive_429: int = 0


AUTH_FAILURE_CODES = frozenset({401, 403})


@dataclass
class WorkerState:
    card_id: str
    label: str
    amount_cents: int
    running: bool = False
    attempts: int = 0
    last_status: int | None = None
    last_message: str = ""
    last_idempotency_key: str = ""
    succeeded: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


def _wait_seconds(response: ApiResponse, settings: RetrySettings, attempt_interval: float) -> float:
    rl = response.rate_limit
    if rl.remaining == 0 and rl.reset_seconds:
        reset = rate_limit_wait_seconds(response)
        if reset is not None:
            return reset
    return attempt_interval


def _format_response(response: ApiResponse) -> str:
    if isinstance(response.data, dict):
        err = response.data.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
        for key in ("message", "error", "detail", "code"):
            if key in response.data:
                val = response.data[key]
                if isinstance(val, str) and val.strip():
                    return val.strip()
    text = response.text.strip()
    if text:
        return text.splitlines()[0][:240]
    return f"HTTP {response.status_code}"


class TopupManager:
    def __init__(
        self,
        client: SolvoCardClient,
        retry_settings: RetrySettings | None = None,
        on_log: LogFn | None = None,
    ) -> None:
        self.client = client
        self.retry_settings = retry_settings or RetrySettings()
        self.on_log = on_log or (lambda _msg, _extra: None)
        self.workers: dict[str, WorkerState] = {}
        self.shared_rate_limit = SharedRateLimit()
        self._lock = threading.Lock()
        self._rate_lock = threading.Lock()

    def _log(self, message: str, **extra: object) -> None:
        self.on_log(message, extra)

    def _rate_limit_remaining(self) -> float:
        return max(0.0, self.shared_rate_limit.blocked_until - time.time())

    def _wait_global_rate_limit(self, worker: WorkerState) -> bool:
        remaining = self._rate_limit_remaining()
        if remaining <= 0:
            return True
        worker.last_status = 429
        worker.last_message = f"Rate limited — retry in {remaining:.0f}s"
        self._log(
            f"[{worker.label}] rate limit active — resuming in {remaining:.0f}s",
            card_id=worker.card_id,
            sleep=remaining,
            level="warn",
        )
        return not worker.stop_event.wait(timeout=remaining)

    def _register_429(self, response: ApiResponse) -> float:
        wait = rate_limit_wait_seconds(response)
        with self._rate_lock:
            if wait is None:
                self.shared_rate_limit.consecutive_429 += 1
                wait = min(
                    RATE_LIMIT_MAX_COOLDOWN,
                    RATE_LIMIT_INITIAL_COOLDOWN
                    * (RATE_LIMIT_GROWTH ** max(0, self.shared_rate_limit.consecutive_429 - 1)),
                )
            else:
                self.shared_rate_limit.consecutive_429 += 1
                wait = min(RATE_LIMIT_MAX_COOLDOWN, max(RATE_LIMIT_MIN_COOLDOWN, wait))
            self.shared_rate_limit.cooldown_seconds = wait
            self.shared_rate_limit.blocked_until = max(self.shared_rate_limit.blocked_until, time.time() + wait)
            return wait

    def _clear_rate_limit_streak(self) -> None:
        with self._rate_lock:
            self.shared_rate_limit.consecutive_429 = 0
            self.shared_rate_limit.cooldown_seconds = RATE_LIMIT_INITIAL_COOLDOWN

    def _apply_rate_limit(self, rate_limit: RateLimitInfo) -> None:
        derived = derive_retry_settings(rate_limit)
        self.retry_settings.interval_seconds = float(derived["interval_seconds"])
        self.retry_settings.max_interval_seconds = float(derived["max_interval_seconds"])
        self.retry_settings.backoff_multiplier = float(derived["backoff_multiplier"])
        self.retry_settings.jitter_seconds = float(derived["jitter_seconds"])

    def register_card(self, card_id: str, label: str, amount_cents: int) -> WorkerState:
        with self._lock:
            if card_id not in self.workers:
                self.workers[card_id] = WorkerState(card_id=card_id, label=label, amount_cents=amount_cents)
            else:
                worker = self.workers[card_id]
                worker.label = label
                worker.amount_cents = amount_cents
            return self.workers[card_id]

    def set_amount(self, card_id: str, amount_cents: int) -> bool:
        with self._lock:
            worker = self.workers.get(card_id)
            if not worker:
                return False
            worker.amount_cents = amount_cents
            return True

    def snapshot(self) -> list[dict]:
        rate_wait = round(self._rate_limit_remaining(), 1)
        with self._lock:
            return [
                {
                    "card_id": w.card_id,
                    "label": w.label,
                    "amount_cents": w.amount_cents,
                    "running": w.running,
                    "attempts": w.attempts,
                    "last_status": w.last_status,
                    "last_message": w.last_message,
                    "last_idempotency_key": w.last_idempotency_key,
                    "succeeded": w.succeeded,
                    "rate_limit_wait": rate_wait if rate_wait > 0 else 0,
                }
                for w in self.workers.values()
            ]

    def stop(self, card_id: str | None = None) -> None:
        with self._lock:
            targets = [self.workers[card_id]] if card_id else list(self.workers.values())
        for worker in targets:
            worker.stop_event.set()
            worker.running = False
            self._log(f"Stop requested for {worker.label}", card_id=worker.card_id)

    def _complete_worker(self, worker: WorkerState, *, succeeded: bool, message: str, level: str = "success") -> None:
        worker.stop_event.set()
        worker.running = False
        worker.succeeded = succeeded
        self._log(message, card_id=worker.card_id, level=level)

    def start(self, card_id: str, *, force: bool = False) -> bool:
        with self._lock:
            worker = self.workers.get(card_id)
            if not worker:
                return False
            if worker.running:
                return True
            if worker.succeeded and not force:
                return False
            worker.stop_event.clear()
            worker.running = True
            worker.succeeded = False
            worker.thread = threading.Thread(
                target=self._run_worker,
                args=(worker,),
                daemon=True,
                name=f"topup-{card_id[:8]}",
            )
            worker.thread.start()
            return True

    def start_all(self, *, force: bool = False) -> None:
        with self._lock:
            ids = list(self.workers.keys())
        for card_id in ids:
            worker = self.workers.get(card_id)
            if not worker or worker.running:
                continue
            if worker.succeeded and not force:
                continue
            self.start(card_id, force=force)

    def _run_worker(self, worker: WorkerState) -> None:
        settings = self.retry_settings
        interval = settings.interval_seconds
        self._log(
            f"Started retry loop for {worker.label} (${worker.amount_cents / 100:.2f}) — runs until success",
            card_id=worker.card_id,
        )

        while not worker.stop_event.is_set():
            if not self._wait_global_rate_limit(worker):
                break

            worker.attempts += 1
            idempotency_key = new_idempotency_key()
            worker.last_idempotency_key = idempotency_key
            self._log(
                f"[{worker.label}] attempt {worker.attempts}: topup ${worker.amount_cents / 100:.2f} (key={idempotency_key})",
                card_id=worker.card_id,
                attempt=worker.attempts,
                level="info",
            )

            try:
                response = self.client.topup(worker.card_id, worker.amount_cents, idempotency_key)
            except Exception as exc:
                worker.last_status = None
                worker.last_message = str(exc)
                self._log(
                    f"[{worker.label}] attempt {worker.attempts} network error: {exc}",
                    card_id=worker.card_id,
                    attempt=worker.attempts,
                    level="error",
                )
                sleep_for = min(interval, settings.max_interval_seconds)
                interval = min(interval * settings.backoff_multiplier, settings.max_interval_seconds)
            else:
                worker.last_status = response.status_code
                worker.last_message = _format_response(response)

                if response.rate_limit.limit or response.rate_limit.reset_seconds:
                    self._apply_rate_limit(response.rate_limit)

                rl = response.rate_limit
                rl_note = ""
                if rl.limit is not None:
                    rl_note = f" rate={rl.remaining}/{rl.limit}"

                if response.status_code == 429:
                    wait = self._register_429(response)
                    rate_msg = _format_response(response)
                    worker.last_message = f"Rate limited — retry in {wait:.0f}s"
                    self._log(
                        f"[{worker.label}] rate limited (429): {rate_msg} — "
                        f"pausing {wait:.0f}s for all topups (auto-adapting)",
                        card_id=worker.card_id,
                        attempt=worker.attempts,
                        status=429,
                        sleep=wait,
                        level="warn",
                    )
                    continue

                if is_topup_success(response):
                    self._clear_rate_limit_streak()
                    rl_note = ""
                    if rl.limit is not None:
                        rl_note = f" rate={rl.remaining}/{rl.limit}"
                    self._complete_worker(
                        worker,
                        succeeded=True,
                        message=f"[{worker.label}] topup succeeded on attempt {worker.attempts}{rl_note} — stopped",
                        level="success",
                    )
                    return

                auth_failure = response.status_code in AUTH_FAILURE_CODES
                self._log(
                    f"[{worker.label}] attempt {worker.attempts} failed ({response.status_code}){rl_note}: {worker.last_message} (key={idempotency_key})",
                    card_id=worker.card_id,
                    attempt=worker.attempts,
                    status=response.status_code,
                    level="error" if auth_failure else "warn",
                )

                if auth_failure:
                    self._complete_worker(
                        worker,
                        succeeded=False,
                        message=f"[{worker.label}] session expired — stopped (reconnect in Session tab)",
                        level="error",
                    )
                    return

                sleep_for = _wait_seconds(response, settings, interval)
                interval = min(interval * settings.backoff_multiplier, settings.max_interval_seconds)

            sleep_for += random.uniform(0, settings.jitter_seconds)
            self._log(
                f"[{worker.label}] waiting {sleep_for:.1f}s before retry (next cap {interval:.1f}s)",
                card_id=worker.card_id,
                sleep=sleep_for,
            )
            if worker.stop_event.wait(timeout=sleep_for):
                break

        worker.running = False
        self._log(f"Worker stopped for {worker.label}", card_id=worker.card_id)
