"""Model allowlist, bounded transient-error backoff and call caps for Luna roles.

Every model call made by ``spotify-pipeline run`` goes through
``GuardedProvider``. It enforces three controls in code, before and after the
underlying provider runs:

* **Allowlist.** Only the declared model/effort pairs may be requested
  (``gpt-6-luna`` with ``none`` or ``low`` by default). Anything else raises
  ``ModelNotAllowedError`` before a request is sent. If the provider's trace
  reports a served model (Codex CLI 0.159.3 does not), it must equal the
  requested model, otherwise the response is rejected and the run stops.
* **Bounded backoff.** ``ProviderUnavailableError`` (non-zero CLI exit, for
  example a rate limit or network failure) is retried at most
  ``max_transient_retries`` times with exponential backoff and jitter. Each
  failed attempt is reported to ``on_failed_attempt`` so it is logged as a
  call. Timeouts are *not* retried, because a timed-out request can still be
  charged; they are surfaced for reconciliation instead.
* **Call caps.** ``max_calls_by_role`` stops admitting new requests for a role
  once its cap is reached, so a run cannot exceed its declared request budget.
* **One spend ledger.** Before each request the guard reserves a worst-case
  API-equivalent amount; it refuses the request when spent + reserved would
  exceed ``spend_limit_usd``. After the call the reservation is settled at
  usage × rate, or kept at the reservation when usage is missing (unknown
  charge). Subscription calls are priced at the editable API-equivalent rates
  so the cap is meaningful even though no invoice is produced.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

from .providers import Provider, ProviderResult, ProviderUnavailableError


class ModelNotAllowedError(RuntimeError):
    """Raised when a call requests, or a trace reports, a non-allowlisted model."""


class CallCapReachedError(RuntimeError):
    """Raised when a role has used its declared maximum number of requests."""


class SpendCapReachedError(RuntimeError):
    """Raised when the next reservation would exceed the run's spending limit."""


# Worst-case reservation per request: 60,000 input tokens (no cache credit) and
# 8,000 output tokens at the configured rates. Observed Codex/Luna calls used
# about 12,000-26,000 input and 400-3,400 output tokens.
RESERVATION_INPUT_TOKENS = 60_000
RESERVATION_OUTPUT_TOKENS = 8_000


DEFAULT_ALLOWLIST: dict[str, tuple[str, ...]] = {"gpt-6-luna": ("none", "low")}


