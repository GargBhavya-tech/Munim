# Limitations (#155)

Every limitation below is stated with its cause. Numbers are never softened: where a figure has not been measured, the entry says so and names what will measure it. Figures for the deck come only from the frozen held-out run (`core/eval/gate4.py`, `GET /api/coverage?source=heldout`), never from this file.

## What is simulated

| Limitation | Cause |
|---|---|
| Every system Munim acts on (settlement, payments, KYC status, tickets, lender) is a mock. | No access to real Paytm systems; hard rule 6 (all data synthetic, labelled SIMULATED). |
| No real merchant data, fees or RBI figures are used as facts. | Same rule. Figures in the KB are placeholders for the pack, not Paytm policy. |
| Chaos (timeouts, duplicates, late webhooks) is injected by switches we control. | Real failure modes will differ in shape and frequency; we test the guards, not production behaviour. |
| Time is a simulated clock. | Hard rule 5. Behaviour under real clock skew is not exercised. |

## What is unverified

| Limitation | Cause |
|---|---|
| Live speech-to-text and the Sarvam language model have not been run end to end here (#11, #36, #39, #48). | The code is written and tested against fakes; no live key was available in the build environment. |
| The microphone path (push-to-talk in the browser) has no automated test. | It needs a real device; only the WAV encoder is unit-tested. |
| Spoken replies and the zero-literacy client (#122) are built and tested against fakes, but have not been heard on a real device or with real Sarvam speech. | The browser microphone and audio playback paths need a real device; the server speech service needs a live key. |
| Hindi, Kannada and Hinglish phrasing has not had a full native-speaker review. | Pending human review (`eval/frozen/claims/NATIVE_REVIEW.json` tracks the claim set). Unsure phrases are left out, per hard rule 8. |
| There is no login. A human's role (auditor, risk reviewer, ops admin...) is whatever the request says, on every human route (approvals, handoffs, policy changes, ledger questions). | A demo API (`docs/API_CONTRACT.md`: "No authentication (demo)"). The policy engine still applies the stated role's rules and every human action is ledgered with it, so a wrong role is visible after the fact, not prevented. Judge and phone routes are scoped by their session. |
| The Gate 4 held-out result does not exist yet. | The held-out set runs once per configuration hash and has not been run on the current hash. Until it is, `deck_numbers` refuses. |
| The Paytm merchant pack is the full one. | The consumer pack (C11, C12) and the lender collections pack (D1, D2, #114) run on the same core with their own dev and held-out scenarios, but cover two playbooks each. |
| Only Hindi, Kannada, Hinglish and English are switched on. | The per-language bar (#126: STT, TTS, translation, at most 5% false-block on 30 or more labelled replies, 0 leaks) is built, but the other seven candidate languages have no labelled sets or live checks, so none is enabled. |

## Measured weaknesses

| Weakness | What we know | Cause |
|---|---|---|
| Noisy or unclear speech. | The Sarvam REST speech-to-text returns no confidence score, only a language probability, and only auto-detect. A missing confidence is treated as low, so the read-back gate fires and the merchant is asked to confirm. | The provider's API. Cost: more confirmations than a confidence-aware system would need. Noisy-speech word accuracy is **not yet measured**; it needs recorded noisy audio and a live key. |
| False blocks (a true reply the grounding gate refuses). | Gate 3 found no leaks of unsupported claims in 200 adversarial turns with the real NLI model, and the non-vacuity check shows the same turns leak with verification removed. The false-block rate on true replies is a **separate figure and is reported only from the held-out run**. Two causes of false blocks were found and fixed during Gate 3 (an unrelated policy text "contradicting" true claims; exact sentences of a long source scoring neutral). | The entailment model is general-purpose, not Indic fine-tuned (#119). It is conservative: it prefers blocking to saying something unproven. |
| Kannada numbers. | The number parser reads Kannada compound number words wrongly, so the renderer is told to write digits. | Parser coverage. Spoken Kannada numbers in merchant input may need read-back. |
| Long source documents. | The entailment model said neutral for an exact sentence taken from a longer document, so sources are checked sentence by sentence. | Model input behaviour. A claim that depends on combining two sentences is blocked rather than inferred. |
| A lone sentence "entailing" an unrelated claim (found and fixed 2026-10-30, BUILD_PLAN A1). | On a tiny premise such as "Merchant city is Mysuru." the entailment model said ENTAILMENT for "Your KYC is approved.", so a claim cited to the wrong read of the same merchant could be sent. Now a lone sentence counts only when it shares a content word with the claim; the whole source is checked as before. 5 wrong-citation and 5 wrong-fact dev claims are blocked (`tests/test_gate_leaks_dev.py`). | The general-purpose model on very short premises. A wrong citation whose sentence shares a word with the claim is still decided by the model alone. |
| Known false blocks after A1. | Still blocked although true: a contact window "15:00 to 17:00" against "you will be contacted only between 3 pm and 5 pm", and the KB line "a replacement for a device fault is free" against "a Soundbox replacement for a device fault is free". The numbers and words line up; the model stays NEUTRAL. Reason codes such as `DOC_BLURRY` cannot entail "the photo was blurry". | Model paraphrase limits; no code-to-English glossary for reason codes. Safe direction: the reply is held, never sent wrong. |
| Routing. | Route quality is checked against an exact subset oracle on small instances only. | Exact solving is infeasible at fleet size; larger routes rely on the heuristic solver. |

## External dependencies

| Dependency | Needed for | If missing |
|---|---|---|
| Sarvam API key | Speech-to-text, the language model, translation | Voice and model turns refuse or escalate; fallback model is empty on purpose (`config/llm.yaml`). |
| Hugging Face models (NLI, embeddings, reranker) | Grounding gate, retrieval | The gate cannot run; nothing unverified is said, so replies are blocked or escalated. |
| A road-distance provider | Field travel times | Route plans are refused rather than guessed. |
| A device with a microphone | Push-to-talk | Text chat still works. |

## Not limitations of the design (for contrast)

The forbidden actions do not exist in the registry, policy is default-deny, and every side effect goes through the executor with deterministic keys. These are enforced by code and gate tests (#10, #33, #51), not by prompts, so they do not depend on the model behaving.

## Finals-day state (2026-10-31)

- **The live model was not exercised on the machine that made these changes.** `SARVAM_API_KEY` is empty in that machine's `.env`, so every live turn there ended in the fixed hand-over line. The model-dependent demo beats (break-it attacks, fix-first, risk hold, waiver, twin, Break Munim) were last checked live on 2026-10-30 by the other developer, before the latest commits.
- **Not run today:** the wide regression for the hand-over changes, the tests written today (warm-up, follow-up on an escalated case), `munim eval --split dev` (the coverage screen shows 0 supported until it is run on the final code) and the held-out run.
- **A follow-up message on an escalated case** now gets the fixed hand-over line each time (it is not escalated again). A person must still pick the case up from the handoff queue; the line only tells the merchant so.
- **The warm-up** runs one real turn on a copy of the database at `--llm` start. It cannot prove the model stays reachable during the demo; if the network drops, the fallback is the hand-over line, then text, then the recording.

## Offline mode (no Sarvam key): started, NOT finished or verified (2026-10-31)

`python -m core.api.dev_server --offline` runs the production turn agent with a rule-based stand-in for the two model calls (`core/llm/offline.py`): intent from the pack's phrase lists, claims copied from system reads, a small per-playbook move table, English and Hinglish sentences. Policy, executor, grounding gate (real entailment model) and ledger are the real ones; nothing is bypassed.

First manual run through the real stack showed it works end to end, and also what is still wrong: (1) merchants whose profile language is Hindi or Kannada fail the "reply must be written in hi-IN" faithfulness check, because offline mode cannot write Devanagari or Kannada (plan: map hi-IN to Hinglish and kn-IN to English in offline mode only); (2) some claims come back NEUTRAL from the entailment model and the case is handed to a person (claim wording needs tuning against the source verbalisation); (3) the demo database's existing escalated cases (CS-0002, CS-0003) stay with a person, so test on a fresh database. The Hinglish back-translation is an exact lookup of the sentences the stand-in itself wrote, so that one check is trivially satisfied here. No automated tests exist for it and none were run.
