from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from client import (
    ApiResponse,
    RateLimitInfo,
    SolvoCardClient,
    is_topup_success,
    is_transient_topup_failure,
    new_idempotency_key,
    rate_limit_wait_seconds,
    response_message,
)
from panel_state import derive_retry_settings


LogFn = Callable[[str, dict], None]

WINDOW_RETRY_SECONDS = 2.5
WINDOW_RETRY_MAX = 5.0
RATE_LIMIT_BACKOFF_INITIAL = 8.0
RATE_LIMIT_BACKOFF_MAX = 15.0
RATE_LIMIT_GROWTH = 1.15


@dataclass
class RetrySettings:
    interval_seconds: float = 2.0
    max_interval_seconds: float = 6.0
    backoff_multiplier: float = 1.15
    jitter_seconds: float = 0.35


@dataclass
class SharedRateLimit:
    blocked_until: float = 0.0
    cooldown_seconds: float = WINDOW_RETRY_SECONDS
    consecutive_hits: int = 0
    last_kind: str = ""


AUTH_FAILURE_CODES = frozenset({401, 403})

RAMP_100_CENTS = 10_000
RAMP_250_CENTS = 25_000


def build_topup_plan(target_cents: int) -> list[int]:
    if target_cents <= 0:
        return []
    if target_cents <= RAMP_100_CENTS:
        return [target_cents]

    chunks = [RAMP_100_CENTS]
    remaining = target_cents - RAMP_100_CENTS

    if remaining <= RAMP_250_CENTS:
        chunks.append(remaining)
        return chunks

    chunks.append(RAMP_250_CENTS)
    remaining -= RAMP_250_CENTS

    while remaining >= RAMP_250_CENTS:
        chunks.append(RAMP_250_CENTS)
        remaining -= RAMP_250_CENTS

    if remaining >= RAMP_100_CENTS:
        chunks.append(RAMP_100_CENTS)
        remaining -= RAMP_100_CENTS

    if remaining > 0:
        chunks.append(remaining)

    return chunks


@dataclass
class WorkerState:
    card_id: str
    label: str
    amount_cents: int
    topped_up_cents: int = 0
    plan: list[int] = field(default_factory=list)
    plan_index: int = 0
    running: bool = False
    attempts: int = 0
    last_status: int | None = None
    last_message: str = ""
    last_idempotency_key: str = ""
    succeeded: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    @property
    def current_try_cents(self) -> int:
        if self.plan and self.plan_index < len(self.plan):
            return self.plan[self.plan_index]
        return self.amount_cents


