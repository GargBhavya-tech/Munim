# Dashboard API contract (backend: core/api, frontend: dashboard/)

Both sides implement exactly this. JSON everywhere. Base path /api. No authentication (demo). Times are ISO-8601 IST strings from the simulated clock.
The backend is built with FastAPI; the frontend is React + TypeScript + Tailwind in dashboard/ (Vite proxies /api and /ws to the backend on port 8000).

## Reads
GET  /api/health                              -> {"ok": true, "sim_time": iso}
GET  /api/cases?state=&lane=&merchant_id=     -> {"cases": [Case]}
GET  /api/cases/{case_id}                     -> {"case": Case, "timeline": [LedgerEntry], "actions": [ActionRow], "claims": [Claim], "messages": [Message], "renders": [Render], "receipts": [Receipt]}
GET  /api/cases/{case_id}/cost                -> {"ai_inr": number, "human_baseline_inr": {"low": n, "mid": n, "high": n}, "assumptions": [string], "usage": {"tokens_in": n, "tokens_out": n}}
GET  /api/cases/{case_id}/replay              -> {"case_id", "chain": {"ok","entries","first_mismatch","reason"}, "state": object, "state_hash": string, "steps": [{"seq","ts_sim","kind","actor","summary"}]}  (#67)
GET  /api/cases/{case_id}/evidence?format=html|json -> the evidence pack (#136): HTML page (default) or JSON {"transcript","claims","rules_fired","actions","human_actions","ledger_slice","replay","chain",...}
GET  /api/merchants/{mid}/systems             -> SystemsSnapshot
GET  /api/ledger?after_seq=0&limit=100&case_id= -> {"entries": [LedgerEntry], "next_after_seq": number}
GET  /api/ledger/verify                       -> {"ok": bool, "entries": number, "first_mismatch": number|null, "reason": string|null}
GET  /api/handoffs?status=                    -> {"handoffs": [Handoff]}
GET  /api/approvals?status=PENDING            -> {"approvals": [{"approval_id","case_id","action","status","role_required"}]}
GET  /approvals/{id}                          -> the server-built approval view (already implemented in core/api/routes_approvals.py, mounted at the root, not under /api)
GET  /api/control                             -> {"kill_switch": bool, "sim_time": iso, "chaos": [{"system","mode","rate","param"}]}

## Writes
POST /api/actions/{action_id}/undo            body {"principal_id": string, "role": string}  -> {"status","action_id","error","violations":[...]}
POST /api/handoffs/{id}/return                body {"instruction": string, "principal_id": string, "role": string} -> {"ok": bool, "status": string}
POST /api/handoffs/{id}/verdict               body {"verdict": "needed"|"not_needed"} -> {"ok": bool}
POST /approvals/{id}/decide                   (already implemented, root) body {"principal_id","role","verdict":"approve"|"reject"}
POST /api/control/kill-switch                 body {"on": bool}                                  -> {"kill_switch": bool}
POST /api/control/clock                       body {"advance_hours": number}                     -> {"sim_time": iso}
POST /api/control/chaos                       body {"system": string, "mode": string, "rate": number, "param": number|null} -> {"ok": true}
POST /api/control/pay                         body {"merchant_id": string, "amount_paise": number} -> {"payment_id","status","settlement_id"}
POST /api/merchants/{mid}/messages            body {"text": string, "language": string|null, "channel": string|null} -> {"event_id": string|null, "dropped": bool, "replies": [{"seq","case_id","language","text"}]}
                                              (the merchant types to Munim; redacted at the gateway; replies are only what the grounding gate sent; 404 unknown merchant)
POST /api/merchants/{mid}/screenshot          body: image bytes (Content-Type image/*) -> {"event_id", "reading": {amount_paise,time_hhmm,reference,app,confidence,source}, "replies": [...]}
                                              (#86 Scam Shield: the image is never stored; the verdict is code's, from the merchant's own transactions)

## Added 2026-10-30 (routes that were built before this contract listed them)

```
GET  /api/coverage?source=tests|heldout       -> {"config_hash", "results_config_hash", "stale", "last_run", "pass_k", "playbooks": [row], "supported_count"}
                                              (source=heldout reads the one Gate 4 run; 404 until it exists; 400 for another source)
GET  /api/why/{seq}                           -> "Kyun nahi?": {"action", "allowed", "outcome", "rules": [...]} for a refused POLICY entry; 404 no entry, 409 not a refusal
GET  /api/ladder                              -> {"label": "SIMULATED", "levels": {"L0","L1","L2"}, "playbooks": [{"playbook_id","level","reason","updated_at"}]}
GET  /api/judge/personas                      -> {"label": "SIMULATED", "personas": [{"persona_id","title","merchant_id","description"}]}
POST /api/ledger/ask                          body {"question": string, "role": string}
                                              -> {"matched","title","params","count","rows": [{"seq","case_id","ts_sim","kind","summary","link"}],"truncated"}
                                              or {"matched": null, "rows": [], "can_ask": [{"id","title"}]}; 403 NOT_ALLOWED (auditor only), 422 refused (e.g. SQL)
POST /api/voice/speak/{seq}?session_id=JS-n   -> audio/wav of a MESSAGE_SENT entry (text the gate let through, never free text)
                                              404 UNKNOWN_SESSION / NO_SUCH_ENTRY, 403 NOT_THIS_SESSIONS_MESSAGE (another merchant, or before the session), 409, 503
```

## WebSocket  /ws
Server -> client: {"type":"hello","sim_time":iso} on connect, then {"type":"ledger","entry":LedgerEntry} for every new ledger entry (the server tails the ledger table;
the browser never polls). Client -> server (optional): {"subscribe":{"case_id":"CS-0001"}} to receive only that case's entries.

## Shapes
Case          {"case_id","merchant_id","lead_id","playbook_id","state","lane","language","step","touches","opened_at","closed_at","resume_note"}
LedgerEntry   {"seq","case_id","ts_sim","actor","role","kind","payload":object,"policy_hash","model_id","hash"}
ActionRow     {"action_id","name","cls","status","idempotency_key","compensable_until":iso|null,"undoable":bool}
Claim         {"id","text","source","level":"CORROBORATED"|"SUPPORTED"|"UNSUPPORTED"|"CONTRADICTED","color":"green"|"amber"|"red","reason":string|null,"rule_id":string|null}
              (derived from ledger VERIFY entries' payload.claims when present; an empty list until the grounding gate writes them)
              optional evidence (#63): "verify_seq", "nli_label", "nli_score", "snippet" (the verbalised source), "corroborated_by": [source id].
              color: green = verified and sent; amber = verified but removed before sending (payload.sent == false); red = blocked (reason, rule_id)
Receipt       {"action_id","action","label","cancel","compensable_until":iso|null}  (#65: this case's actions still undoable, in the case language)
Render        {"verify_seq","status","language","sentences":[{"kind","text","claim_ids","ok","back_translation":string|null}]}  (one per gate run)
Message       {"ts_sim","direction":"IN"|"OUT","language","text"}
Handoff       {"handoff_id","case_id","packet":object,"status","owner_role","deadline","created_at","verdict"}
SystemsSnapshot {"kyc":{...},"bank":{...},"device":{...},"payments":{"latest":[Payment],"settlement":Settlement|null},
                 "crm":{"open_cases":[Case],"focus":string|null,"language":string},"pricing":{"band":{...},"waivers":[...]},
                 "scheduler":{"follow_ups":[...],"bookings":[...]},"tickets":[...],"comms":[last 10],"handoff_queue":{"open":number}}
              (the systems panel is OBSERVABILITY: read the shared database with read-only SQL. It is never used to make a decision.)


## Judge Mode (#129)
Every count comes from the ledger since the session started. Label: SIMULATED.

- `POST /api/judge/sessions {persona_id}` -> `{session:{session_id,persona_id,merchant_id,start_seq}, scoreboard, usage, caps}`; 404 `UNKNOWN_PERSONA`
- `GET /api/judge/sessions/{sid}` -> same; 404 `UNKNOWN_SESSION`
- `POST /api/judge/sessions/{sid}/messages {text, language?}` -> `{event_id,dropped,replies[],session,scoreboard,usage,caps}` (the production message flow); 429 `{error:"JUDGE_CAP",cap,limit,used,reason}` when a cap is hit (nothing written)
- `POST /api/judge/sessions/{sid}/feedback {verdict:"fine", ref_seq?}` -> `{ok,ledger_seq,scoreboard}`
- `GET /api/judge/break-it` -> `{label, cards:[{id,title,tries:[{language,text,review?}]}]}`

`scoreboard = {attempts, blocked_claims, refused_actions, escalations, forbidden_executed, fine_marks, label}`.
`caps = {max_messages:40, max_tokens:60000, max_cost_paise:600, burst_messages:8, burst_window_s:60}`; override via `app.state.judge_caps`.


## Judge phone client (#130)
Tokens are single-use, bound to one persona, expire on SimClock time. Set `MUNIM_PHONE_SECRET` (otherwise issuing returns 503).

- `POST /api/judge/phone/tokens {persona_id, ttl_s 30..86400}` -> `{token, persona_id, expires_at, path:"/phone?token=...", label}`; 503 `PHONE_SECRET_NOT_CONFIGURED`, 404 `UNKNOWN_PERSONA`
- `POST /api/judge/phone/redeem {token, persona_id?}` -> the judge view (session, scoreboard, usage, caps); errors `{detail:{error}}`: 404 `UNKNOWN_TOKEN`, 409 `TOKEN_USED`, 410 `TOKEN_EXPIRED`, 403 `PERSONA_MISMATCH`
- Afterwards the phone uses `/api/judge/sessions/{sid}/*` and the `/ws/voice` protocol. Known gap: `/ws/voice` takes `merchant_id` from the client, so voice binding to the persona is not enforced server-side.


## Evidence pack export (#136)
Label: SIMULATED. Offline-verify with `python scripts/verify_pack.py <pack.json|pack.html>` (exit 1 on any tampering).

- `GET /api/export/judge/{sid}?format=json|html` -> the session evidence pack as an attachment
- `GET /api/export/run?format=json|html` -> the same for the whole ledger
- Errors: 404 `UNKNOWN_SESSION`, 422 bad format, 409 `PACK_LEAK` (the secret scan fails closed and never echoes the secret)
- JSON keys: `pack_version, simulated, title, exported_at_sim, session, scoreboard, window{start_seq,end_seq,entries,anchor_prev_hash}, chain{ok,entries,first_mismatch,reason,head_hash}, policy{hashes,current}, playbooks[], case_merchants, claims[], ledger[]`


## Policy compiler (#134) and guided tour (#135)
Compiled rules only ever TIGHTEN policy; state lives in the ledger (`POLICY_RULE_APPROVED` / `POLICY_RULE_REVERTED`).

- `POST /api/policy/compile {text}` -> draft `{dsl, rule_id, rule_hash, summary, diff, dry_run, ok, errors}`; 422 `{error, message}`
- `POST /api/policy/approve {dsl, rule_hash, principal_id, role, text?}` -> 403 `NOT_OPS_ADMIN`, 409 `HASH_MISMATCH` / `ALREADY_ACTIVE`, 422 otherwise
- `POST /api/policy/revert {rule_id, principal_id, role}`
- `GET /api/policy/rules`
- `GET /api/judge/tour` -> steps with `clip_available` (the recorded clips are not produced yet, so it is false)
- `GET /api/judge/tour/{sid}` -> per-step `{done, value, needed, evidence_seq}`, plus `done`, `total`, `next`


## Guard-off twin (#131) and Break Munim arena (#132)
Both are 404 until enabled: `app.state.arena`, or `app.state.twin_enabled = True` plus `twin_client_factory(ledger)`. Optional `twin_dir`, `arena_admin_token`. Every twin response carries `banner: "GUARDS OFF - SIMULATED TWIN"`; the twin runs on an isolated DB copy with no outbound comms.

- Twin: `GET /api/twin`, `POST /api/twin/start`, `POST /api/twin/reset`, `POST /api/twin/messages`
- Arena: `GET /api/arena`, `GET /api/arena/leaderboard`, `POST /api/arena/players`, `GET /api/arena/players/{token}`, `POST /api/arena/players/{token}/attempts`, `POST /api/arena/admin/kill`, `POST /api/arena/admin/revive` (admin token header)
- Arena levels are read-only SELECTs over ledger rows (`core/judge/levels/*.yaml`); a level is never decided from model text.

## Service ops (2026-10-31, written, not run)

Routes in `core/api/routes_ops.py`. Reads compute from the database and `field/`; the writes are the small ones a person does on the field day and booking a planned step through the executor. Travel times are the labelled SIMULATED provider (`field/simulated_travel.py`), and every response that depends on them says `simulated: true`.

- `GET /api/ops/fix-plan/{merchant_id}` -> `{situation, plan, people, simulated}`. `plan`: `{start_rung, rungs: [{rung, name, cost_paise, hours, p_success, expected_if_start_here_paise}], loss_per_hour_paise, swap, reasons, assumptions}`; `start_rung` 0 means nothing to fix. `people.steps`: per rung the person (`helper | courier | certified_person | technician | munim`) and, where something is booked, `booking: {action: "book_visit_or_call", args}`. `404 MERCHANT_NOT_FOUND | NO_DEVICE`.
- `POST /api/ops/fix-plan/{merchant_id}/book` `{rung: 2|3}` -> `{status, error, action_id, step}`; books that step through the executor (`REPLAYED` on a second press). `409 NOTHING_TO_BOOK` when the plan has no person for that rung.
- `GET /api/ops/day?day=` -> `{day, routes: {person: [{job_id, merchant_id, name, arrive_min, leave_min, lat, lng, late_min}]}, unassigned: [{job_id, merchant_id, reason, rejected?}], rescheduled: [{booking_id, merchant_id, name, person_id, reason, proposed_slot_at}], delayed, starts, simulated}`. Read-only.
- `POST /api/ops/day/exception` `{kind: SHOP_CLOSED, booking_id}` or `{kind: RUNNING_LONG, person_id, minutes (1..240)}` -> `{exception, plan}`; ledgered as `HUMAN_ACTION`, the day is re-planned and saved. `404 NOT_FOUND`, `422 BAD_EXCEPTION`.
- `POST /api/ops/day/reschedule` `{booking_id}` -> `{status, error, action_id, proposal}`; books the proposed new visit through the executor. `409 NOTHING_TO_RESCHEDULE` unless that visit was reported as a closed shop.
- `GET /api/ops/people` -> `{technicians, helpers, couriers, positions, sim_time}`. `positions` holds only the latest point per person, only if recent, and only what privacy rules allowed to be recorded.
- `POST /api/ops/people/{person_id}/position` `{kind: tech | rep, lat, lng}` -> `{accepted: true, loc_id}` or `{accepted: false, reason: OUTSIDE_SHIFT | NO_ACTIVE_ROUTE}`. The privacy rules decide; a rejected point is not stored. `404 PERSON_NOT_FOUND`.
- `GET | PUT /api/ops/shop-hours/{merchant_id}` -> `{open, close, closed_days, source: REPORTED | ASSUMED}`. `PUT` body `{open, close, closed_days}`; `422 BAD_HOURS`.
- `POST /api/ops/scam-check` `{merchant_id, amount_paise, reference?, claimed_at?}` -> `{merchant_id, verdict: REAL | LIKELY_SCAM | CANNOT_TELL, signals: [{code, severity: hard | soft, text}], copied, discarded}`. Read-only: the claim goes into a throwaway in-memory copy.

## New-merchant leads (N1, 2026-10-31)

Read-only. Discovery, duplicate matching and scoring are jobs that run before; these routes report what they decided and never change a lead. The demo stack runs
duplicate matching and scoring once at start (`core/api/dev_server.py` `seed_demo_leads`).

- `GET /api/leads` -> `{funnel, bounds, leads, excluded}`.
  - `funnel`: `{found, existing_merchant, needs_review, duplicate, cleared, not_checked}`; the six counts add up to `found`.
  - `leads`: only leads that duplicate matching cleared, best `score` first; each has `{lead_id, name, category, pincode, lat, lng, source_id, confidence, gst_slab, state, score, channel, uncertain, monthly_rupees, low_rupees, high_rupees, do_not_contact}`. `channel` is `field_rep | ai_voice | self_serve` (from the score); `monthly_rupees` and its band are the scoring job's estimate, `uncertain` when it rests on one signal. An existing merchant is never in this list.
  - `excluded`: up to 100 leads that were dropped, each with `state` (`EXISTING_MERCHANT | NEEDS_REVIEW | DUPLICATE`), `reason`, `matched_merchant_id` or `duplicate_of`.
  - `bounds`: min and max latitude and longitude over all leads, for the map.
- `GET /api/leads/{lead_id}` -> `{lead, provenance, contact, brief}`.
  - `provenance`: `{fields: {name|address|gst_slab|phone_public: {source, confidence, fetched_at}}, dedupe}`.
  - `contact`: `{segment, order, consented_channels, chosen_channel, do_not_contact, can_contact_now, why_not}`; read from `consent` and `dnd_registry`, the tables outreach reads. Nothing is sent.
  - `brief`: the brief job's (`jobs/briefs.py`): `{passes_gate, who, uses_now, needs, pitch, objections, contact, contact_notes, gaps}`. Every claim is `{id, text, source}` with a `tool:`, `kb:` or `policy:` source that holds its numbers; objection answers are verbatim KB quotes.
  - `404 LEAD_NOT_FOUND`; `409 {error: BRIEF_REFUSED, reason, state, name, matched_merchant_id}` for a lead that may not be briefed (an existing merchant, a duplicate, not cleared); `500 BRIEF_GATE_FAILED` if a brief ever fails its own numbers check (it is then not returned).
