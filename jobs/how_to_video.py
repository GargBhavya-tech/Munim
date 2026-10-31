"""The short how-to video for a merchant who keeps coming back with the same how-to question.

``jobs/recurrence.py`` decides WHY a merchant keeps coming back. When the cause is MERCHANT_NEEDS_HELP (a repeated how-to, not a fault), the fix is not another repeat of
the same answer but a short video in their language. This job turns each such open root-cause task into that video message:

  * the video comes from the approved list (``packs/paytm_merchant/videos.yaml``), chosen by the issue types on the task; with no match, the basics video;
  * the message is built by code from a fixed template, the approved title and the approved link: no model writes any of it, so nothing unsourced can be said;
  * it goes through the executor like any other contact, so consent, quiet hours, the contact budget and the do-not-disturb list all still apply;
  * a merchant with no consented channel is not messaged: the task says why, and stays open.

Every send is recorded (``video_sends``) and the task is marked actioned, so the same video is not sent twice for one task. Template wording for hi-IN, hinglish and kn-IN
needs a native speaker's review before real use (the same rule as every other non-English line).
"""
from __future__ import annotations

import sqlite3
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.actions.types import ActionIntent, Principal
from core.engine.clock import SimClock
from core.memory.store import write_txn

PACK_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant"
PRINCIPAL = Principal("AGENT", "munim")
PLAYBOOK = "VIDEO_TIP"
CAUSE = "MERCHANT_NEEDS_HELP"
CHANNEL_ORDER = ("whatsapp", "app", "sms")
LANGUAGES = ("en-IN", "hi-IN", "hinglish", "kn-IN")
SCHEMA = ("CREATE TABLE IF NOT EXISTS video_sends (task_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, video_id TEXT NOT NULL, language TEXT NOT NULL, "
          "channel TEXT NOT NULL, action_id TEXT, sent_at TEXT NOT NULL)")
SENT, NO_CHANNEL, BLOCKED, FAILED, ALREADY = "SENT", "NO_CONSENTED_CHANNEL", "BLOCKED", "SEND_FAILED", "ALREADY_SENT"


@dataclass(frozen=True)
class Video:
    video_id: str
    topics: tuple[str, ...]
    seconds: int
    titles: dict
    url: str


@dataclass
class VideoResult:
    task_id: str
    merchant_id: str
    status: str
    video_id: str | None = None
    channel: str | None = None
    body: str | None = None
    reasons: list[str] = field(default_factory=list)
    action_id: str | None = None


def load_catalog(path: Path | None = None) -> tuple[list[Video], dict]:
    doc = yaml.safe_load((path or PACK_DIR / "videos.yaml").read_text(encoding="utf-8"))
    videos = [Video(v["id"], tuple(v["topics"]), int(v["seconds"]), dict(v["title"]), v["url"]) for v in doc["videos"]]
    return videos, dict(doc["message"])


def pick_video(videos: list[Video], issue_types: list[str]) -> Video:
    """The video whose topics name one of the task's issue types (first match, in the catalog's order); otherwise the basics video."""
    wanted = {i.lower() for i in issue_types}
    for v in videos:
        if wanted & {t.lower() for t in v.topics}:
            return v
    return next(v for v in videos if v.video_id == "soundbox_basics")


def build_message(video: Video, language: str, templates: dict) -> str:
    lang = language if language in templates and language in video.titles else "en-IN"
    return templates[lang].format(title=video.titles[lang], url=video.url)


class HowToVideoJob:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, executor, *, catalog: Path | None = None):
        self.conn, self.clock, self.executor = conn, clock, executor
        self.videos, self.templates = load_catalog(catalog)
        conn.execute(SCHEMA)

    def _consented(self, merchant_id: str) -> list[str]:
        have = {r["channel"] for r in self.conn.execute("SELECT channel FROM consent WHERE subject_id = ? AND status = 'GRANTED'", (merchant_id,))}
        return [c for c in CHANNEL_ORDER if c in have]

    def tasks(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM root_cause_tasks WHERE cause = ? AND status = 'OPEN' ORDER BY task_id", (CAUSE,)).fetchall()

    def send_for(self, task: sqlite3.Row) -> VideoResult:
        import json
        mid = task["merchant_id"]
        if self.conn.execute("SELECT 1 FROM video_sends WHERE task_id = ?", (task["task_id"],)).fetchone():
            return VideoResult(task["task_id"], mid, ALREADY, reasons=["a video was already sent for this task"])
        m = self.conn.execute("SELECT language FROM merchants WHERE merchant_id = ?", (mid,)).fetchone()
        language = (m["language"] if m else None) or "en-IN"
        video = pick_video(self.videos, list(json.loads(task["issue_types_json"])))
        phone = self.conn.execute("SELECT phone FROM merchants WHERE merchant_id = ?", (mid,)).fetchone()
        if phone and phone["phone"] and self.conn.execute("SELECT 1 FROM dnd_registry WHERE phone = ?", (phone["phone"],)).fetchone():
            return VideoResult(task["task_id"], mid, BLOCKED, video.video_id, reasons=["DND: the number is in the do-not-disturb registry"])
        channels = self._consented(mid)
        if not channels:
            return VideoResult(task["task_id"], mid, NO_CHANNEL, video.video_id, reasons=["the merchant has not consented to any channel for messages"])
        channel, body = channels[0], build_message(video, language, self.templates)
        intent = ActionIntent("send_message", {"merchant_id": mid, "channel": channel, "body": body, "language": language if language in LANGUAGES else "en-IN"},
                              step_seq=zlib.crc32(task["task_id"].encode()) % 1_000_000, principal=PRINCIPAL, playbook_id=PLAYBOOK, step="send_video",
                              proactive=True, channel=channel, allowlist=("send_message",), facts={"task_id": task["task_id"], "video_id": video.video_id})
        out = self.executor.submit(intent)
        if out.status in ("REFUSED", "ESCALATE"):
            return VideoResult(task["task_id"], mid, BLOCKED, video.video_id, channel, body, [out.error or out.status])
        if out.status == "ERROR":
            return VideoResult(task["task_id"], mid, FAILED, video.video_id, channel, body, [out.error or "ERROR"], out.action_id)
        with write_txn(self.conn):
            self.conn.execute("INSERT OR IGNORE INTO video_sends VALUES (?,?,?,?,?,?,?)", (task["task_id"], mid, video.video_id, language, channel, out.action_id, self.clock.now_iso()))
            self.conn.execute("UPDATE root_cause_tasks SET status = 'ACTIONED' WHERE task_id = ?", (task["task_id"],))
        return VideoResult(task["task_id"], mid, SENT, video.video_id, channel, body, action_id=out.action_id)

    def run(self) -> list[VideoResult]:
        return [self.send_for(t) for t in self.tasks()]
