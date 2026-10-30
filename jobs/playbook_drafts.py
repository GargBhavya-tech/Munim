"""Playbook drafts from gaps (Build Map #124; Bible: self-extending, human-gated).

Recurring escalations (handoffs.packet_json reasons, plus ESCALATION ledger entries that have no handoff) and UNSUPPORTED knowledge gaps are
clustered; every cluster seen at least ``min_count`` times becomes a DRAFT playbook proposal. A proposal is:
  * built conservatively: the only allowed action is send_message, it hands to a person after one reply, no side effect can be proposed;
  * validated by core.engine.playbook.load_playbook (it is written to a temp ``.yaml`` first) and written only if that passes;
  * saved to ``drafts/playbooks/<id>.yaml.draft`` OUTSIDE the pack. The ``.draft`` suffix means neither ``load_playbooks`` (``*.yaml``)
    nor the engine ever picks it up. A human enables it by reviewing it and moving it into the pack as ``<id>.yaml`` (renaming the id
    and removing the DRAFT header); nothing here does that. ``refuse_inside_pack`` stops a save target under ``packs/``.
Cluster keys are reason codes (before the colon) or the normalised gap question. Question text in a gap is already redacted.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from core.actions.registry import ActionRegistry
from core.engine.knowledge_layer import normalise_question
from core.engine.playbook import kb_scopes_from_dir, load_playbook

ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = ROOT / "drafts" / "playbooks"
PACK_DIR = ROOT / "packs"
SUFFIX = ".yaml.draft"
HEADER = ("# DRAFT: proposed from recurring gaps. NOT LIVE. The engine cannot load this file (suffix {suffix}).\n"
          "# A human reviews it, renames the id, removes this header and moves it into the pack as <id>.yaml. SIMULATED data only.\n")
# escalation reasons that are not gaps in coverage (a person was always meant to see them)
NOT_GAPS = {"AREA_INCIDENT", "WANTS_HUMAN"}


@dataclass(frozen=True)
class Cluster:
    key: str
    kind: str                 # "ESCALATION" | "UNSUPPORTED"
    count: int
    sample: str


def _escalation_reasons(conn: sqlite3.Connection) -> list[str]:
    out, with_handoff = [], set()
    for r in conn.execute("SELECT packet_json FROM handoffs"):
        for reason in json.loads(r[0]).get("reasons", []):
            out.append(str(reason))
    for r in conn.execute("SELECT payload_json FROM ledger WHERE kind = 'ESCALATION'"):
        p = json.loads(r[0])
        if p.get("handoff_id"):
            with_handoff.add(p["handoff_id"])
        else:
            out += [str(x) for x in p.get("reasons", [])]
    return out


def find_clusters(conn: sqlite3.Connection, min_count: int = 3) -> list[Cluster]:
    counts: Counter = Counter()
    sample: dict = {}
    for reason in _escalation_reasons(conn):
        code = reason.split(":", 1)[0].strip()
        if code and code not in NOT_GAPS:
            counts[("ESCALATION", code)] += 1
            sample.setdefault(("ESCALATION", code), reason)
    try:
        rows = conn.execute("SELECT question_redacted FROM knowledge_gaps WHERE kind = 'UNSUPPORTED' AND status != 'DISMISSED'").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for (q,) in rows:
        k = normalise_question(q)
        if k:
            counts[("UNSUPPORTED", k)] += 1
            sample.setdefault(("UNSUPPORTED", k), q)
    return [Cluster(k, kind, n, sample[(kind, k)]) for (kind, k), n in sorted(counts.items(), key=lambda t: (-t[1], t[0])) if n >= min_count]


def draft_id(cluster: Cluster) -> str:
    return "DRAFT_" + hashlib.sha256(f"{cluster.kind}|{cluster.key}".encode("utf-8")).hexdigest()[:8].upper()


def build_yaml(cluster: Cluster, kb_scope: str = "safety_lines") -> str:
    pid = draft_id(cluster)
    return (HEADER.format(suffix=SUFFIX)
            + f"# Evidence: {cluster.kind} cluster {json.dumps(cluster.key, ensure_ascii=False)} seen {cluster.count} times.\n"
            + f"id: {pid}\nhalf: service\npriority_lane: P2\n"
              "triggers:\n  events: []\n  intents: []\nrequired_reads: []\n"
              "steps:\n  - {name: reply, done: \"touches >= 1\"}\n"
              "allowed_actions:\n  - send_message\n"
              "done_check: \"touches >= 1\"\n"
              "escalate_when:\n  - \"intent in ['WANTS_HUMAN']\"\n"
              f"kb_scope: [{kb_scope}]\nmax_touches: 2\nsla_sim_hours: 24\n")


def refuse_inside_pack(path: Path) -> None:
    p = path.resolve()
    if PACK_DIR.resolve() in p.parents or p.suffix == ".yaml":
        raise ValueError(f"a draft must not be saved as a loadable file or inside packs/: {path}")


def validate(yaml_text: str, registry: ActionRegistry, kb_scopes: set[str]) -> None:
    """Raise a PlaybookError when the draft would not load. Uses the real loader on a temp .yaml copy."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "draft.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        load_playbook(p, registry, kb_scopes)


def write_drafts(conn: sqlite3.Connection, registry: ActionRegistry, *, drafts_dir: Path = DRAFTS_DIR, min_count: int = 3,
                 kb_scopes: set[str] | None = None) -> list[Path]:
    kb_scopes = kb_scopes if kb_scopes is not None else kb_scopes_from_dir(ROOT / "packs" / "paytm_merchant" / "kb")
    written = []
    for c in find_clusters(conn, min_count):
        text = build_yaml(c)
        validate(text, registry, kb_scopes)
        target = Path(drafts_dir) / (draft_id(c) + SUFFIX)
        refuse_inside_pack(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written
