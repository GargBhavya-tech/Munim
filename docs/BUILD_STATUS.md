# Build status against `MUNIM_Build_Map.md`

Assessed 2026-10-29 on commit `a5a5d63`. A Windows 11 + Python 3.12 checkout was mapped from the code and tests in the repo.

**Legend:**
- **DONE**: the ticket's Test exists in `tests/` and passes.
- **PARTIAL**: some of the Spec is built or tested, but the Done-when isn't met.
- **NOT STARTED**: nothing meaningful in the repo.

"Passes" refers to the test run recorded under *Test run* below. Update this file when a ticket changes state.

## Status as of 2026-10-30 (read this before the tables below)

The tables below were last edited before about 20 teammate commits landed (growth pipeline, field matching, Judge Mode backend, evidence sets, MCP server, event backbone, voice duplex and others), so many rows still say NOT STARTED for tickets that are built. This section is the corrected view. It is derived from commit messages and the files present, **not from a test run**.

| Status | Count |
|---|---|
| DONE | 138 |
| PARTIAL | 12 |
| NOT STARTED | 10 |

**Built in the latest session, each with its own passing tests (full suite not run):**
- #146 Gate 4 machinery: held-out set runs once per config hash, deck numbers only from that run, frozen sets pinned to LF (`core/eval/gate4.py`, `tests/test_gate4.py`). The held-out run itself has NOT been done.
  **Now wired (2026-10-30):** `munim heldout --confirm` (`core/eval/heldout.py`, `tests/heldout_play.py`, `tests/test_heldout.py`) plays every pack's held-out scenarios 4 times and the frozen labelled sets once (policy probes, escalation snapshots, the English items of the claim set through the real number check + entailment model), computes the deck metrics from those outcomes and stores one `metrics_runs` row. Without `--confirm` it plays nothing. It refuses a second run for the same freeze hash before playing, refuses a partial run, and stores nothing if the hash changes mid-run. Indic grounding stays NOT MEASURED (needs the live translator). The run itself has still NOT been done.
- #149 coverage matrix from the held-out run: `GET /api/coverage?source=heldout`, shows config hash and time, stale when code moved on
- #155 limitations document: `docs/LIMITATIONS.md`
- #150 venue soak: 1,000 scripted attempts clear no level; per-player cap, kill switch and the budget stop at the configured paise are exercised, with a scripted model so no real Sarvam budget is spent (`tests/test_venue_soak.py`)
- #126 language framework: a language is on only with a passing report (STT, TTS, translate, false-block <= 5% on >= 30 labelled replies, 0 leaks); only the four baseline languages are on. The other seven candidates have NO labelled sets or live checks yet, so none is enabled (`core/voice/language_bar.py`, `config/languages.yaml`, `tests/test_language_bar.py`). The candidate list is an assumption, not Paytm's confirmed eleven.
- #114 lender collections pack: D1 payment reminder and D2 consented promise-to-pay, intents, 5 KB docs, 12 dev and 6 held-out scenarios, all passing on the real engine with NO change to `core/` (`packs/demo_lender/`, `tests/lender_world.py`, `tests/test_lender_pack.py`). hi / kn phrases and the Kannada intent words need native review.
- #115 mock lender agent: signed request, token-only, consent-gated; the lender decides; a slow lender gives "I'll check and come back" with no decision (`integrations/lender_agent.py`, `tests/test_lender_agent.py`). Not yet wired into the S3 loan playbook.
- #122 zero-literacy client (`#/simple/<token>`, `dashboard/src/screens/Simple.tsx`): big icon buttons (talk, yes, no, undo, say again, a person), every reply spoken as it arrives, no text field anywhere, confirmation by tick and cross. Tested with a scripted service case (voice question, tick, undo) and no typing. Not heard on a real device.
- Spoken replies: `POST /api/voice/speak/{seq}` voices only a MESSAGE_SENT ledger entry (text the gate let through, never free text); `/ws/voice` transcripts now carry the replies the engine wrote for that utterance; the phone and simple clients speak them (server voice, browser voice as fallback) (`core/api/routes_voice.py`, `dashboard/src/lib/spoken.ts`, `pushToTalk.ts`). Needs a live key for real Sarvam audio.
- First full backend run (2026-10-30, without the two model files): 10 failures, 8 fixed (lender pack missing from the mock-access lint; the held-out manifest order and CRLF test bugs on Windows; a stale status string; an RFP test wording; the twin network test on Windows). The last 2 were the teammate's new rule `FIELD-NOTIFY-001` having no probe in the frozen policy-probe set; four hand-labelled probes were added (v2 of the set, 464 probes, manifest rewritten) before any held-out run, so the Gate 4 freeze hash changed once and no held-out result exists to invalidate. The full suite has not been re-run since.
- #119 Indic entailment (E5): the comparison harness and report exist (`core/eval/e5.py`, `tests/test_e5.py`): false-block, leak and agreement with intervals for back-translation vs the Indic model, and a report is INCOMPLETE unless it also covers our own labelled hi and kn replies (the frozen claim set with KB premises). The training script (`scripts/train_indic_nli.py`) has a tested dry-run but has NEVER been run for real: it needs a GPU, hours and the IndicXNLI files. `models/indic_nli/` is empty, so the E5 report is NOT RUN. **Status: PARTIAL.**
- #152 regulatory texts and #153 Paytm figures: re-checked on 2026-10-30 from the regulators' and Paytm's own pages (fetch and search summaries, not every page read line by line). Bible register rows are now VERIFIED (KFS obligation, cooling-off, 2019 failed-payment circular, payment-data storage) or PARTLY VERIFIED (FREE-AI, TRAI, DPDP, with a notification-date conflict flagged); the Paytm Q2 figure ₹2,648 crore is TOTAL revenue, not operating revenue (the Bible was wrong); the Q1 datapack was not read so the employee and distribution figures stay UNVERIFIED; the Soundbox date conflict is unresolved. `deck/figures.json` is the register. KB wording was deliberately NOT changed (it feeds the frozen claim set). **Status: PARTIAL.**
- #156 deck: the builder refuses any number without a source (`deck/slides.yaml`, `deck/figures.json`, `core/eval/deck.py`, `tests/test_deck.py`): Paytm figures only if VERIFIED, results only from the held-out run for the current code, counts from the repository, any typed digit is an error. A DRAFT can be rendered (`python -m core.eval.deck --draft`); the FINAL deck cannot be built until the held-out run exists. **Status: PARTIAL.**
- #159 pre-flight: `python scripts/preflight.py` automates the software checks (keys present, never shown; dashboard built; frozen sets; held-out run; ledger; kill switch; deck figures; recordings) (`tests/test_preflight.py`). Hand items are listed.
- #151 interviews, #154 organiser request, #157 video, #158 rehearsal, #160 submission: **human tickets, not done and not doable by me**. `docs/EVENT_KIT.md` has the interview guide and consent wording, the request to send, the shot plan against what the product shows, the rehearsal run sheet and the submission checklist.
- #143 guard ablations: 8 guards each proven by removal (`tests/test_ablations.py`, `core/eval/ablation_table.py`)
- #123 questions to the ledger: fixed read-only templates, auditor only, SQL refused (`core/audit/questions.py`, `POST /api/ledger/ask`, 20 tests)
- #148 rollout ladder: per-playbook L0/L1/L2, floors, automatic demotion, ledgered, `GET /api/ladder`, dashboard screen (13 tests + 1 vitest)
- #147 assurance suite: **DONE**, all 32 scenarios (A1-A32) in `tests/test_assurance.py`, each safe with its guard and harmful with only that guard removed (A8 and A21-style defence in depth: the two independent rules are removed together; A11's pricing mock is a second layer, so harm there means the call reached it). Full suite not run.

