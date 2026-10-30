"""Action registry: the ONLY way to reach a mock endpoint (enforced by a grep test once the executor lands).

Forbidden actions are not registered "for internal use"; they do not exist. Looking one up raises.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable, Iterable

from pydantic import BaseModel


class RiskClass(str, enum.Enum):
    R0 = "R0"          # read, no side effect
    DRAFT = "DRAFT"    # prepares something, no effect, discardable
    R1 = "R1"          # reversible exactly
    R2 = "R2"          # compensable within a window
    R3 = "R3"          # irreversible


FORBIDDEN_ACTIONS = frozenset({
    "approve_kyc", "change_bank_details", "change_settlement_account", "credit_decision", "delete_record",
    "accept_documents_in_chat", "request_otp",
    # firmware is staged by ops only (#93); the agent must never have it
    "update_firmware",
})


class UnknownAction(KeyError):
    pass


class ForbiddenAction(Exception):
    pass


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ActionSpec:
    name: str
    cls: RiskClass
    args_model: type[BaseModel]
    system: str | None = None            # mock system whose chaos/breaker applies; None = internal (no HTTP)
    method: str | None = None            # GET | POST | DELETE
    path: str | None = None              # str.format template over the validated args, query string included
    body_fields: tuple[str, ...] | None = None   # args sent as the JSON body (None = no body)
    compensation: str | None = None      # name of the registered action that undoes this one
    window_hours: float | None = None    # None on an R2 means "while the object is still open"
    comp_args: Callable[[dict, dict], dict] | None = None  # (args, result_body) -> args of the compensation
    offer: bool = False                  # a P3 sales action: removed from every allowlist by fix-first
    contact: bool = False                # delivers something to the merchant: consent, quiet hours, contact budget
    agent_callable: bool = True          # False for compensations and human-only actions
    description: str = ""


class ActionRegistry:
    def __init__(self, specs: Iterable[ActionSpec]):
        self._specs: dict[str, ActionSpec] = {}
        for s in specs:
            if s.name in FORBIDDEN_ACTIONS:
                raise ForbiddenAction(f"{s.name!r} is forbidden and cannot be registered")
            if s.name in self._specs:
                raise RegistryError(f"duplicate action {s.name!r}")
            self._specs[s.name] = s
        self._validate()

    def _validate(self) -> None:
        for s in self._specs.values():
            if s.cls in (RiskClass.R1, RiskClass.R2) and s.agent_callable:
                if not s.compensation or s.compensation not in self._specs:
                    raise RegistryError(f"{s.name} is {s.cls.value} but has no registered compensation")
                if s.comp_args is None:
                    raise RegistryError(f"{s.name} needs comp_args to build its compensation")
            if s.cls is RiskClass.R0 and s.method not in (None, "GET"):
                raise RegistryError(f"{s.name} is a read but uses {s.method}")
            if s.system is not None and (s.method is None or s.path is None):
                raise RegistryError(f"{s.name} names system {s.system!r} without a method and path")
            if s.cls is RiskClass.R3 and s.compensation:
                raise RegistryError(f"{s.name} is irreversible and cannot have a compensation")

    # -- lookups -------------------------------------------------------------------
    def get(self, name: str) -> ActionSpec:
        if name in FORBIDDEN_ACTIONS:
            raise ForbiddenAction(name)
        try:
            return self._specs[name]
        except KeyError:
            raise UnknownAction(name) from None

    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self, *, agent_only: bool = False) -> list[str]:
        return sorted(n for n, s in self._specs.items() if s.agent_callable or not agent_only)

    def specs(self) -> list[ActionSpec]:
        return [self._specs[n] for n in sorted(self._specs)]

    def offer_actions(self) -> frozenset[str]:
        return frozenset(n for n, s in self._specs.items() if s.offer)

    def contact_actions(self) -> frozenset[str]:
        return frozenset(n for n, s in self._specs.items() if s.contact)

    def writes(self) -> list[ActionSpec]:
        return [s for s in self.specs() if s.cls in (RiskClass.R1, RiskClass.R2, RiskClass.R3)]
