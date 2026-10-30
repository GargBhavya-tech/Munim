"""Diagnose: the first unmet step of a playbook, from live system reads (Bible §15).

Every call re-reads every required read through the Reader. Nothing is cached between calls or between
steps: after an action the state is stale, and a cached read would close the case early.
"""
from __future__ import annotations

from core.actions.types import Reader
from core.engine.fields import build_namespace
from core.engine.playbook import Playbook, Step


def diagnose(playbook: Playbook, reader: Reader, merchant_id: str, case_facts: dict | None = None) -> Step | None:
    """First step (in the playbook's fixed order) whose done-expression is false, else None."""
    ns = build_namespace(reader, merchant_id, playbook.required_reads, case_facts)
    for step in playbook.steps:
        if not playbook.expr(step.done).evaluate(ns):
            return step
    return None


def done_check(playbook: Playbook, reader: Reader, merchant_id: str, case_facts: dict | None = None) -> bool:
    ns = build_namespace(reader, merchant_id, playbook.required_reads, case_facts)
    return bool(playbook.expr(playbook.done_check).evaluate(ns))


def escalation_reasons(playbook: Playbook, reader: Reader, merchant_id: str, case_facts: dict | None = None) -> list[str]:
    """The playbook's escalate_when expressions that are true right now (source text of each)."""
    ns = build_namespace(reader, merchant_id, playbook.required_reads, case_facts)
    return [src for src in playbook.escalate_when if playbook.expr(src).evaluate(ns)]


def action_permitted(playbook: Playbook, name: str, reader: Reader, merchant_id: str, case_facts: dict | None, prior_count: int) -> tuple[bool, str | None]:
    """Enforce the playbook's own limits on an allowed action: max_per_case and precondition.

    Parsing them is not enough: a playbook that says 'max_per_case: 1' must actually stop a second replacement.
    ``prior_count`` is how many times this case already ran the action. steps.<name>.done is derived from each step's
    done-expression (live reads), unless the caller supplied it as a case fact.
    """
    a = playbook.action(name)
    if a is None:
        return True, None  # not constrained here; the allowlist and the policy engine decide whether it may run at all
    if a.max_per_case is not None and prior_count >= a.max_per_case:
        return False, f"MAX_PER_CASE: {name} already ran {prior_count} time(s), limit {a.max_per_case}"
    if a.precondition:
        ns = build_namespace(reader, merchant_id, playbook.required_reads, case_facts)
        for step in playbook.steps:
            key = f"steps.{step.name}.done"
            if ns.get(key) is None:
                ns[key] = bool(playbook.expr(step.done).evaluate(ns)) if step.done != key else False
        if not playbook.expr(a.precondition).evaluate(ns):
            return False, f"PRECONDITION: {a.precondition}"
    return True, None