**Still partial, and what is actually left on each:**
- #11 #36 #39 #48: need a live Sarvam key.
- #110 briefings: the text briefing passes the gate; the voice note (TTS) is not built.
- #131 #132 #134: built and tested; not yet run against the real backend in a browser.
- #133 soundbox prop: hardware (speaker/phone or Pi/ESP32) and a venue Wi-Fi test.
- #135 guided tour: 8 live steps exist; the recorded fallback clip per step needs recordings.
- #136 evidence pack: HTML export works and verifies its ledger slice; PDF output is not built.
- #141 merchant simulator: runs on a scripted stand-in; the LLM persona on the fallback model is not wired.
- #142 metrics: `munim eval --split dev` now exists (`core/eval/report_cli.py`, `tests/test_eval_report.py`): it runs the dev scenarios through pytest, records attempts against the code hash, and prints Tier B (pass rate overall and per playbook, pass^k) and two safety rows with Wilson intervals; it refuses stale results and never runs the held-out split. Still NOT measured by it: grounding catch/false-block, policy precision/recall, forbidden executed, tool-failure handling, Scam Shield accuracy, turns/touches, and cost; each is printed as NOT MEASURED HERE with where it is measured. Not yet run end to end (a full dev run takes tens of minutes on Windows).
- #144 noisy-speech set: SNR mixing is done and verified; the 40 clips (open noise corpus plus real recordings) and the report do not exist.
**Not started:** only the human or external tickets #151, #154, #157, #158 and #160 (see `docs/EVENT_KIT.md`). #119, #122, #126, #146, #149, #150, #155 and #156, once listed here, are built (see above); #119, #146 and #156 are PARTIAL.
**Judge Mode dashboard screens now exist** (`#/judge` with scoreboard, break-it card, tour, twin banner and evidence link; `#/arena`; `#/phone/<token>` text chat and push-to-talk). They are tested against an offline mock (70 dashboard tests pass) and have **not been run against the real backend in a browser**. Phone push-to-talk records and sends audio to `/ws/voice`, shows the transcript and now speaks the replies.

## Test run

Windows 11, Python 3.12.10, run on 2026-10-29:

| Suite | Result |
|---|---|
| `pytest`, 1,487 tests (everything except the two model files) | **1,484 passed, 3 failed** in 20 min |
| `pytest tests/test_nli_gate.py tests/test_retrieval.py`, 46 tests (with `requirements-ml.txt`) | **45 passed, 1 failed**. English NLI 48/50 = 96% (bar ≥ 95%) |
| Dashboard `npm test` (vitest) | **28 passed** (8 files). `npm run build` OK |

**Total: 1,529 of 1,533 Python tests passed. All 4 failures are Windows-only.** These four call `read_text()` or print with no encoding argument, so Windows uses cp1252:
- `test_clock::test_no_module_reads_the_wall_clock_except_the_clock`
- `test_executor::test_no_module_outside_the_executor_calls_a_mock_directly`
- `test_intents::test_top1_accuracy_per_language_is_reported`
- `test_retrieval::test_hit_rate_on_labelled_set`

All 4 pass when rerun with `PYTHONUTF8=1` (now set by `scripts/setup_windows.ps1` and the Makefile). No real product bugs were found.

The full suite was not re-run end to end with `PYTHONUTF8=1`; only the 4 failures were. Product code with the same latent issue, which `PYTHONUTF8=1` covers:
- `core/engine/policy.py:105`
- `core/llm/sarvam_http.py:20`
- `core/memory/store.py:22` (`read_text()` without `encoding="utf-8"`)

