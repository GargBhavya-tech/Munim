# Munim

Munim is one AI teammate that finishes Paytm merchant cases across sales, customer service and field operations, by voice and text in Hindi, Kannada, Hinglish and English. It takes real actions across (mock) Paytm systems and proves every sentence it says. Entry for the Paytm Build for India AI Hackathon, Track 3 (Autonomous AI Teammates), finals 31 Oct 2026.

## Source of truth

- `MUNIM_Bible_Merged.md` defines **intent** (the why). Everything in it is in buildable scope. There are no stretch tiers. Never remove content from it: relabel or annotate instead. (`bible.md` is an older draft. Use the merged one.)
- `MUNIM_Build_Map.md` defines **execution order**: 160 tickets, each with Spec / Watch out / Test / Done when. `Blocked by` only points to lower numbers. **Never start a ticket whose blockers fail their Test.** Where the Bible and the map disagree, the Bible defines intent and the map defines execution.
- The four gates always get their full tests: **#10** exactly-once (`tests/gate_exactly_once.py`), **#33** forbidden never executes (`tests/gate_forbidden.py`), **#51** nothing unproven is said (`tests/gate_grounding.py`) and **#146** one frozen held-out run.
- `docs/BUILD_STATUS.md` records which tickets are done, partial or not started. Update it when you finish a ticket. **Read its "Status as of …" section first:** the per-phase ticket tables below it are older and still say NOT STARTED for many built tickets.
- `docs/HANDOFF.md`: read it FIRST in a new session; it says what was just done, what is in progress and what is next.
- `docs/BUILD_PLAN.md` is the ordered list of everything still open to finals (problem, fix, test, done-when per row). Work it top to bottom.
- `docs/ENGINEERING_RULES.md` covers file ownership for parallel work, the hard rules for code, and the test standard (real expected values, a failing test for every "Watch out", and mutation-proving the riskiest guards). Read it before writing code.
- `docs/API_CONTRACT.md` is the exact backend ↔ dashboard contract (`core/api` ↔ `dashboard/`).

## Layout

`core/{engine,actions,audit,gate,memory,llm,api,eval,voice,vision,judge}` is the domain-agnostic core. `packs/paytm_merchant/` holds the playbooks (YAML), mocks, policy rules and limits, KB, and the dev/held-out scenarios. `packs/demo_lender/` is a second domain (lender collections) running on the same engine with no change to `core/`; `packs/paytm_consumer/` is a smaller pack. `jobs/` holds background jobs (health, recurrence, ticket order, leads, outreach, incidents, style bandit). `field/` is field ops (matching, OR-Tools routing, briefings, visits, territory). `integrations/` holds the MCP server and the mock lender agent. `eval/frozen/` holds manifest-pinned frozen sets (LF endings; changing one changes the Gate 4 freeze hash). `deck/` builds the deck and refuses any number without a source. `dashboard/` is Vite + React + TS + Tailwind. The test harness lives in `tests/{conftest,world,scenario_runner,checkpoint_script}.py`. `tests/test_checkpoint_s1.py` shows a whole case end to end.

## Setup and tests on Windows

Python 3.12 is used here, and every pinned wheel exists for it. The py launcher is required. **Set `PYTHONUTF8=1`** (the setup script and Makefile do). Several modules and tests call `read_text()` without an encoding, and on Windows that decodes the Hindi/Kannada sources as cp1252 and fails. Tests are slow on Windows (around 1s of SQLite setup per test, likely antivirus scanning `%TEMP%`). A full run takes tens of minutes. Run the files you touched while working.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1        # venv, deps, .env, hook, dashboard, tests
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Ml    # also torch/transformers for the grounding tests
```

Manual steps:

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pip install -r requirements-ml.txt   # needed: tests/test_nli_gate.py imports torch; test_retrieval.py uses embeddings/rerank
bash scripts/install_hooks.sh                                 # pre-commit secret grep
cd dashboard && npm ci && npm run build
PYTHONUTF8=1 .venv/Scripts/python -m pytest                    # full suite
```

- Run without ML deps: `MUNIM_SKIP_MODEL_TESTS=1 .venv/Scripts/python -m pytest --ignore=tests/test_nli_gate.py --ignore=tests/test_retrieval.py`.
- The first model run downloads Hugging Face models.
- Dashboard tests: `cd dashboard && npm test`.
- The Makefile picks `.venv/Scripts` on Windows if you have `make`.
- `.env` is local only. Copy it from `.env.example`, never commit it, and leave keys empty unless you are running the Sarvam spike.

## Setup and tests on Linux / macOS

```bash
make setup                                                     # venv, deps, hook, dashboard build, full pytest
make setup-ml                                                  # torch/transformers for the grounding tests
.venv/bin/python -m pytest tests/test_turn.py                  # one file (prefer this while working)
MUNIM_SKIP_MODEL_TESTS=1 .venv/bin/python -m pytest --ignore=tests/test_nli_gate.py --ignore=tests/test_retrieval.py
```

`make seed` reseeds `data/munim.db` (`data/seed.py --seed 42`).

## Running it

```bash
.venv/bin/python -m core.api.dev_server --port 8000            # seeded DB, in-process mocks, real policy + executor, scripted decider
.venv/bin/python -m core.api.dev_server --port 8000 --llm      # production turn agent: needs SARVAM_API_KEY and requirements-ml.txt
cd dashboard && npm run dev                                    # Vite proxies /api, /approvals and /ws to :8000
.venv/bin/python -m core.cli verify-ledger                     # also: replay <case_id>, eval --split dev
.venv/bin/python scripts/preflight.py                          # pre-demo checks (keys, build, frozen sets, held-out run, ledger, deck)
.venv/bin/python -m core.cli heldout                           # Gate 4: says whether the ONE held-out run is possible; --confirm runs and stores it
```

Dashboard hash routes: `#/judge` (Judge Mode), `#/arena` (Break Munim), `#/coverage`, `#/ladder`, `#/control`, `#/phone/<token>` (text + push-to-talk), `#/simple/<token>` (zero-literacy client, no dashboard chrome).

## Hard rules (enforced by code and tests, never by prompts)

1. **The AI never computes numbers.** Every number a merchant sees comes from a system read. Money is integer paise. The LLM decides and writes; code enforces.
2. **Every side effect goes through `core.actions.executor`**, using deterministic idempotency keys (`sha256(case_id|action|canonical_json(args)|step_seq)`). Never generate a fresh key on retry. Never call a mock directly. The ledger entry comes before the side effect.
3. **Policy is default-deny.** A write not explicitly allowed by the current playbook step is refused. Forbidden actions (`approve_kyc`, `change_bank_details`, `change_settlement_account`, `credit_decision`, `delete_record`, `accept_documents_in_chat`, `request_otp`) must not exist in the registry. Humans go through the same policy, and there is no admin bypass.
4. **Nothing unverified is said.** Claims need a source that exists in the turn's context, and their numbers must match that source. Offer tools are absent during open service cases (fix-first). They are not just discouraged.
5. **Never read the wall clock.** Time comes from the injected `SimClock` (`tests/test_clock.py` lints this). IDs and seeds are deterministic.
6. **All data is synthetic** and labelled **SIMULATED** on screen. No real personal data, no real Paytm fees or RBI figures as facts.
7. **Never name earlier projects** in docs, code or commits.
8. Hindi, Kannada and Hinglish must be natural. If you are unsure of a phrase, leave it out and flag it for native review.
