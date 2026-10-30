# Engineering rules for parallel work

Several engineers work in ONE working tree at the same time. These rules are what keeps them from clashing. They are not optional.

## Repository state
Phases 0 to 2 are committed and green: clock, schema, mocks, event log, ledger, case engine, policy, executor. Read the code you depend on
before writing anything: core/engine/*, core/actions/*, core/audit/*, core/memory/{store,knowledge}.py + schema.sql,
packs/paytm_merchant/{actions.py,mocks,policy,playbooks,kb}, core/eval/scenario.py, and the harness tests/{conftest,world,scenario_runner,checkpoint_script}.py.
`tests/test_checkpoint_s1.py` shows how a whole case runs end to end through the real components.

## Ownership (this is how we avoid clashes)
1. Create and edit ONLY the files listed under YOUR FILES in your brief.
2. EVERYTHING that already exists is READ-ONLY to you, in particular: core/memory/schema.sql, core/memory/store.py, core/audit/*, every existing
   core/engine/*, core/actions/*, core/eval/*, core/llm/*, packs/paytm_merchant/{actions.py,mocks/*,policy/*,intents.yaml}, existing playbooks, existing
   KB docs, config/*, tests/{conftest,world,scenario_runner,checkpoint_script}.py, requirements*.txt, Makefile, pytest.ini, package files outside dashboard/.
3. If you need a change in a read-only file, DO NOT make it. Work around it (an `ensure_schema()` with `CREATE TABLE IF NOT EXISTS` in your own module;
   injected collaborators; your own helper) and list the need under "NEEDS FROM INTEGRATOR" in your final report.
4. Never run a git write command (add, commit, stash, checkout, reset, restore, clean). Never spawn sub-agents.
5. Other engineers' files may be half-written. If an import of someone else's module breaks, do not touch it: wait, retry, and say so in your report.
6. A new playbook YAML in packs/paytm_merchant/playbooks/ is loaded by EVERYONE's tests (tests/world.py loads the whole directory). Only save a YAML there
   once it loads cleanly: write the draft under tempfile.mkdtemp(), run core.engine.playbook.load_playbook on it, then copy it into place.
   The same goes for scenario files and KB docs: complete and valid before they land.

## Hard rules for all code
- Never read the wall clock: no datetime.now/utcnow/date.today/time.time, no CURRENT_TIMESTAMP or datetime('now') in SQL (tests/test_clock.py lints this).
  Time comes from the injected SimClock. Backoff and sleeps use an injected callable.
- Money is integer paise. IDs are deterministic. All data is synthetic; no real person's data. No new dependencies unless your brief says so.
- Every side effect on a Paytm system goes through core.actions.executor (never call a mock directly). Every number a merchant sees comes from a system read.
- Hindi, Kannada and Hinglish text must be correct and natural. If you are not sure of a phrase, leave it out and say so; a native-speaker review is expected.
- The LLM decides and writes; code enforces. Nothing safety-relevant may depend on a prompt.

## Tests
- Tests must be real: concrete expected values, no tautologies. For every "Watch out" in your tickets write a test that FAILS if that mistake is present.
- PROVE it: for your 3 riskiest guards, temporarily mutate the code (or monkeypatch in a test) to introduce the mistake, confirm the test fails, restore it,
  and say which mutations you tried in your report. A guard that cannot fail proves nothing.
- Run only your own test files while working (nine others are running theirs). Run the full suite (`.venv/bin/python -m pytest -q`) ONCE at the end; a failure
  in files you do not own is reported, not fixed. If another engineer's in-progress file makes the full run noisy, rerun your own files and say so.

## Final report (be honest; it is read by the integrator, who re-runs your tests)
Per ticket: status and test count. Files created. Deviations from the ticket and why. What is NOT done or NOT verified. Which mutations you tried.
NEEDS FROM INTEGRATOR. A short interface summary (signatures other code should call).
