"""The labelled sets of the Gate 4 held-out run (#146), replayed against the real engines. NOT part of the normal suite (the name does not match test_*.py).

Run only by core/eval/heldout.py, which sets MUNIM_LABELLED_OUT to a JSON path; each test writes one section of raw per-item outcomes there. Metrics are
computed in core/eval/heldout.py from these outcomes, never here, so what this file records is decisions, not numbers:
  * policy probes (eval/frozen/probes/policy_probes.json): the hand label and the policy engine's outcome for every probe;
  * escalation snapshots (escalation_snapshots.json): the hand label and whether the hard triggers / soft tiers escalated;
  * the claim set (eval/frozen/claims), ENGLISH items only: whether the real number check + entailment model would let each claim be sent.
    Hindi, Kannada and Hinglish items are judged by the back-translation, which needs the live translator, so they are not run here.
Which sources a claim is tried against (it counts as SENT if any one of them lets it through):
  * a true KB claim: the KB section its label was derived from (what a correct model cites);
  * a true or false claim about the merchant's data: every read tool result for that merchant, as the turn would have them (the label's SQL only
    names the merchant; the gate never sees a bare column);
  * a false KB claim and an unsupported claim: every KB section of the pack (the worst case: a model citing whatever helps).
"""
import json
import os
import re
from pathlib import Path

import pytest

from core.actions.registry import FORBIDDEN_ACTIONS, RiskClass
from core.gate.sources import Claim, SourceBook
from core.gate.verify import GroundingVerifier
from tests.test_claim_set import kb, load_items  # noqa: F401  (kb is a module fixture)
from tests.test_probe_set import PROBES, SNAPSHOTS, hard_codes, replay, soft_decision  # noqa: F401  (replay is a fixture)

OUT = os.environ.get("MUNIM_LABELLED_OUT")
pytestmark = pytest.mark.skipif(not OUT, reason="run by core/eval/heldout.py only")
_MID = re.compile(r"merchant_id\s*=\s*'(M-\d+)'")
_REF = re.compile(r"reference\s*=\s*'([^']+)'")


def _write(section: str, rows: list[dict]) -> None:
    p = Path(OUT)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data[section] = rows
    p.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def test_policy_probes(replay):
    got = replay(PROBES)
    _write("policy_probes", [{"id": p["id"], "action": p["intent"]["action"], "forbidden": p["intent"]["action"] in FORBIDDEN_ACTIONS,
                              "expected": p["expect"]["outcome"], "engine": got[p["id"]][0]} for p in PROBES])


def test_escalation_snapshots():
    rows = []
    for s in SNAPSHOTS:
        escalated = bool(hard_codes(s)) if s["kind"] == "hard" else soft_decision(s) == "ESCALATE"
        rows.append({"id": s["id"], "kind": s["kind"], "should_escalate": bool(s["expect"]["should_escalate"]), "escalated": escalated})
    _write("escalation_snapshots", rows)


def _kb_id(doc: str, section: str) -> str:
    return f"kb:{doc}#{section}@v1"


def _merchant_reads(sql: str, http, registry) -> list[tuple[str, object]]:
    """Every R0 read for the merchant the label's SQL names, through the mocks, as tool sources."""
    mid = _MID.search(sql).group(1)
    ref = _REF.search(sql)
    out = []
    for spec in registry.specs():
        if spec.cls is not RiskClass.R0 or spec.method != "GET":
            continue
        args = {"merchant_id": mid, "limit": 20, "reference": ref.group(1) if ref else ""}
        if "{reference}" in spec.path and not ref:
            continue
        r = http.get(spec.path.format(**args))
        if r.status_code == 200:
            out.append((f"tool:{spec.name}#1", r.json()))
    return out


def test_claims_english(http, kb):  # noqa: F811
    from core.gate.nli import EntailmentModel
    from packs.paytm_merchant.actions import build_registry
    verifier = GroundingVerifier(EntailmentModel())
    registry = build_registry()
    every_kb = [(_kb_id(doc, sec), text) for doc, secs in sorted(kb.items()) for sec, text in sorted(secs.items())]
    rows = []
    for item in [i for i in load_items() if i["lang"] == "en"]:
        tr = item["truth"]
        if tr["kind"] == "db":
            options = _merchant_reads(tr["sql"], http, registry)
        elif tr["kind"] == "kb" and item["label"] == "true":
            options = [(_kb_id(tr["doc"], tr["section"]), kb[tr["doc"]][tr["section"]])]
        else:
            options = every_kb
        sent = False
        for sid, body in options:
            book = SourceBook()
            (book.add_tool_result if sid.startswith("tool:") else book.add_kb)(sid, body)
            if verifier.verify([Claim("c1", item["text"], sid)], book)[0].sendable:
                sent = True
                break
        rows.append({"id": item["id"], "label": item["label"], "kind": tr["kind"], "sent": sent, "sources_tried": len(options)})
    _write("claims_en", rows)