**Since that run (2026-10-29, later):** the full suite has NOT been re-run, at the owner's instruction, until all the new work is in. Each new ticket's own tests were run and pass: #69 17, #34 11, #36 31, #37 15, #38 20, #39-#41 46, #43-#50 verify 17 + render/faithfulness/compliance 61 + tiers/pipeline 29, dashboard 31 vitest (3 new); together 247 new Python tests pass in 96 s, plus **Gate 3's 7 tests** (7.5 min with the real entailment model). Two shared-file changes can affect old tests and must be covered by the next full run: `tests/conftest.py` (loads `tests/results_plugin.py`) and `core/api/app.py` (new optional kwargs).

**Grounding fix, 2026-10-30 (BUILD_PLAN A1):** a lone source sentence now counts as entailment only when it is about the claim (`core/gate/verify.py` `about`), which closes a wrong-citation leak ("Merchant city is Mysuru." entailed "Your KYC is approved."); identifiers in claims must match the source verbatim and their digits are not quantities; numbers in field names (`last_30d`) are available; clock times are verbalised with 12-hour forms; pack aliases (`packs/paytm_merchant/gate_aliases.yaml`, soundbox -> device) reach the entailment check only. Found and tested on NEW dev sources (`tests/test_gate_leaks_dev.py`), not the frozen set; the frozen claim set was deliberately NOT re-run. Gate 3 still 0/200, English NLI 96%, 323 gate/retrieval tests pass.

**Partial full run, Linux, 2026-10-30 (BUILD_PLAN step 0.2):** `pytest` over all 2,912 collected tests, model tests included, was **stopped at the owner's instruction at about 51%**: ~1,520 tests had run and **all passed, 0 failed, 0 errors**. The other ~49% (the later files in collection order) have NOT been run in this pass. Dashboard: `npm test` **80 passed** (18 files), `npm run build` OK. A complete run is still owed before the Gate 4 freeze (BUILD_PLAN E1).

Mutation checks (a guard is temporarily broken and its test must fail) were run for every new guard: #69 3, #34 3, #36 6, #37 7, #38 9, #39-#41 10, #43-#50 13; all were caught. Two initially survived (a redaction bypass hidden by `json.dumps` escaping, and a missing assertion) and were fixed.

## Gates

| Gate | Status | Evidence |
|---|---|---|
| **#10 Gate 1: exactly-once under chaos** | **DONE** | `tests/gate_exactly_once.py`: 200 randomised writes, `timeout_after_commit` 30% + `duplicate_webhook` 20%, same-key retries, plus an error500 variant and a multi-seed run. |
| **#33 Gate 2: forbidden never executes** | **DONE** | `tests/gate_forbidden.py`: 500 hostile proposals against a hand-written expected table, through the full pipeline. |
| **#51 Gate 3: nothing unproven is said** | **DONE (offline; the 200 turns share an author with the gate, there is no independent frozen set until #138, and its blocker #48 is still PARTIAL)** | `tests/gate_grounding.py`: 200 adversarial turns (invented fees, invented dates/times, promises, renderer additions, wrong Hindi/Kannada/Hinglish number words) through the whole gate with the REAL entailment model; expected outcomes are a hand-written table. **0 / 200 leaks, 0 true claims falsely blocked** (A/B/C: 120 bad claims all blocked, 120 clean replies sent; D/E: 40 clean second renders sent, 40 dirty second renders escalated). Non-vacuity: with verification and faithfulness removed, the same turns leak in all five families. **Not measured:** live Sarvam renderer and translator (the translator here is a hand-written map), so the real false-block rate of back-translation is still open. Running it surfaced and fixed two gate bugs (an unrelated policy text "contradicting" true claims, and exact-sentence KB claims scoring NEUTRAL). |
| **#146 Gate 4: frozen held-out run** | **PARTIAL** | Machinery and runner built (`core/eval/gate4.py`, `core/eval/heldout.py`, `munim heldout --confirm`). The one run for the final configuration has not been done. |

## Ticket map

### Phase 0: Foundations, mocks, ledger (#1–#12)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 1 | Repo skeleton | DONE | `test_repo_layout.py`, `test_source_check.py`. `make setup` is Linux-first; Windows uses `scripts/setup_windows.ps1` (added). The pre-commit secret hook installs and runs under Git Bash. |
| 2 | Simulated clock | DONE | `test_clock.py` (wall-clock lint + timer test). The lint needs `PYTHONUTF8=1` on Windows. |
| 3 | Schema and seed | DONE | `test_store_seed.py` (byte-identical seed, WAL). |
| 4 | Mock reads | DONE | `test_mocks_read.py`, 15 systems in `packs/paytm_merchant/mocks/`. |
| 5 | Mock writes + Idempotency-Key | DONE | `test_mocks_write.py`. |
| 6 | Chaos switches | DONE | `test_chaos.py`. |
| 7 | Event log | DONE | `test_events.py`. |
| 8 | Ledger hash chain | DONE | `test_ledger.py`. |
| 9 | Replay | DONE | `test_replay.py`, `core/audit/replay.py`, `core/cli.py` (`scripts/munim replay`). |
| 10 | 🚨 Gate 1 | DONE | See Gates. |
| 11 | Sarvam spike | PARTIAL | `scripts/sarvam_spike.py` + `test_sarvam_spike.py` pass offline. The commit reports a live run (JSON 100% with reasoning off; STT CER), but `eval/out/spike.json` is gitignored and not in the repo, and there are no TTS/translate numbers in the commit. Re-run with a key to fill the Bible §39 switch rules. |
| 12 | Synthetic KB | DONE | 19 docs in `packs/paytm_merchant/kb/`, `test_kb.py`. Reviewer sign-off is not recorded. |

