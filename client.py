from __future__ import annotations

import base64
import json
import random
import re
import string
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from curl_cffi import requests as cffi_requests

DEFAULT_IMPERSONATE = "chrome146"


@dataclass
class RateLimitInfo:
    limit: int | None = None
    remaining: int | None = None
    reset_seconds: int | None = None
    retry_after: int | None = None


@dataclass
class ApiResponse:
    ok: bool
    status_code: int
    data: Any
    text: str
    rate_limit: RateLimitInfo
    headers: dict[str, str]


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = unquote(value.strip())
    return cookies


def parse_curl_command(curl_text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for line in curl_text.replace("\\\n", "\n").splitlines():
        line = line.strip()
        if line.startswith("-b ") or line.startswith("--cookie "):
            raw = line.split(" ", 1)[1].strip().strip("'\"")
            cookies.update(parse_cookie_header(raw))
        elif line.startswith("-H ") or line.startswith("--header "):
            header = line.split(" ", 1)[1].strip().strip("'\"")
            if header.lower().startswith("cookie:"):
                cookies.update(parse_cookie_header(header.split(":", 1)[1].strip()))
    return cookies


def repair_supabase_cookie(raw: str) -> str:
    if not raw.startswith("base64-"):
        return raw
    b64 = raw[7:]

    metadata_marker = "ImFwcF9tZXRhZGF0YSI6"
    if metadata_marker in b64:
        start = b64.find(metadata_marker)
        try:
            prefix = base64.b64decode(b64[:start] + "==").decode("utf-8")
            embedded = b64[start:]
            for pad in ("", "=", "==", "==="):
                try:
                    suffix = base64.b64decode(embedded + pad).decode("utf-8")
                    data = json.loads(prefix + suffix)
                    return "base64-" + base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    continue
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            pass

    identity_marker = "Gl0eV9pZCI6"
    if identity_marker in b64:
        start = b64.find(identity_marker)
        try:
            prefix = base64.b64decode(b64[:start] + "==").decode("utf-8")
            inner = b64[start + len(identity_marker) :]
            suffix = 'ity_id":' + base64.b64decode(inner + "=").decode("utf-8")
            data = json.loads(prefix + suffix)
            return "base64-" + base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            pass

    return raw


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def new_idempotency_key(prefix: str = "topup") -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
    return f"{prefix}_{int(time.time() * 1000)}_{suffix}"


def _decode_supabase_cookie(raw: str) -> dict[str, Any]:
    raw = repair_supabase_cookie(raw)
    value = raw[7:] if raw.startswith("base64-") else raw
    padded = value + "=" * (-len(value) % 4)
    return json.loads(base64.b64decode(padded))


def _parse_rate_limit(headers: dict[str, str]) -> RateLimitInfo:
    lower = {k.lower(): v for k, v in headers.items()}

    def as_int(key: str) -> int | None:
        val = lower.get(key)
        if val is None:
            return None
        val = str(val).strip()
        if val.lower() in ("nan", "null", "undefined", "none", ""):
            return None
        try:
            num = float(val)
        except ValueError:
            return None
        if num != num or num < 0:
            return None
        return int(num)

    return RateLimitInfo(
        limit=as_int("x-ratelimit-limit"),
        remaining=as_int("x-ratelimit-remaining"),
        reset_seconds=as_int("x-ratelimit-reset"),
        retry_after=as_int("retry-after"),
    )


def _positive_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num <= 0:
        return None
    return num


def rate_limit_wait_seconds(response: ApiResponse) -> float | None:
    rl = response.rate_limit
    for candidate in (rl.retry_after, rl.reset_seconds):
        seconds = _positive_seconds(candidate)
        if seconds is None:
            continue
        if seconds > 1_000_000_000:
            seconds = max(0.0, seconds - time.time())
        if seconds > 0:
            return seconds

    data = response.data
    if isinstance(data, dict):
        for key in ("retryAfter", "retry_after", "waitSeconds", "wait_seconds", "retryIn", "retry_in", "cooldown"):
            seconds = _positive_seconds(data.get(key))
            if seconds is not None:
                return seconds
        err = data.get("error")
        if isinstance(err, dict):
            for key in ("retryAfter", "retry_after", "waitSeconds", "wait_seconds"):
                seconds = _positive_seconds(err.get(key))
                if seconds is not None:
                    return seconds
    return None


def _money_to_cents(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            return int(round(value * 100))
        if abs(value) >= 10000:
            return int(value)
        return int(round(value * 100))
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        if not cleaned:
            return None
        try:
            if "." in cleaned:
                return int(round(float(cleaned) * 100))
            return int(cleaned)
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("amount", "value", "cents", "available", "balance"):
            if key in value:
                parsed = _money_to_cents(value[key])
                if parsed is not None:
                    return parsed
    return None


def _pick_first(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _display_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "label", "title", "description", "merchantName"):
            if key in value and value[key] not in (None, ""):
                nested = _display_text(value[key])
                if nested:
                    return nested
    return None


def _format_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    text = value.strip()
    if not text:
        return None
    if "T" in text:
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return text


class SolvoCardClient:
    def __init__(
        self,
        cookies: dict[str, str],
        base_url: str = "https://www.solvocard.com",
        timeout: float = 60.0,
        impersonate: str = DEFAULT_IMPERSONATE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = dict(cookies)
        for name, value in list(self.cookies.items()):
            if name.endswith("-auth-token") and isinstance(value, str):
                self.cookies[name] = repair_supabase_cookie(value)
        self.timeout = timeout
        self.impersonate = impersonate
        self.session = cffi_requests.Session()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SolvoCardClient":
        return cls(
            cookies=config.get("cookies", {}),
            base_url=config.get("base_url", "https://www.solvocard.com"),
            impersonate=config.get("impersonate", DEFAULT_IMPERSONATE),
        )

    def _access_token(self) -> str | None:
        for name, value in self.cookies.items():
            if name.endswith("-auth-token") and value:
                try:
                    payload = _decode_supabase_cookie(value)
                    token = payload.get("access_token")
                    if isinstance(token, str):
                        return token
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue
        return None

    def _headers(self, referer: str | None = None, json_body: bool = True) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.5",
            "origin": self.base_url,
            "referer": referer or f"{self.base_url}/dashboard/cards",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if json_body:
            headers["content-type"] = "application/json"
        token = self._access_token()
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        referer: str | None = None,
        json_body: bool = True,
        **kwargs: Any,
    ) -> ApiResponse:
        url = f"{self.base_url}{path}"
        response = self.session.request(
            method,
            url,
            headers=self._headers(referer, json_body=json_body),
            cookies=self.cookies,
            impersonate=self.impersonate,
            timeout=self.timeout,
            **kwargs,
        )
        text = response.text or ""
        data: Any = None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                data = response.json()
            except json.JSONDecodeError:
                data = None
        rate_limit = _parse_rate_limit(dict(response.headers))
        ok = 200 <= response.status_code < 300
        return ApiResponse(
            ok=ok,
            status_code=response.status_code,
            data=data,
            text=text,
            rate_limit=rate_limit,
            headers=dict(response.headers),
        )

    def get_card_details(self, card_id: str) -> ApiResponse:
        referer = f"{self.base_url}/dashboard/cards/{card_id}"
        return self._request("GET", f"/api/cards/{card_id}/details", referer=referer, json_body=False)

    def get_transactions(self, limit: int = 100) -> ApiResponse:
        referer = f"{self.base_url}/dashboard/transactions"
        return self._request(
            "GET",
            f"/api/transactions?limit={limit}",
            referer=referer,
            json_body=False,
        )

    def discover_card_ids(self) -> list[str]:
        page = self._request("GET", "/dashboard/cards", json_body=False)
        if not page.ok:
            return []

        candidates = UUID_RE.findall(page.text or "")
        seen: set[str] = set()
        card_ids: list[str] = []
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            details = self.get_card_details(candidate)
            if details.ok and isinstance(details.data, dict):
                card_ids.append(candidate)
        return card_ids

    def topup(self, card_id: str, amount_cents: int, idempotency_key: str | None = None) -> ApiResponse:
        key = idempotency_key or new_idempotency_key()
        referer = f"{self.base_url}/dashboard/cards/{card_id}?topup=true"
        return self._request(
            "POST",
            f"/api/cards/{card_id}/topup",
            referer=referer,
            json={"amount": amount_cents, "idempotencyKey": key},
        )

    @staticmethod
    def _walk_transactions(node: Any, found: list[dict[str, Any]]) -> None:
        if isinstance(node, list):
            if node and all(isinstance(item, dict) for item in node[: min(len(node), 3)]):
                sample = node[0]
                tx_keys = {"amount", "merchant", "description", "status", "createdAt", "created_at", "date"}
                if len(tx_keys.intersection(sample.keys())) >= 2:
                    found.extend(item for item in node if isinstance(item, dict))
                    return
            for item in node:
                SolvoCardClient._walk_transactions(item, found)
            return
        if isinstance(node, dict):
            for key in ("transactions", "activity", "ledger", "history", "items", "data"):
                if key in node:
                    SolvoCardClient._walk_transactions(node[key], found)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    SolvoCardClient._walk_transactions(value, found)

    @staticmethod
    def parse_card(details: ApiResponse) -> dict[str, Any]:
        if not details.ok or not isinstance(details.data, dict):
            return {
                "error": details.text[:240] or f"HTTP {details.status_code}",
                "status_code": details.status_code,
            }

        data = details.data
        card_obj = data.get("card") if isinstance(data.get("card"), dict) else data
        merged = {**data, **card_obj} if isinstance(card_obj, dict) else data

        balance_raw = _pick_first(
            merged,
            (
                "availableBalance",
                "available_balance",
                "balance",
                "currentBalance",
                "current_balance",
                "spendingBalance",
                "walletBalance",
            ),
        )
        balance_cents = _money_to_cents(balance_raw)

        parsed = {
            "id": _pick_first(merged, ("id", "cardId", "card_id")),
            "label": _pick_first(merged, ("nameOnCard", "name_on_card", "nickname", "cardName", "label", "name")),
            "last4": _pick_first(merged, ("lastFour", "last4", "last_four", "panLast4")),
            "status": _pick_first(merged, ("status", "cardStatus", "state")),
            "currency": _pick_first(merged, ("currency", "currencyCode")) or "USD",
            "balance": balance_raw,
            "balance_cents": balance_cents,
            "balance_display": f"${balance_cents / 100:.2f}" if balance_cents is not None else None,
            "type": _pick_first(merged, ("type", "cardType", "productType")),
        }

        transactions: list[dict[str, Any]] = []
        SolvoCardClient._walk_transactions(details.data, transactions)
        parsed["transactions"] = transactions[:100]
        parsed["transaction_count"] = len(transactions)
        return parsed

    @staticmethod
    def normalize_transactions(payload: Any, card_id: str | None = None, card_label: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        SolvoCardClient._walk_transactions(payload, rows)
        normalized: list[dict[str, Any]] = []
        for row in rows:
            amount = _pick_first(row, ("amount", "value", "total", "transactionAmount"))
            cents = _money_to_cents(amount)
            merchant_raw = _pick_first(row, ("merchant", "merchantName", "description", "name", "title"))
            merchant = _display_text(merchant_raw) or _display_text(row.get("description"))
            date_raw = _pick_first(
                row,
                ("paymentDateTime", "payment_date_time", "createdAt", "created_at", "date", "timestamp", "postedAt"),
            )
            normalized.append(
                {
                    "card_id": card_id or row.get("cardId") or row.get("card_id"),
                    "card_label": card_label,
                    "merchant": merchant,
                    "status": _pick_first(row, ("status", "state")),
                    "amount": amount,
                    "amount_cents": cents,
                    "amount_display": f"${cents / 100:.2f}" if cents is not None else str(amount or "—"),
                    "date": date_raw,
                    "date_display": _format_datetime(date_raw),
                    "type": _pick_first(row, ("type", "category", "transactionType")),
                    "raw": row,
                }
            )
        return normalized

    @staticmethod
    def summarize_card(details: ApiResponse) -> dict[str, Any]:
        parsed = SolvoCardClient.parse_card(details)
        if "error" in parsed:
            return parsed
        return {k: v for k, v in parsed.items() if k not in ("transactions", "raw")}


def is_topup_success(response: ApiResponse) -> bool:
    if response.status_code in (401, 403):
        return False
    if response.status_code == 409:
        return True
    if not (200 <= response.status_code < 300):
        return False
    data = response.data
    if not isinstance(data, dict):
        return True
    if data.get("success") is False:
        return False
    status = str(data.get("status") or "").lower()
    if status in {"failed", "error", "rejected", "declined"}:
        return False
    if data.get("error") and data.get("success") is not True:
        err = data.get("error")
        if isinstance(err, str) and err.strip():
            return False
        if isinstance(err, dict) and err:
            return False
    return True


TRANSIENT_TOPUP_STATUSES = frozenset({429, 502, 503, 504})
TRANSIENT_TOPUP_HINTS = (
    "try again",
    "too many",
    "unable to process",
    "temporarily",
    "rate limit",
    "busy",
    "overloaded",
)


def response_message(response: ApiResponse) -> str:
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


def is_transient_topup_failure(response: ApiResponse) -> bool:
    if response.status_code in TRANSIENT_TOPUP_STATUSES:
        return True
    if response.status_code >= 500:
        msg = response_message(response).lower()
        return any(hint in msg for hint in TRANSIENT_TOPUP_HINTS)
    return False