def _wait_seconds(response: ApiResponse, settings: RetrySettings, attempt_interval: float) -> float:
    rl = response.rate_limit
    if rl.remaining == 0 and rl.reset_seconds:
        reset = rate_limit_wait_seconds(response)
        if reset is not None:
            return reset
    return attempt_interval


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
        self._topup_lock = threading.Lock()

    def _log(self, message: str, **extra: object) -> None:
        self.on_log(message, extra)

    def _rate_limit_remaining(self) -> float:
        return max(0.0, self.shared_rate_limit.blocked_until - time.time())

    def _wait_global_rate_limit(self, worker: WorkerState) -> bool:
        last_logged = 0.0
        while not worker.stop_event.is_set():
            remaining = self._rate_limit_remaining()
            if remaining <= 0:
                with self._rate_lock:
                    self.shared_rate_limit.consecutive_hits = 0
                return True
            worker.last_message = f"Waiting — retry in {remaining:.0f}s"
            if time.time() - last_logged >= 5:
                self._log(
                    f"[{worker.label}] rate limit active — resuming in {remaining:.0f}s",
                    card_id=worker.card_id,
                    sleep=remaining,
                    level="warn",
                )
                last_logged = time.time()
            if worker.stop_event.wait(timeout=min(remaining, 5)):
                return False
        return False

    def _register_throttle(self, response: ApiResponse) -> float:
        header_wait = rate_limit_wait_seconds(response)
        now = time.time()
        is_429 = response.status_code == 429
        kind = "rate_limit" if is_429 else "window"

        with self._rate_lock:
            if self.shared_rate_limit.blocked_until > now + 1:
                return self.shared_rate_limit.blocked_until - now

            if kind != self.shared_rate_limit.last_kind:
                self.shared_rate_limit.consecutive_hits = 0
            self.shared_rate_limit.last_kind = kind
            self.shared_rate_limit.consecutive_hits += 1
            hits = self.shared_rate_limit.consecutive_hits

            if header_wait is not None:
                cap = RATE_LIMIT_BACKOFF_MAX if is_429 else WINDOW_RETRY_MAX
                wait = min(header_wait, cap)
            elif is_429:
                wait = min(
                    RATE_LIMIT_BACKOFF_MAX,
                    RATE_LIMIT_BACKOFF_INITIAL * (RATE_LIMIT_GROWTH ** max(0, hits - 1)),
                )
            else:
                wait = min(WINDOW_RETRY_MAX, WINDOW_RETRY_SECONDS + hits * 0.35)

            self.shared_rate_limit.cooldown_seconds = wait
            self.shared_rate_limit.blocked_until = max(self.shared_rate_limit.blocked_until, now + wait)
            return self.shared_rate_limit.blocked_until - now

    def _clear_rate_limit_streak(self) -> None:
        with self._rate_lock:
            self.shared_rate_limit.consecutive_hits = 0
            self.shared_rate_limit.last_kind = ""
            self.shared_rate_limit.cooldown_seconds = WINDOW_RETRY_SECONDS

    def _apply_rate_limit(self, rate_limit: RateLimitInfo) -> None:
        derived = derive_retry_settings(rate_limit)
        self.retry_settings.interval_seconds = min(3.0, float(derived["interval_seconds"]))
        self.retry_settings.max_interval_seconds = min(8.0, float(derived["max_interval_seconds"]))
        self.retry_settings.backoff_multiplier = min(1.2, float(derived["backoff_multiplier"]))
        self.retry_settings.jitter_seconds = min(0.5, float(derived["jitter_seconds"]))

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
            rows = []
            for w in self.workers.values():
                try_cents = w.current_try_cents
                rows.append(
                    {
                        "card_id": w.card_id,
                        "label": w.label,
                        "amount_cents": w.amount_cents,
                        "target_amount_cents": w.amount_cents,
                        "topped_up_cents": w.topped_up_cents,
                        "current_try_cents": try_cents,
                        "plan_total": len(w.plan),
                        "plan_index": w.plan_index,
                        "running": w.running,
                        "attempts": w.attempts,
                        "last_status": w.last_status,
                        "last_message": w.last_message,
                        "last_idempotency_key": w.last_idempotency_key,
                        "succeeded": w.succeeded,
                        "rate_limit_wait": rate_wait if rate_wait > 0 else 0,
                    }
                )
            return rows

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
            worker.topped_up_cents = 0
            worker.plan_index = 0
            worker.plan = build_topup_plan(worker.amount_cents)
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
        target = worker.amount_cents
        plan_summary = " → ".join(f"${c / 100:.0f}" for c in worker.plan) if len(worker.plan) > 1 else f"${target / 100:.2f}"
        self._log(
            f"Started retry loop for {worker.label} (target ${target / 100:.2f}, plan: {plan_summary})",
            card_id=worker.card_id,
        )

        while not worker.stop_event.is_set():
            if not self._wait_global_rate_limit(worker):
                break

            try_cents = worker.current_try_cents
            worker.attempts += 1
            idempotency_key = new_idempotency_key()
            worker.last_idempotency_key = idempotency_key
            progress = f"${worker.topped_up_cents / 100:.2f}/${target / 100:.2f}"
            self._log(
                f"[{worker.label}] attempt {worker.attempts}: topup ${try_cents / 100:.2f} "
                f"({progress}, step {worker.plan_index + 1}/{len(worker.plan)}, key={idempotency_key})",
                card_id=worker.card_id,
                attempt=worker.attempts,
                level="info",
            )

            try:
                with self._topup_lock:
                    if not self._wait_global_rate_limit(worker):
                        break
                    response = self.client.topup(worker.card_id, try_cents, idempotency_key)
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
                worker.last_message = response_message(response)

                if response.rate_limit.limit or response.rate_limit.reset_seconds:
                    if not is_transient_topup_failure(response):
                        self._apply_rate_limit(response.rate_limit)

                rl = response.rate_limit
                rl_note = ""
                if rl.limit is not None:
                    rl_note = f" rate={rl.remaining}/{rl.limit}"

                if is_transient_topup_failure(response):
                    wait = self._register_throttle(response)
                    if response.status_code == 429:
                        worker.last_message = f"Rate limited — retry in {wait:.0f}s"
                        pause_label = "rate limited"
                    else:
                        worker.last_message = f"Window closed — retry in {wait:.0f}s"
                        pause_label = "window closed"
                    self._log(
                        f"[{worker.label}] {pause_label} ({response.status_code}): {response_message(response)} — "
                        f"retrying in {wait:.0f}s",
                        card_id=worker.card_id,
                        attempt=worker.attempts,
                        status=response.status_code,
                        sleep=wait,
                        level="warn",
                    )
                    continue

                if is_topup_success(response):
                    self._clear_rate_limit_streak()
                    worker.topped_up_cents += try_cents
                    worker.plan_index += 1
                    if worker.plan_index >= len(worker.plan):
                        rl_note = ""
                        if rl.limit is not None:
                            rl_note = f" rate={rl.remaining}/{rl.limit}"
                        self._complete_worker(
                            worker,
                            succeeded=True,
                            message=(
                                f"[{worker.label}] topup complete — ${worker.topped_up_cents / 100:.2f} "
                                f"on attempt {worker.attempts}{rl_note}"
                            ),
                            level="success",
                        )
                        return

                    next_cents = worker.current_try_cents
                    worker.last_message = (
                        f"Topped up ${try_cents / 100:.2f} — "
                        f"${worker.topped_up_cents / 100:.2f}/${target / 100:.2f}, next ${next_cents / 100:.2f}"
                    )
                    self._log(
                        f"[{worker.label}] step {worker.plan_index}/{len(worker.plan)} done "
                        f"(${try_cents / 100:.2f}) — trying ${next_cents / 100:.2f} next",
                        card_id=worker.card_id,
                        level="success",
                    )
                    continue

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