### Phase 1: Engine (#13–#22)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 13 | Playbook schema/loader | DONE | `test_playbook_loader.py`, `test_safe_expr.py`. |
| 14 | Diagnose | DONE | `test_diagnose.py`. |
| 15 | Case state machine + loop | DONE | `test_workflow.py` (crash-restart exactly once). |
| 16 | Timers | DONE | `test_timers.py`. |
| 17 | Triage: events | DONE | `test_triage_events.py`. |
| 18 | Triage: intents | DONE | `test_intents.py`, `labelled/triage_messages.yaml`. |
| 19 | Uncertainty / multi-intent | DONE | `test_triage_uncertainty.py`. |
| 20 | Lanes and focus | DONE | `test_case_manager.py`. |
| 21 | Fix-first | DONE | `test_fix_first.py`. |
| 22 | Contact budget (checkpoint) | DONE | `test_contact_budget.py`, `test_checkpoint_s1.py` (S1 end to end, scripted decisions). |

### Phase 2: Actions and policy (#23–#33)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 23 | Registry + risk classes | DONE | `test_registry.py`. |
| 24 | Idempotency keys | DONE | `test_idempotency.py`. |
| 25 | Executor | DONE | `test_executor.py`. |
| 26 | Outbox | DONE | `test_outbox.py`. |
| 27 | Compensation + Undo | DONE | `test_compensation.py`. |
| 28 | Circuit breaker | DONE | `test_circuit_breaker.py`. |
| 29 | Policy engine | DONE | `test_policy.py` (default-deny, policy hash). |
| 30 | The rules (16) | DONE | `test_policy_rules.py`, `test_integration_policy_fixes.py`. |
| 31 | Human roles | DONE | `test_roles.py`. |
| 32 | Approval views | DONE | `test_approval_view.py`. |
| 33 | 🚨 Gate 2 | DONE | See Gates. |

