"""
Shared helpers for the pkmnprices.com daily tracker.

Handles:
- API auth (x-api-key)
- Credit-budget tracking against the Free plan's 500 credits/day
  (read live from the x-credits-charged / x-credits-limit response headers,
  since the JWT-only /v1/usage endpoint isn't reachable with an API key)
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

    def _update_budget_from_headers(self, headers):
        # These headers reflect the server's own daily counter, so trust them
        # over any local estimate once they show up.
        charged = headers.get("x-credits-charged")
        limit = headers.get("x-credits-limit")
        if charged is not None:
            try:
                self.credits_charged_today = int(charged)
            except ValueError:
                pass
        if limit is not None:
            try:
                self.credits_limit = int(limit)
            except ValueError:
                pass

    def remaining_credits(self) -> int:
        return max(0, self.credits_limit - self.credits_charged_today)

    def get(self, path: str, params: dict | None = None, max_retries: int = 5):
        """
        GET request with:
        - a pre-flight budget check (refuses to spend past the safety buffer)
        - exponential backoff on 429 rate_limit_exceeded
        - a hard stop on 429 credit_limit_exceeded (no point retrying)
        """
        if self.remaining_credits() <= CREDIT_SAFETY_BUFFER:
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
