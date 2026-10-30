"""A scripted stand-in for the turn agent, so a case can run end to end with no LLM (checkpoint, tests)."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from core.engine.workflow import Decision, DecisionContext


def scripted_decider(table: Mapping[tuple[str, str], Any], default: Decision | None = None) -> Callable[[DecisionContext], Decision]:
    """``table[(playbook_id, step_name)]`` is one of:
      * ``("action_name", {args})``                       -> that action, no message
      * a ``Decision``
      * a dict ``{"action": str, "args": dict|callable(ctx)->dict, "message_en": str, "follow_up_hours": float, "channel": str}``
      * a callable ``ctx -> Decision``
    A step missing from the table yields "no action" (the case waits)."""
    def decide(ctx: DecisionContext) -> Decision:
        key = (ctx.case.playbook_id, ctx.step.name)
        entry = table.get(key)
        if entry is None:
            return default or Decision()
        if callable(entry):
            return entry(ctx)
        if isinstance(entry, Decision):
            return entry
        if isinstance(entry, tuple):
            name, args = entry
            return Decision(action=(name, dict(args(ctx) if callable(args) else args)))
        if isinstance(entry, dict):
            args = entry.get("args", {})
            action = (entry["action"], dict(args(ctx) if callable(args) else args)) if entry.get("action") else None
            return Decision(action=action, message_en=entry.get("message_en"),
                            follow_up_hours=entry.get("follow_up_hours"), channel=entry.get("channel"))
        raise TypeError(f"unsupported script entry for {key}: {type(entry)}")
    return decide
