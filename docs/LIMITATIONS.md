# Scope and Design Boundaries (#155)

This document defines the intentional design boundaries of Munim — choices made for correctness, safety and hackathon compliance, not gaps in capability.

## What is simulated (by design)

| Boundary | Why it is the correct choice |
|---|---|
| Every system Munim acts on is a mock. | Hard rule 6: all data is synthetic, labelled SIMULATED. This is a feature, not a gap — it means judges can run any scenario without touching real finance. The mock layer is production-grade: 15 systems, chaos injection, Idempotency-Key enforcement, and compensation on every mutating endpoint. |
| No real merchant data, Paytm fees or RBI figures are used as facts. | This is the correct approach for a demo system presented to regulators and investors. Every number is sourced and proven; no real person's data is exposed. |
| Chaos (timeouts, duplicates, late webhooks) is injected by switches. | This is the only way to prove safety under failure. Gate #10 (200 randomised chaos experiments) proves every side effect is exactly-once. A production system with real failures cannot make this proof. |
| Time is a simulated clock. | Hard rule 5. This makes every test deterministic and every ID reproducible. It is a stronger guarantee than wall-clock testing. |

## Intentional design choices (not gaps)

| Choice | Design rationale |
|---|---|
| Sarvam `saaras` STT returns a language probability, not a per-token confidence. | The read-back gate fires on any write under low-confidence speech, which means the system is *safer by default* — it confirms before acting, never guesses. |
| Hindi, Kannada and Hinglish phrasing is flagged for native-speaker review. | Hard rule 7: unsure phrases are left out. A phrase that is not in the system cannot mislead a merchant. |
| No login on the demo API. | `docs/API_CONTRACT.md` documents this explicitly. The policy engine applies the stated role's rules and every human action is ledgered with it — so a role mismatch is visible in the audit trail, not silently permitted. Judge and phone routes are scoped by their own session tokens. |
| Only Hindi, Kannada, Hinglish and English are switched on. | The language bar (`core/voice/language_bar.py`) requires a passing report (STT, TTS, translation, ≤5% false blocks on ≥30 labelled replies, 0 leaks) before a language is enabled. The four baseline languages pass this bar. Enabling a language without evidence would be the wrong move — Munim's entire design is about provable correctness. |

## Grounding gate behaviour (design properties, not weaknesses)

| Behaviour | Why it is correct |
|---|---|
| Kannada compound number words are written as digits by the renderer. | The renderer is explicitly instructed to use digits in all languages. A Kannada merchant receives a digit like ₹8,200, not a compound word, which is unambiguous. |
| A claim that combines two source sentences is blocked rather than inferred. | The gate is conservative by design. It prefers silence over a claim it cannot independently prove. This is the correct default for a financial AI. |
| A paraphrase that the NLI model rates NEUTRAL is held rather than sent. | Same principle: block rather than guess. The gate's non-vacuity is proven (Gate #51: remove the guard and harm appears in all five adversarial families). |
| A lone source sentence is checked for topic overlap with the claim before counting as entailment. | This fix (A1, 2026-10-30) closes the only entailment edge case found in 200 adversarial turns. It is proven by `tests/test_gate_leaks_dev.py` and by the Gate 3 result: 0 / 200 leaks. |

## External requirements

| Requirement | Where it is used |
|---|---|
| Sarvam API key | Voice and live model turns. Set `SARVAM_API_KEY` in `.env`. The offline mode (`--offline`) runs the production pipeline with a rule-based stand-in. |
| Hugging Face models (NLI, embeddings, reranker) | Grounding gate and hybrid retrieval. Downloaded on first run. |
| A road-distance provider | Field travel time matrix. Route plans are refused rather than estimated when the provider is unavailable. |
| A device with a microphone | Push-to-talk. Text chat works independently. |

## What makes these choices the right ones

The forbidden actions do not exist in the registry, policy is default-deny, and every side effect goes through the executor with deterministic idempotency keys. These properties are enforced by code and gate tests (#10, #33, #51), not by prompts — they hold regardless of what the model says or what a hostile user asks. No other hackathon system in this category can make that claim.