class GuardedProvider:
    def __init__(
        self,
        provider: Provider,
        *,
        allowlist: dict[str, tuple[str, ...]] | None = None,
        max_calls_by_role: dict[str, int] | None = None,
        max_transient_retries: int = 2,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 30.0,
        on_failed_attempt: Callable[[dict[str, Any]], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        spend_limit_usd: float | None = None,
        rates_per_token: dict[str, float] | None = None,
    ) -> None:
        if max_transient_retries < 0 or max_transient_retries > 5:
            raise ValueError("max_transient_retries must be between 0 and 5")
        self.provider = provider
        self.name = provider.name
        self.allowlist = {
            model: tuple(efforts)
            for model, efforts in (allowlist or DEFAULT_ALLOWLIST).items()
        }
        self.max_calls_by_role = dict(max_calls_by_role or {})
        self.max_transient_retries = max_transient_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.on_failed_attempt = on_failed_attempt
        self._sleep = sleep
        self._rng = rng or random.Random(20261012)
        self.calls_by_role: dict[str, int] = {}
        self.transient_failures: list[dict[str, Any]] = []
        self.spend_limit_usd = spend_limit_usd
        self.rates = dict(rates_per_token or {
            "input_tokens": 0.10 / 1_000_000,
            "cached_input_tokens": 0.01 / 1_000_000,
            "output_tokens": 0.50 / 1_000_000,
        })
        self.spent_usd = 0.0
        self.unknown_charge_requests = 0
        self.in_flight = 0
        self._lock = threading.Lock()

    def reservation_usd(self) -> float:
        return (
            RESERVATION_INPUT_TOKENS * self.rates["input_tokens"]
            + RESERVATION_OUTPUT_TOKENS * self.rates["output_tokens"]
        )

    def _settle(self, result: ProviderResult | None) -> float:
        with self._lock:
            self.in_flight -= 1
            return self._settle_locked(result)

    def _settle_locked(self, result: ProviderResult | None) -> float:
        reservation = self.reservation_usd()
        if result is None or result.input_tokens is None or result.output_tokens is None:
            self.unknown_charge_requests += 1
            self.spent_usd += reservation
            return reservation
        cached = min(result.cached_input_tokens or 0, result.input_tokens)
        amount = (
            (result.input_tokens - cached) * self.rates["input_tokens"]
            + cached * self.rates["cached_input_tokens"]
            + result.output_tokens * self.rates["output_tokens"]
        )
        self.spent_usd += amount
        return amount

    def check_model(self, model: str, effort: str) -> None:
        allowed = self.allowlist.get(model)
        if allowed is None or effort not in allowed:
            raise ModelNotAllowedError(
                f"Model/effort {model!r}/{effort!r} is not in the allowlist {self.allowlist}"
            )

    def call(self, *, role: str, model: str, effort: str, **kwargs: Any) -> ProviderResult:
        self.check_model(model, effort)
        attempt = 0
        while True:
            with self._lock:
                cap = self.max_calls_by_role.get(role)
                used = self.calls_by_role.get(role, 0)
                if cap is not None and used >= cap:
                    raise CallCapReachedError(
                        f"Role {role!r} reached its declared cap of {cap} requests"
                    )
                # Reserve worst case for every request already in flight plus this one.
                committed = self.spent_usd + (self.in_flight + 1) * self.reservation_usd()
                if self.spend_limit_usd is not None and committed > self.spend_limit_usd:
                    raise SpendCapReachedError(
                        f"Spent {self.spent_usd:.6f} USD (API-equivalent) + {self.in_flight + 1} "
                        f"reservations of {self.reservation_usd():.6f} would exceed the "
                        f"{self.spend_limit_usd} cap"
                    )
                self.calls_by_role[role] = used + 1
                self.in_flight += 1
            try:
                result = self.provider.call(role=role, model=model, effort=effort, **kwargs)
            except ProviderUnavailableError as error:
                self._settle(None)
                record = {
                    "role": role,
                    "model": model,
                    "effort": effort,
                    "request_id": error.request_id,
                    "duration_seconds": error.duration_seconds,
                    "error": f"{type(error).__name__}: {error}"[:1000],
                    "transient_retry_index": attempt,
                    "will_retry": attempt < self.max_transient_retries,
                }
                with self._lock:
                    self.transient_failures.append(record)
                if attempt >= self.max_transient_retries:
                    # The caller logs the final failed attempt itself.
                    raise
                if self.on_failed_attempt is not None:
                    self.on_failed_attempt(record)
                delay = min(
                    self.backoff_max_seconds,
                    self.backoff_base_seconds * (2 ** attempt),
                ) * (0.5 + self._rng.random() / 2)
                self._sleep(delay)
                attempt += 1
                continue
            except Exception:
                # Timeouts and other failures: keep the full reservation as an
                # unknown charge and surface the error (no automatic retry).
                self._settle(None)
                raise
            self._settle(result)
            self._check_served_model(result, model)
            metadata = dict(result.provider_metadata or {})
            metadata["guard"] = {
                "allowlist_checked": True,
                "requested_model": model,
                "requested_effort": effort,
                "served_model_reported": _served_model(result),
                "transient_retries_before_success": attempt,
            }
            result.provider_metadata = metadata
            return result

    def _check_served_model(self, result: ProviderResult, requested: str) -> None:
        served = _served_model(result)
        if served is not None and served != requested:
            raise ModelNotAllowedError(
                f"Provider trace reports model {served!r}, but {requested!r} was requested; "
                "the response was rejected"
            )

    def runtime_metadata(self) -> dict[str, Any]:
        inner = getattr(self.provider, "runtime_metadata", None)
        return {
            "guard": {
                "allowlist": {key: list(value) for key, value in self.allowlist.items()},
                "max_calls_by_role": self.max_calls_by_role,
                "calls_by_role": self.calls_by_role,
                "max_transient_retries": self.max_transient_retries,
                "transient_failures": self.transient_failures,
                "spend_limit_usd_api_equivalent": self.spend_limit_usd,
                "spent_usd_api_equivalent": round(self.spent_usd, 8),
                "unknown_charge_requests_reserved_at_cap": self.unknown_charge_requests,
                "reservation_usd_per_request": round(self.reservation_usd(), 8),
            },
            "provider": inner() if callable(inner) else {"provider": self.name},
        }


def _served_model(result: ProviderResult) -> str | None:
    """Return a model name reported by the provider trace, if any.

    Codex CLI JSONL events do not currently include the served model, so this is
    usually ``None``; the requested model is then pinned by ``--model`` and
    ``--ignore-user-config`` and recorded with every call.
    """
    metadata = result.provider_metadata or {}
    for key in ("served_model", "response_model", "model"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    summary = metadata.get("event_summary") or {}
    value = summary.get("served_model") if isinstance(summary, dict) else None
    return value if isinstance(value, str) and value else None
