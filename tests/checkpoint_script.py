"""The scripted (no-LLM) decisions for S1, shared by the checkpoint and scenario tests. Arguments name the CASE's merchant, so any
seeded persona can run S1 (the old table hardcoded M-0006)."""
MID = "M-0006"


def _mid(**extra):
    return lambda ctx: {"merchant_id": ctx.case.merchant_id, **extra}


SCRIPT = {
    ("S1_ACTIVATION", "kyc"): {"action": "send_kyc_resume_link", "args": _mid(), "channel": "app"},
    ("S1_ACTIVATION", "bank"): ("retrigger_bank_verification", _mid()),
    ("S1_ACTIVATION", "device"): ("create_device_replacement", _mid(reason="NO_ANNOUNCEMENT")),
}
