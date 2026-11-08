"""
Shared helpers for the pkmnprices.com daily tracker.

Handles:
- API auth (x-api-key)
- Credit-budget tracking against the plan's daily credit limit (read live
  from the x-credits-remaining / x-credits-limit response headers, since the
  JWT-only /v1/usage endpoint isn't reachable with an API key). Note:
  x-credits-charged is the cost of that one request, not a cumulative daily
  total, despite its name — don't use it for budget tracking.
- Rate-limit-aware retries (429 credit_limit_exceeded vs 429 rate_limit_exceeded)
"""

import os
import time
import json
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional; env vars can also be set directly (e.g. in CI)

BASE_URL = "https://api.pkmnprices.com/v1"
API_KEY = os.environ.get("PKMNPRICES_API_KEY")

# Free plan = 500 credits/day. Leave a safety margin for the run itself
# (headers, retries, and the odd list call) rather than spending to the wire.
DAILY_CREDIT_LIMIT = int(os.environ.get("PKMNPRICES_DAILY_LIMIT", "500"))
CREDIT_SAFETY_BUFFER = int(os.environ.get("PKMNPRICES_SAFETY_BUFFER", "10"))

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)


class CreditBudgetExhausted(Exception):
    pass


class ApiClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or API_KEY
        if not self.api_key:
            sys.exit(
                "ERROR: PKMNPRICES_API_KEY is not set. "
                "Copy .env.example to .env and add your key, "
                "or set it as a GitHub Actions secret."
            )
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": self.api_key})
        self.credits_charged_today = 0
        self.credits_limit = DAILY_CREDIT_LIMIT
        self._allocation: int | None = None
        self._allocation_baseline = 0

    def set_allocation(self, credits: int):
        """Caps how many credits this client may spend from now on, on top
        of (not instead of) the real account-wide daily limit. For an
        orchestrator (daily_sync.py) splitting one day's credit pool across
        several tasks that share a client — call again with a new figure to
        switch the cap to the next task."""
        self._allocation = max(0, credits)
        self._allocation_baseline = self.credits_charged_today

    def allocation_spent(self) -> int:
        return max(0, self.credits_charged_today - self._allocation_baseline)

    def allocation_remaining(self) -> int | None:
        if self._allocation is None:
            return None
        return max(0, self._allocation - self.allocation_spent())

    def _update_budget_from_headers(self, headers):
        # x-credits-remaining is the server's own cumulative daily counter;
        # x-credits-charged is only the cost of this one request (confirmed
        # empirically — it does not accumulate across calls), so it's not
        # usable for budget tracking despite the name.
        limit = headers.get("x-credits-limit")
        remaining = headers.get("x-credits-remaining")
        if limit is not None:
            try:
                self.credits_limit = int(limit)
            except ValueError:
                pass
        if remaining is not None:
            try:
                self.credits_charged_today = self.credits_limit - int(remaining)
            except ValueError:
                pass

    def remaining_credits(self) -> int:
        real = max(0, self.credits_limit - self.credits_charged_today)
        allocated = self.allocation_remaining()
        return real if allocated is None else min(real, allocated)

    def get(self, path: str, params: dict | None = None, max_retries: int = 5):
        """
        GET request with:
        - a pre-flight budget check (refuses to spend past the safety buffer,
          or past this client's task allocation if one is set)
        - exponential backoff on 429 rate_limit_exceeded
        - a hard stop on 429 credit_limit_exceeded (no point retrying)
        """
        if self.remaining_credits() <= CREDIT_SAFETY_BUFFER:
            allocated = self.allocation_remaining()
            if allocated is not None and allocated <= CREDIT_SAFETY_BUFFER:
                raise CreditBudgetExhausted(
                    f"Task allocation reached ({self.allocation_spent()}/{self._allocation} credits spent)."
                )
            raise CreditBudgetExhausted(
                f"Only {self.remaining_credits()} credits left today "
                f"(buffer is {CREDIT_SAFETY_BUFFER}). Stopping before the next call."
            )

        url = f"{BASE_URL}{path}"
        backoff = 2
        for attempt in range(1, max_retries + 1):
            resp = self.session.get(url, params=params, timeout=30)
            self._update_budget_from_headers(resp.headers)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                body = {}
                try:
                    body = resp.json()
                except ValueError:
                    pass
                code = body.get("error", {}).get("code")
                if code == "credit_limit_exceeded":
                    raise CreditBudgetExhausted(
                        "Server reports daily credit limit reached."
                    )
                # rate_limit_exceeded: back off and retry
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            if resp.status_code in (401, 403, 404, 400):
                # Not retryable — surface it to the caller with context.
                try:
                    detail = resp.json()
                except ValueError:
                    detail = resp.text
                raise RuntimeError(f"{resp.status_code} on GET {path}: {detail}")

            # 5xx or unexpected — retry with backoff
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

        raise RuntimeError(f"Exhausted retries on GET {path}")


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
