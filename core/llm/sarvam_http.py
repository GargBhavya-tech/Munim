"""Thin, dependency-light wrapper over Sarvam's REST API. Model names come from config/sarvam.yaml.

This is plumbing only: the provider-switching, logging and ledger attribution live in the LLM client (#34).
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "sarvam.yaml"


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


class SarvamError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


@dataclass
class ChatResult:
    text: str
    usage: dict
    raw: dict = field(repr=False, default_factory=dict)


class SarvamHTTP:
    def __init__(self, api_key: str | None = None, config: dict | None = None, http: httpx.Client | None = None):
        self.cfg = config or load_config()
        self.api_key = api_key or os.environ.get("SARVAM_API_KEY", "")
        self.http = http or httpx.Client(base_url=self.cfg["base_url"])

    @property
    def headers(self) -> dict:
        return {"api-subscription-key": self.api_key}

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise SarvamError(r.status_code, r.text)
        return r

    def chat(self, messages: list[dict], *, response_format: dict | None = None, **overrides) -> ChatResult:
        c = self.cfg["chat"]
        body = {"model": c["model"], "messages": messages, "temperature": c["temperature"], "max_tokens": c["max_tokens"]}
        effort = c.get("reasoning_effort")
        if effort == "off":
            body["reasoning_effort"] = None      # explicit JSON null disables reasoning; omitting the field leaves it ON
        elif effort:
            body["reasoning_effort"] = effort
        if response_format:
            body["response_format"] = response_format
        body.update(overrides)
        r = self._check(self.http.post(c["path"], json=body, headers=self.headers, timeout=self.cfg["timeouts_seconds"]["chat"]))
        data = r.json()
        return ChatResult(data["choices"][0]["message"]["content"] or "", data.get("usage") or {}, data)

    def stt(self, wav_bytes: bytes, language_code: str = "unknown", mode: str | None = None) -> dict:
        c = self.cfg["stt"]
        r = self._check(self.http.post(
            c["path"], headers=self.headers, timeout=self.cfg["timeouts_seconds"]["stt"],
            files={"file": ("clip.wav", wav_bytes, "audio/wav")}, data={"model": c["model"], "mode": mode or c["mode"], "language_code": language_code},
        ))
        return r.json()

    def tts(self, text: str, language_code: str) -> bytes:
        c = self.cfg["tts"]
        body = {"text": text, "language_code": language_code, "model": c["model"], "speaker": c["speakers"].get(language_code, "shubh"),
                "speech_sample_rate": c["sample_rate"]}
        r = self._check(self.http.post(c["path"], json=body, headers=self.headers, timeout=self.cfg["timeouts_seconds"]["tts"]))
        return base64.b64decode(r.json()["audios"][0])

    def translate(self, text: str, source: str, target: str) -> dict:
        c = self.cfg["translate"]
        body = {"input": text, "source_language_code": source, "target_language_code": target, "model": c["model"], "mode": c["mode"]}
        r = self._check(self.http.post(c["path"], json=body, headers=self.headers, timeout=self.cfg["timeouts_seconds"]["translate"]))
        return r.json()


def parse_json_loose(text: str):
    """Parse a model reply that should be JSON. Raises ValueError if it is not."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.index("\n") + 1:] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    return json.loads(s)