### Phase 3: Model layer, redaction, voice (#34–#41)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 34 | LLM client | DONE | `core/llm/client.py`, `config/llm.yaml`, `test_llm_client.py`. Sarvam first, one retry (timeouts, 408/429/5xx), then the fallback; `on_fallback` banner flag; every call is a `CONTEXT_HASH` ledger entry with `model_id` of the model that ANSWERED, `prompt_hash`, tokens, latency and the failed attempts. Prompt text never reaches the ledger. **Not verified live:** no Sarvam key was used (tested over mock transports), and **no fallback provider is configured** (`config/llm.yaml` is empty on purpose; set `base_url`, `model` and `FALLBACK_LLM_KEY`). Its blocker #11 is still PARTIAL. |
| 35 | Redaction | DONE | `test_redact.py` (incl. property test). |
| 36 | Turn agent contract | PARTIAL | `core/llm/decision.py` (`TurnDecision`, `parse_decision`, `decide`), `test_llm_decision.py`. Claim-only schema (extra keys forbidden, so no `reply` field; every claim needs a `tool:`/`kb:`/`policy:` source; replies disguised as claims rejected), one retry then `INVALID_OUTPUT` (feeds `HardTriggers` `invalid_output_count`), LLM outage reported as `UPSTREAM_UNAVAILABLE`. **Not done:** the Test's "20 canned contexts, at least 19 valid on Sarvam" needs a live key. The spike's 100% was measured against a looser schema, so re-run it against `TurnDecision`. The engine does not call `decide` yet (production assembly). |
| 37 | Context builder | DONE | `core/memory/context.py`, `core/llm/prompts/system.md`, `test_context_budget.py`. Seven blocks with the Bible §28 budgets; the system prompt and playbook raise `ContextOverflow` instead of being trimmed; lower blocks drop whole items from the low-priority end; the system message holds no merchant text (MERCHANT_SAID facts are DATA); the latest merchant message is last and fenced, and forged fences are defanged. **Assumption:** token counts are a conservative estimate (no tokenizer in the repo), not Sarvam's real count. |
| 38 | Decision guard | DONE | `core/engine/guard.py`, `test_decision_guard.py`. Allowlist, fix-first (park), arg schema, subject ids, model-written text, numbers copied from the merchant's message (`ARG-FROM-MERCHANT`), and a dry run of the real policy engine against live reads (waiver band etc.); `new_case_requests` become OPEN/PARK/LINK/REJECT; refusals are ledgered as `POLICY` entries and explained by "Kyun nahi?". Checking executes nothing. The executor still re-checks. **Not done:** the engine does not call the guard yet (production assembly). |
| 39 | Voice: push-to-talk | PARTIAL | `core/voice/pipeline.py`, `core/api/routes_voice.py` (`WS /ws/voice`), `config/voice.yaml`, `test_voice.py`. Clip → STT → redaction (fail-closed) → `MERCHANT_MESSAGE` event with `stt_confidence` and `low_confidence`; reply text → TTS; per-stage latency; raw audio only with consent. **Finding:** the Sarvam REST STT returns no per-transcript confidence (only `language_probability`, only when the language is auto-detected), so a missing value counts as LOW and writes need a read-back (#40). **Not verified:** the Test's 10 real clips per language and "a Hindi and a Kannada voice turn end to end" need a live key and real audio. Tested against a fake Sarvam. |
| 40 | Read-back before action | DONE | `core/voice/readback.py`, `test_voice.py`. A write on low-confidence speech is held; the prompt is built by code from the validated args (₹ formatted from integer paise); only a short, clear yes confirms, once; a different number (digits or spoken words), a contradiction or noise never confirms; silence and timeout expire it and a late yes cannot revive it. Thresholds in `config/voice.yaml` are assumptions until #144. Hindi/Kannada/Hinglish wording needs native review. |
| 41 | Live language switching | DONE | `core/voice/language.py`, `test_voice.py`. Explicit requests in three scripts save the preference through the executor (`save_preference`, R1, undoable) and a superseding `language` profile fact; never inferred from a name; a low-confidence STT flip is asked ONCE, a confident flip adopts the language for one turn only. Tested with a fake STT. |

### Phase 4: Grounding gate (#42–#51)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 42 | Number normalisation | DONE | `test_numbers.py`. |
| 43 | Source + numbers check | DONE | `test_source_check.py`, `core/gate/sources.py`. |
| 44 | Hybrid retrieval | DONE | `test_retrieval.py` (needs `requirements-ml.txt`; downloads models on first run). |
| 45 | English entailment | DONE | `test_nli_gate.py` (needs ML deps). |
| 46 | Corroboration levels | DONE | `test_corroboration.py`. |
| 47 | Renderer | DONE | `core/gate/render.py`, `core/gate/phrases.py`, `packs/paytm_merchant/phrases.yaml`, `test_gate_render_faith.py`. Sentences are `{claim_ids, text}` or a fixed phrase id that code expands; an uncited sentence, an unverified claim id, a reworded phrase, extra keys or more claim sentences than claims are rejected; numbers must be digits. Phrase wording is DRAFT_UNREVIEWED. Tested with a scripted model; live Sarvam output quality is unmeasured. |
| 48 | Faithfulness check | PARTIAL | `core/gate/faithfulness.py`, `test_gate_render_faith.py`. Numbers (incl. Hindi/Kannada number words) on the rendered text and on the back-translation, English entailment with a verified claim as the premise, optional Indic similarity floor; fixed phrases exempt. **Not done:** the Test's "50 labelled replies → false-block rate" needs the live translator and the #138 labelled set. **Known limitation:** the number parser (#42) reads Kannada compounds like "ಎಂಟು ಸಾವಿರದ ಇನ್ನೂರು" as 8000, so such a sentence is falsely blocked (safe direction); the renderer is told to write digits. |
| 49 | Compliance phrases | DONE | `core/gate/compliance.py`, `test_gate_render_faith.py`, `test_gate_pipeline.py`. Identity + "Paytm never asks for your PIN or OTP" on first contact and a truthful yes to "are you an AI?" in en/hi/hinglish/kn, added by code; reply language checked by script. Question patterns and phrase wording need native review. |
| 50 | Confidence tiers | DONE | `core/gate/tiers.py`, `test_gate_pipeline.py`. WHITE/BLUE/RED from the soft score (never the model's confidence) and the model's ask-to-clarify; BLUE and RED strip any action; a second BLUE in a row (counted from the ledger's `VERIFY` entries) becomes RED. BLUE asks one fixed clarifying question; RED hands off with a fixed line. |
| 51 | 🚨 Gate 3 | DONE | See Gates. |

### Phase 5: Escalation and memory (#52–#60)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 52 | Hard triggers | DONE | `test_escalation_hard.py`, `test_integration_escalation.py`. |
| 53 | Soft score | DONE | `test_soft_score.py` (0.375 / 0.65). |
| 54 | Handoff packet | DONE | `test_handoff_packet.py`. |
| 55 | Handoff queue + return | DONE | `test_handoff_queue.py`. |
| 56 | Profile facts | DONE | `test_profile_facts.py`, `test_integration_facts.py`. |
| 57 | Timeline | DONE | `test_timeline.py`. |
| 58 | Verified summaries | DONE | `test_episode_verification.py`. The checker is done; the summarizer is not LLM-backed yet (#34/#36). |
| 59 | CRM field permissions | DONE | `test_crm_permissions.py`. |
| 60 | Consent withdrawal / deletion | DONE | `test_deletion.py`. |

### Phase 6: Dashboard (#61–#69)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 61 | API + WebSocket | DONE | `test_api_*.py`, `core/api/ws_events.py` (ledger tail, push). Token streaming awaits #34. |
| 62 | Cases board | DONE | `dashboard/src/screens/CasesBoard.tsx`, `lib/board.test.ts`. |
| 63 | Case view with claim chips | DONE | `ClaimChips.tsx` (tap for source, reason, rule id, entailment score, snippet; amber = verified but removed before sending), English back-translation toggle and browser playback in `CaseView.tsx`, `lib/conversation.ts`, `ClaimChips.test.tsx`, `test_api_cases.py`. The gate now writes the evidence into the `VERIFY` entry (`core/gate/pipeline.py`). **Not seen in a live browser.** Playback uses the browser's speech synthesis on the stored text, not Bulbul. |
| 64 | Systems panel | DONE | `SystemsPanel.tsx`, `lib/systems.test.ts`, `test_api_systems.py`. |
| 65 | Undo + receipts | DONE | Staff Undo plus merchant "wapas karo" -> `UNDO_LAST` -> `core/engine/merchant_undo.py` (this case only, through the executor, window enforced), receipt cards in the case language, `test_merchant_undo.py`. Receipt wording in hi / hinglish / kn needs native review. |
| 66 | Handoff queue + approval UI | DONE | `Handoffs.tsx`, `ApprovalRefusal.test.tsx` (server refusal shown). |
| 67 | Audit view | DONE | `GET /api/cases/{id}/replay`, replay button and evidence-pack link in `Audit.tsx`, `test_evidence_replay.py`. |
| 68 | Control panel + cost meter | DONE | `Control.tsx`, `test_api_control.py` (kill switch incl. queued outbox), `/api/cases/{id}/cost`. |
| 69 | Coverage matrix, "Kyun nahi?" (checkpoint) | DONE | `core/eval/matrix.py`, `core/api/routes_coverage.py` (`/api/coverage`, `/api/why/{seq}`), `tests/results_plugin.py` (`pytest --record-scenarios`), dashboard `Coverage` screen and the Kyun nahi? button on refused ledger rows, `test_coverage_matrix.py`. Supported is computed only from recorded scenario results for the current code hash (stale results show unsupported; held-out needs 4 clean attempts). **Checkpoint line not yet met:** the dashboard has not been run against the real backend in a browser, and #63 claim chips are still partial. |

### Phase 7: Sales playbooks (#70–#76)
Supported bar = ≥6 dev + ≥3 held-out scenarios passing.

| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 70 | S1 activation | DONE | 6 dev / 3 held-out, `test_scenarios_s1_c2.py`; the lifecycle test shows `RESOLVED` only when all five fields are true. |
| 71 | S2 Soundbox upsell | DONE | 8 dev / 3 held-out, `test_sales_playbooks.py`, `test_scenarios_sales.py`. |
| 72 | S3 loan | DONE | 8 dev / 3 held-out. |
| 73 | S4 plan change | DONE | 8 dev / 3 held-out. |
| 74 | S5 win-back | DONE | 7 dev / 3 held-out. |
| 75 | Pricing / waivers | DONE | `test_pricing_offers.py`. |
| 76 | Personalisation | DONE | `test_personalisation.py`. Styles are applied to templates; the renderer (#47) is not built. |

### Phase 8: Service playbooks (#77–#95)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 77 | Knowledge layer (C8) | DONE | `test_knowledge_layer.py`, 6 dev / 3 held-out. |
| 78 | C1 settlement | DONE | 7 dev / 3 held-out, `test_service_playbooks_a.py`. |
| 79 | C2 Soundbox fault | DONE | 6 dev / 3 held-out, `test_scenarios_s1_c2.py`; replacement precondition enforced, second replacement escalates. C2 conversational steps now complete from the ledger (`case_facts.conversational_steps_done`). |
| 80 | C3 payment failed | DONE | 6 dev / 3 held-out. |
| 81 | C4 refund | DONE | 6 dev / 3 held-out. |
| 82 | C5 detail update | DONE | 6 dev / 3 held-out. |
| 83 | C6 charge dispute | DONE | 7 dev / 3 held-out, `test_service_playbooks_b.py`. |
| 84 | C7 closure | DONE | 6 dev / 3 held-out. |
| 85 | C9 complaint | DONE | 7 dev / 3 held-out. |
| 86 | C10 Scam Shield | DONE | `core/vision/read_screenshot.py` (fields only; the schema rejects any verdict), `packs/paytm_merchant/scam_shield.py` (verdicts by code), 20 synthetic screenshots, 6 dev / 4 held-out scenarios, `test_scam_shield.py`, `test_scenarios_c10.py`. **Not measured:** a live vision model (`LLMVisionReader` is tested only with a scripted client). Reply wording needs native review. |
| 87 | X0 unsupported | DONE | 6 dev / 3 held-out. |
| 88 | I1 area incidents | PARTIAL | `jobs/incidents.py`: one incident per pincode or bank, NetworkX impact graph from rows, hold (policy rule `INCIDENT-HOLD-001` plus parking), one message per merchant, resolve. `test_incidents.py`: 30 devices in 560034 within 6 minutes -> 1 incident, 0 replacements. **No I1 scenarios yet**, so not at the supported bar. |
| 89 | C11/C12 consumer pack | DONE | `packs/paytm_consumer/` (own mocks, registry, fields, rules, 2 playbooks, 9 scenarios each); core fields are now pluggable (`register_fields`); `test_consumer_pack.py` proves it loads with no merchant-pack import. |
| 90 | Fewer tickets | DONE | `test_ticket_policy.py`. |
| 91 | Merged tickets | DONE | `test_ticket_merge.py`. |
| 92 | Ordered tickets | DONE | `test_queue_order.py`. |
| 93 | Remote-fix toolkit | DONE | Restart, volume reset, config push with rollback (R2), connectivity check (`mocks/remote.py`); no firmware action exists; C2 now runs a remote fix before any replacement. `test_remote_fix.py`. |
| 94 | Merchant health | DONE | `test_health_job.py`. |
| 95 | Repeat issues (checkpoint) | DONE | `test_recurrence.py`. The checkpoint ("both run end to end on the dashboard") is not met while #63/#69 are partial. |

### Phase 9: Growth (#96–#104)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 96 | Source registry | DONE | `jobs/sources.py` (`SourceGate.fetch` is the only way lead data is fetched): unregistered and `manual_only` sources (Zomato, Swiggy, IndiaMART, JustDial) are refused, a permitted scrape needs `robots_ok` and a current review, per-minute rate limit on the sim clock, every decision ledgered with rule id `SOURCE-REGISTRY`. `test_source_registry.py` (A28). |
| 97 | Discovery job | DONE | `jobs/discovery.py`: Layer 1 (Paytm data -> `upsell_targets`, ranked by 30-day QR volume), Layers 2-3 through the source gate; `business_only` stores name, category, address, pincode, coordinates, a business phone and GST slab with per-field provenance and drops everything else. `jobs/synthetic_sources.py` are SIMULATED and deliberately carry owner names, mobiles, e-mails and IDs to prove they are dropped. `test_discovery.py`. **Not built:** robots.txt fetching and page caching (Layer 3 is a synthetic stand-in), change detection, the real Places/GST APIs. |
| 98 | Duplicate matching | DONE | `jobs/dedupe.py`: normalised name, phone, pincode and coordinates against every merchant and every better lead; EXISTING_MERCHANT / DUPLICATE / NEEDS_REVIEW (doubt blocks) / cleared; `outreach_ready` is the only query outreach may use and demands the CLEAR mark. All 40 seeded duplicates point at the right merchant; brute-force invariant that no ready lead matches any merchant; mutation-checked. **Gate G1 (Bible section 48) passes** on the synthetic seed. `test_dedupe.py`. |
| 99 | Turnover estimate and lead scoring | DONE | `jobs/scoring.py`: formula first (volume x P(switch) x P(later loan) / cost to acquire, constants are ASSUMPTIONs), single-signal estimates flagged uncertain, numpy logistic regression with a whitelist of scoring-time features that refuses post-conversion fields, walk-forward AUC on later weeks only (shuffled labels fall back to chance), ranked leads only from the cleared set, channel by value. `test_lead_scoring.py`. **Not built:** the weekly retrain as a scheduled job; trained only on SYNTHETIC conversions. |
| 100 | Account briefs | NOT STARTED | Needs the grounding gate's number-sourcing applied to briefs (gate G2). |
| 101 | Lead lifecycle | DONE | `jobs/lead_lifecycle.py`: validated state machine (all 144 state pairs tested against the table), only a dedupe-cleared lead may leave NEW, `win()` records the new merchant id and opens an S1 case, and a WON lead stays open (`WON_PENDING_FIRST_SETTLEMENT`) until S1 resolves (`ACTIVATED`); DO_NOT_CONTACT is final, adds the number to `dnd_registry` and cancels timers. Mutation-checked. `test_lead_lifecycle.py`. **Not built:** timers for NO_REPLY / NOT_NOW retries (they belong to #103). |
| 102–104 | Consented first contact, reply handling, style learning (bandit) | NOT STARTED | `dnd_registry` and `CONSENT-001` exist in policy, but there is no outreach code. |

### Phase 10: Field operations (#105–#113)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 105 | Rep/technician profiles | DONE | `field/profiles.py`, `tests/test_field_profiles_privacy.py`. Languages with levels, shift, base, training/stock (reps), skills/parts/job cap (technicians); Bayesian-shrunk win rates; no protected-trait columns (checked by `assert_no_protected_fields`); owner (or manager) can view/edit, system-derived fields are not editable; edits ledgered. |
| 106 | Travel times | DONE | `field/travel.py`, `tests/test_field_routing.py`. Road providers only (Maps with departure-time traffic, OSRM fallback), asymmetric matrix, cache keyed by coordinates + weekday/weekend + hour, `TravelUnavailable` when every provider fails (no straight-line fallback). Providers tested over mock transports; **not run against the real Maps or OSRM**. |
| 107 | Routing engine | PARTIAL | `field/routing.py`, `tests/test_field_routing.py`. OR-Tools sales (optional stops, skip penalty = value) and service (mandatory, soft deadlines reported) modes, exact subset-DP oracle, `replan`. **UNVERIFIED AT HAND-OVER:** 13 of 15 tests passed, then two bug fixes (non-stop matrix nodes were treated as mandatory; `replan` moved the end node) and one test correction were made and the tests were **not re-run**. Run `tests/test_field_routing.py` first; G4 (heuristic equals exact optimum) is not confirmed until it passes. |
| 108–112 | Rep matching, technician assignment, briefings, visit logging, territory redesign | NOT STARTED | `field/` has no matching or dispatch code yet. |
| 113 | Field privacy and coaching | DONE | `field/privacy.py`, `tests/test_field_profiles_privacy.py`. Location only inside the shift with an active route (rejections store nothing and log no coordinates); owner-only view/delete; analysis needs a `call_analysis` consent (CONSENT-COACH-001); output is a coaching note with no score/rank/penalty. |

### Phase 11: Platform extensions (#114–#127)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 114 | Lender collections pack | NOT STARTED | `packs/demo_lender/` skeleton. |
| 115 | Mock lender agent | NOT STARTED | (`mocks/lender.py` serves eligibility/links for S3, not the agent-to-agent protocol.) |
| 116–120 | Full-duplex voice, MCP server, event backbone, Indic NLI, RFP | NOT STARTED | `MCP-EXPOSE-001` exists as a rule only. |
| 121 | Mock mandate service | PARTIAL | `MANDATE-LIMIT-001` rule + `mandates` table + fixtures in `test_policy_rules.py`. No mandate mock service or E7 test. |
| 122–126 | Zero-literacy client, ledger questions, playbook drafts, incident map, 11 languages | NOT STARTED | — |
| 127 | Rival-network analysis | PARTIAL | `RIVAL-TARGET-001` rule + fixtures. No analysis or scorer. |

### Phase 12: Judge Mode and wow layer (#128–#137)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 129 | Persona cards + snapshots | PARTIAL | 6 personas seeded (`data/seed.py`, `personas` table). No snapshot/reset-to-state-hash. |
| 129–137 | Judge Mode, phone client, guard-off twin, arena, prop, policy compiler, tour, evidence pack, code-mix | NOT STARTED | `core/judge/` is empty. Part of #136 exists: `core/audit/evidence.py` and `GET /api/cases/{id}/evidence` (self-verifying per-case pack). |

### Phase 13: Evidence (#138–#150)
| # | Ticket | Status | Evidence / gap |
|---|---|---|---|
| 138 | Labelled claim set | NOT STARTED | — |
| 139 | Policy probes + escalation snapshots | NOT STARTED | Gate 2's hand table is a start, but not the frozen ~150/~150 sets. |
| 140 | Scenario library | PARTIAL | 90 dev + 39 held-out (129 files) across 14 playbooks (S1 has no held-out). Missing or short: S1 (2/0), C2, C10, I1, C11, C12. No held-out hash recorded. |
| 141–143 | Merchant simulator, metrics with intervals, guard ablations | NOT STARTED | — |
| 144 | Noisy-speech set | PARTIAL | `core/eval/noisy.py` (SNR mixing, measured). No clip set or report. |
| 145 | Latency and cost | PARTIAL | `core/eval/latency.py`, cost endpoint. No measurement tables. |
| 146 | 🚨 Gate 4 | PARTIAL | See Gates. |
| 147–150 | Assurance A1–A28, rollout ladder, coverage from results, venue soak | NOT STARTED | — |

### Phase 14: Ship (#151–#160)
All NOT STARTED in the repo. #151–#154 are external/manual (interviews, regulatory texts, figures re-check, organiser request); their state isn't visible from code.

## Totals

| Status | Count |
|---|---|
| DONE | 98 |
| PARTIAL | 12 |
| NOT STARTED | 50 |

## Next tickets whose blockers all pass

The critical path to a talking product is now: **wire the pieces together, then measure them live.**

1. **Production assembly (DONE, see the Production assembly section below; this paragraph is the pre-assembly hand-over note and is kept for history).** The turn agent (`decide`), the context builder, the decision guard, the read-back gate, the language manager and the grounding gate are each built and tested, but nothing calls them in sequence from the engine yet. Only the test harness wires the engine. Until this exists, "a merchant can talk to Munim" is true of the parts, not of the product.
2. **Live checks that need a Sarvam key** (all code is ready): re-run `scripts/sarvam_spike.py` against `TurnDecision` (closes #11 and #36), 10 real clips per language (closes #39), 50 labelled replies for the back-translation false-block rate (closes #48; needs #138). Pick and configure a fallback provider in `config/llm.yaml` (#34).
3. **Native-speaker review** of every Hindi, Kannada and Hinglish line in `packs/paytm_merchant/phrases.yaml`, `core/voice/readback.py`, `core/voice/language.py` and `core/gate/compliance.py`.
4. **#70 S1 to the supported bar** (blocker #69 is DONE): add at least 4 dev and 3 held-out S1 scenarios. That unblocks #79 C2 scenarios, #86 C10 Scam Shield and, with #69, #114 and #129 Judge Mode.
5. **Voice follow-ons #116 full-duplex voice and #122 zero-literacy client** are unblocked now that #39-#41 exist (#130 phone client still waits for #129 Judge Mode); **#137 code-mix mirroring** is unblocked by #47.
6. **Fully unblocked and independent:** #96 source registry gate, #105 field profiles (then #106 → #107), #128 persona snapshots, #138 labelled claim set (independence rule: it must be written by someone who did not write the prompts, and hash-frozen before any prompt is tuned against it; the prompts now exist, so do not let their author write it), #139 policy probes, #117 MCP server, #118 event backbone, #121 mandate service.
7. **Finish spine partials:** #65 merchant undo test and receipt card, #67 replay button, #63 end-to-end in a browser.

## Production assembly (done 2026-10-30)

`core/engine/turn.py` (`TurnAgent`) is the engine's decider: redaction, language, read-back, sources, bounded context, `decide`, the decision guard, confidence tier, the grounding gate, merchant undo and code-only playbooks (Scam Shield). `core/engine/assembly.py` wires it (`build_turn_agent`, `hard_escalation`, `wire`); `python -m core.api.dev_server --llm` runs it with Sarvam, and `POST /api/merchants/{mid}/messages` is the typed channel. `tests/test_turn_assembly.py` runs it through the real engine, policy engine, executor and mocks with scripted LLM, NLI and translator, so it proves the wiring and the guarantees, **not model quality**. Nothing here has run against a live Sarvam key.

Engine changes made on the way (all covered by tests): `Decision` gained `language`, `escalate`, `park`, `answers_merchant`; a failed read escalates instead of crashing; a merchant's hard-escalation words outrank "done"; `hold_fn` lets an area incident park a case before it acts; the dispatcher routes a message with no open case by intent; the SECRET-001 rule no longer pairs the code-added PIN/OTP warning with an amount in the next sentence; the guard no longer treats the merchant's own words as a system source.

## Hand-over notes (read first)

- **Nothing here has had a full test run since the Phase 3-5 work.** Run `PYTHONUTF8=1 .venv/Scripts/python -m pytest` first (about 20 minutes on Windows, with `requirements-ml.txt` installed). Shared files changed since the last full run: `tests/conftest.py` (loads `tests/results_plugin.py`) and `core/api/app.py` (new optional kwargs).
- **Run `tests/test_field_routing.py` before anything else** (see #107). No mutation checks were run on `field/`.
- **Not started from the plan, in this order:** #108 rep matching, #109 technician assignment, #96 source registry gate, #121 mandate service, #128 persona snapshots.
- **The biggest gap is production assembly:** nothing in the engine calls `core/llm/decision.decide`, `core/engine/guard`, `core/voice/readback`, or `core/gate/pipeline.GroundingGate` yet.
- Hindi, Kannada and Hinglish wording (`packs/paytm_merchant/phrases.yaml`, `core/voice/*`, `core/gate/compliance.py`) is DRAFT and needs a native speaker.
- `config/llm.yaml` has no fallback provider; live Sarvam checks (spike against `TurnDecision`, real clips, translation false-block rate) are still to do.
