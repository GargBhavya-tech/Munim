"""LLM client (#34): one entry point for every model call, so every call is attributable.

* Two providers behind one interface: ``sarvam`` (model name from config/sarvam.yaml) and ``fallback`` (any OpenAI-compatible
  endpoint, configured in config/llm.yaml and keyed by FALLBACK_LLM_KEY).
* Each provider gets the first try plus ONE retry for timeouts, network errors, 408/429 and 5xx. Other 4xx errors do not retry.
  When the primary is exhausted the call moves to the fallback, and the response says so (``on_fallback``), so the UI can show a banner.
* Every call writes one CONTEXT_HASH ledger entry whose ``model_id`` column is the model that actually answered, whose ``prompt_hash``
  column is the hash of the request, and whose payload lists every provider attempt that failed first. A switch is never silent.
* The ledger gets hashes and counts, never prompt or reply text (redaction happens before a prompt is built; the ledger stays small).
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import httpx
import yaml

from core.audit.ledger import Ledger, canonical_json
from core.eval.latency import Stopwatch
from core.llm.sarvam_http import ChatResult, SarvamError, SarvamHTTP

LLM_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "llm.yaml"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class ChatProvider(Protocol):
    name: str          # 'sarvam' | 'fallback'
    model_id: str

    def chat(self, messages: list[dict], *, response_format: dict | None = None, **overrides) -> ChatResult: ...


class LLMUnavailable(Exception):
    """Every provider failed. ``attempts`` lists each failure so the caller can ledger and escalate honestly."""

    def __init__(self, attempts: list[dict]):
        super().__init__("no LLM provider answered: " + "; ".join(f"{a['provider']}: {a['error']}" for a in attempts))
        self.attempts = attempts


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model_id: str
    on_fallback: bool
    tokens_in: int
    tokens_out: int
    latency_ms: float
    prompt_hash: str
    attempts: tuple[dict, ...] = ()          # failed attempts before this answer
    ledger_seq: int | None = None
    raw: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------------------------------- providers
class SarvamProvider:
    name = "sarvam"

    def __init__(self, http: SarvamHTTP):
        self.http = http
        self.model_id = http.cfg["chat"]["model"]

    def chat(self, messages, *, response_format=None, **overrides) -> ChatResult:
        return self.http.chat(messages, response_format=response_format, **overrides)


class OpenAICompatProvider:
    """Any OpenAI-compatible /chat/completions endpoint, used as the fallback. Nothing Sarvam-specific (no reasoning_effort)."""
    name = "fallback"

    def __init__(self, base_url: str, model: str, api_key: str, *, path: str = "/chat/completions", temperature: float = 0.2,
                 max_tokens: int = 1024, timeout: float = 60.0, http: httpx.Client | None = None):
        self.model_id, self.api_key, self.path = model, api_key, path
        self.temperature, self.max_tokens, self.timeout = temperature, max_tokens, timeout
        self.http = http or httpx.Client(base_url=base_url)

    def chat(self, messages, *, response_format=None, **overrides) -> ChatResult:
        body = {"model": self.model_id, "messages": messages, "temperature": self.temperature, "max_tokens": self.max_tokens}
        if response_format:
            body["response_format"] = response_format
        body.update(overrides)
        r = self.http.post(self.path, json=body, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout)
        if r.status_code >= 400:
            raise SarvamError(r.status_code, r.text)          # same error type, so retry rules are shared
        data = r.json()
        return ChatResult(data["choices"][0]["message"]["content"] or "", data.get("usage") or {}, data)


def load_llm_config(path: Path | str = LLM_CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------------------------------- the client
def prompt_hash(messages: list[dict], response_format: dict | None = None) -> str:
    """Hash of the request. Deterministic: canonical JSON, no model name, no time."""
    return hashlib.sha256(canonical_json({"messages": messages, "response_format": response_format}).encode("utf-8")).hexdigest()


def _retryable(e: Exception) -> bool:
    if isinstance(e, SarvamError):
        return e.status in RETRYABLE_STATUS
    return isinstance(e, (httpx.TimeoutException, httpx.TransportError))


def _describe(e: Exception) -> str:
    return f"HTTP {e.status}" if isinstance(e, SarvamError) else f"{type(e).__name__}"


class LLMClient:
    def __init__(self, primary: ChatProvider, fallback: ChatProvider | None = None, ledger: Ledger | None = None, *,
                 retries: int = 1, sleep: Callable[[float], None] = time.sleep, backoff_s: float = 0.5, actor: str = "SYSTEM"):
        self.primary, self.fallback, self.ledger = primary, fallback, ledger
        self.retries, self.sleep, self.backoff_s, self.actor = retries, sleep, backoff_s, actor
        self.last_provider: str | None = None

    @property
    def on_fallback(self) -> bool:
        """True when the most recent answer came from the fallback: the UI shows a banner."""
        return self.last_provider == "fallback"

    def status(self) -> dict:
        return {"on_fallback": self.on_fallback, "last_provider": self.last_provider, "primary_model": self.primary.model_id,
                "fallback_model": self.fallback.model_id if self.fallback else None}

    def complete(self, messages: list[dict], *, case_id: str | None = None, purpose: str = "turn",
                 response_format: dict | None = None, **overrides) -> LLMResponse:
        phash = prompt_hash(messages, response_format)
        failed: list[dict] = []
        for provider in [p for p in (self.primary, self.fallback) if p is not None]:
            for attempt in range(self.retries + 1):
                sw = Stopwatch()
                try:
                    res = provider.chat(messages, response_format=response_format, **overrides)
                except Exception as e:     # noqa: BLE001 - any provider failure is recorded and routed
                    failed.append({"provider": provider.name, "model_id": provider.model_id, "attempt": attempt + 1,
                                   "error": _describe(e), "latency_ms": round(sw.elapsed_ms(), 1)})
                    if attempt < self.retries and _retryable(e):
                        self.sleep(self.backoff_s * (2 ** attempt))
                        continue
                    break
                return self._answer(provider, res, sw.elapsed_ms(), phash, tuple(failed), case_id, purpose)
        self._record_failure(phash, failed, case_id, purpose)
        raise LLMUnavailable(failed)

    # -- internals ------------------------------------------------------------------------------------------------
    def _answer(self, provider: ChatProvider, res: ChatResult, ms: float, phash: str, failed: tuple[dict, ...],
                case_id: str | None, purpose: str) -> LLMResponse:
        self.last_provider = provider.name
        t_in, t_out = int(res.usage.get("prompt_tokens") or 0), int(res.usage.get("completion_tokens") or 0)
        seq = None
        if self.ledger is not None:
            seq = self.ledger.append(kind="CONTEXT_HASH", actor=self.actor, case_id=case_id, model_id=provider.model_id, prompt_hash=phash, payload={
                "purpose": purpose, "ok": True, "provider": provider.name, "on_fallback": provider.name == "fallback",
                "tokens_in": t_in, "tokens_out": t_out, "latency_ms": round(ms, 1), "failed_attempts": list(failed)}).seq
        return LLMResponse(res.text, provider.name, provider.model_id, provider.name == "fallback", t_in, t_out, ms, phash, failed, seq, res.raw)

    def _record_failure(self, phash: str, failed: list[dict], case_id: str | None, purpose: str) -> None:
        self.last_provider = None
        if self.ledger is not None:
            self.ledger.append(kind="CONTEXT_HASH", actor=self.actor, case_id=case_id, prompt_hash=phash,
                               payload={"purpose": purpose, "ok": False, "failed_attempts": failed})


def build_client(ledger: Ledger | None = None, *, env: dict | None = None, sarvam_http: SarvamHTTP | None = None,
                 llm_config: dict | None = None, **kw) -> LLMClient:
    """Production wiring. The fallback exists only if config/llm.yaml names an endpoint AND FALLBACK_LLM_KEY is set."""
    env = os.environ if env is None else env
    primary = SarvamProvider(sarvam_http or SarvamHTTP(api_key=env.get("SARVAM_API_KEY", "")))
    cfg = (llm_config if llm_config is not None else load_llm_config()).get("fallback") or {}
    fb = None
    if cfg.get("base_url") and cfg.get("model") and env.get("FALLBACK_LLM_KEY"):
        fb = OpenAICompatProvider(cfg["base_url"], cfg["model"], env["FALLBACK_LLM_KEY"], path=cfg.get("path", "/chat/completions"),
                                  temperature=cfg.get("temperature", 0.2), max_tokens=cfg.get("max_tokens", 1024),
                                  timeout=cfg.get("timeout_seconds", 60.0))
    return LLMClient(primary, fb, ledger, **kw)
