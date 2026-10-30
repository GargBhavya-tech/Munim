-- Munim schema. Money is integer paise. Timestamps are ISO-8601 strings in IST.
-- No SQL-side clock defaults anywhere: every timestamp is supplied by the SimClock.

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE counters (name TEXT PRIMARY KEY, n INTEGER NOT NULL);

-- ---------- Mock Paytm systems (state the agent can read and change) ----------
CREATE TABLE merchants (
  merchant_id TEXT PRIMARY KEY, name TEXT NOT NULL, segment TEXT NOT NULL, city TEXT NOT NULL,
  pincode TEXT NOT NULL, lat REAL, lng REAL, language TEXT NOT NULL, phone TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE', registered_at TEXT NOT NULL,
  avg_daily_volume_paise INTEGER NOT NULL DEFAULT 0,
  contact_window_start TEXT, contact_window_end TEXT, owner_rep_id TEXT
);
CREATE TABLE kyc (
  merchant_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE bank (
  merchant_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT, account_last4 TEXT, ifsc TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE links (
  link_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE devices (
  device_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
  battery_pct INTEGER, signal_pct INTEGER, last_heartbeat TEXT, last_test_announcement_ok INTEGER NOT NULL DEFAULT 0,
  installed_at TEXT
);
CREATE TABLE device_replacements (
  replacement_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, merchant_id TEXT NOT NULL, reason TEXT,
  status TEXT NOT NULL, created_at TEXT NOT NULL, compensable_until TEXT NOT NULL
);
CREATE TABLE device_orders (
  order_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, compensable_until TEXT NOT NULL
);
CREATE TABLE payments (
  payment_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL,
  ts TEXT NOT NULL, reference TEXT NOT NULL, settlement_id TEXT
);
CREATE INDEX idx_payments_merchant_ts ON payments(merchant_id, ts);
CREATE TABLE settlements (
  settlement_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL,
  expected_at TEXT NOT NULL, settled_at TEXT, reason TEXT
);
CREATE INDEX idx_settlements_merchant ON settlements(merchant_id, expected_at);
CREATE TABLE offer_bands (segment TEXT PRIMARY KEY, offer_id TEXT NOT NULL, max_months INTEGER NOT NULL);
CREATE TABLE offers (
  waiver_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, offer_id TEXT NOT NULL, months INTEGER NOT NULL,
  status TEXT NOT NULL, applied_at TEXT NOT NULL, revocable_until TEXT NOT NULL
);
CREATE TABLE plans (
  merchant_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, status TEXT NOT NULL, billing_date TEXT NOT NULL,
  monthly_fee_paise INTEGER NOT NULL, pending_plan_id TEXT
);
CREATE TABLE plan_changes (
  change_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, from_plan_id TEXT NOT NULL, to_plan_id TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, revertible_until TEXT NOT NULL
);
CREATE TABLE risk_flags (
  flag_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
  detail TEXT, ts TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN'
);
CREATE TABLE tickets (
  ticket_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL,
  system_object TEXT, summary TEXT, created_at TEXT NOT NULL, case_id TEXT
);
CREATE TABLE schedules (
  schedule_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, kind TEXT NOT NULL, due_at TEXT NOT NULL,
  case_id TEXT, note TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE bookings (
  booking_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, kind TEXT NOT NULL, person_id TEXT,
  slot_at TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE comms_outbox (
  comm_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, channel TEXT NOT NULL, language TEXT, body TEXT NOT NULL,
  case_id TEXT, status TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE lender_apps (
  app_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, partner TEXT NOT NULL, status TEXT NOT NULL,
  link TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE refunds (
  refund_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, payment_id TEXT NOT NULL, amount_paise INTEGER NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, compensable_until TEXT NOT NULL
);
CREATE TABLE closures (
  closure_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, reason TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL, compensable_until TEXT NOT NULL
);
CREATE TABLE mandates (
  mandate_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, monthly_limit_paise INTEGER NOT NULL,
  purpose TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
-- Mock plumbing: chaos switches and per-system idempotency stores.
CREATE TABLE chaos (
  system TEXT NOT NULL, mode TEXT NOT NULL, rate REAL NOT NULL, param REAL, PRIMARY KEY (system, mode)
);
CREATE TABLE mock_idempotency (
  system TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, status_code INTEGER NOT NULL,
  body_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (system, key)
);

-- ---------- Engine state ----------
CREATE TABLE cases (
  case_id TEXT PRIMARY KEY, merchant_id TEXT, lead_id TEXT, playbook_id TEXT NOT NULL, state TEXT NOT NULL,
  lane TEXT NOT NULL, language TEXT, step TEXT, touches INTEGER NOT NULL DEFAULT 0,
  opened_at TEXT NOT NULL, closed_at TEXT, resume_note TEXT
);
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, merchant_id TEXT, case_id TEXT,
  type TEXT NOT NULL, payload_json TEXT NOT NULL, ts_sim TEXT NOT NULL
);
CREATE TABLE case_events (case_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY (case_id, event_id));
CREATE TABLE turns (
  turn_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, ts_sim TEXT NOT NULL, role TEXT NOT NULL,
  text_redacted TEXT, language TEXT, stt_confidence REAL, decision_json TEXT, model_id TEXT
);
CREATE TABLE actions (
  action_id TEXT PRIMARY KEY, case_id TEXT, name TEXT NOT NULL, cls TEXT NOT NULL, args_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, step_seq INTEGER, ts_sim TEXT NOT NULL
);
CREATE TABLE idempotency (key TEXT PRIMARY KEY, action_id TEXT, result_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE timers (
  timer_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, kind TEXT NOT NULL, due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING', payload_json TEXT
);
CREATE TABLE compensations (
  action_id TEXT PRIMARY KEY, compensation_json TEXT NOT NULL, compensable_until TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN', result_json TEXT
);
CREATE TABLE breaker_state (
  system TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'CLOSED', failures INTEGER NOT NULL DEFAULT 0, opened_at TEXT
);
CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY, case_id TEXT, action_id TEXT, status TEXT NOT NULL, view_json TEXT NOT NULL,
  role_required TEXT, decided_by TEXT, decided_at TEXT
);
CREATE TABLE handoffs (
  handoff_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, packet_json TEXT NOT NULL, status TEXT NOT NULL,
  owner_role TEXT, deadline TEXT, created_at TEXT NOT NULL, verdict TEXT
);
CREATE TABLE incidents (
  incident_id TEXT PRIMARY KEY, kind TEXT NOT NULL, area TEXT NOT NULL, status TEXT NOT NULL,
  opened_at TEXT NOT NULL, closed_at TEXT, detail_json TEXT
);
CREATE TABLE consent (
  subject_id TEXT NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL, source TEXT, ts TEXT NOT NULL,
  PRIMARY KEY (subject_id, channel)
);
CREATE TABLE contact_touches (
  touch_id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id TEXT NOT NULL, channel TEXT NOT NULL, case_id TEXT,
  direction TEXT NOT NULL, ts TEXT NOT NULL
);

-- ---------- Memory and knowledge ----------
CREATE TABLE profile_facts (
  fact_id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
  source TEXT NOT NULL, evidence_ref TEXT, confidence REAL NOT NULL DEFAULT 1.0,
  valid_from TEXT NOT NULL, valid_to TEXT, consent_scope TEXT
);
CREATE TABLE episodes (
  episode_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, merchant_id TEXT, summary_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE kb_docs (
  doc_id TEXT PRIMARY KEY, version INTEGER NOT NULL, title TEXT NOT NULL, review_status TEXT NOT NULL,
  scopes TEXT, body_hash TEXT NOT NULL
);
CREATE TABLE kb_chunks (
  chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, version INTEGER NOT NULL, section_id TEXT NOT NULL,
  heading TEXT NOT NULL, text TEXT NOT NULL
);
CREATE VIRTUAL TABLE kb_chunks_fts USING fts5(chunk_id UNINDEXED, heading, text);
CREATE TABLE kb_vectors (chunk_id TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL);

-- ---------- Audit ----------
CREATE TABLE ledger (
  seq INTEGER PRIMARY KEY, case_id TEXT, ts_sim TEXT NOT NULL, ts_real TEXT NOT NULL, actor TEXT NOT NULL,
  role TEXT, kind TEXT NOT NULL, payload_json TEXT NOT NULL, policy_hash TEXT, model_id TEXT, prompt_hash TEXT,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX idx_ledger_case ON ledger(case_id, seq);
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE metrics_runs (
  run_id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, split TEXT NOT NULL, started_at TEXT, results_json TEXT NOT NULL
);
CREATE UNIQUE INDEX metrics_runs_one_heldout ON metrics_runs(config_hash) WHERE split = 'heldout';   -- Gate 4: one held-out run per hash

-- ---------- Growth and field ----------
CREATE TABLE sources (
  source_id TEXT PRIMARY KEY, name TEXT NOT NULL, access TEXT NOT NULL, terms_url TEXT, robots_ok INTEGER NOT NULL,
  rate_limit_per_min INTEGER, last_reviewed TEXT, refresh_days INTEGER
);
CREATE TABLE leads (
  lead_id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT, address TEXT, pincode TEXT, lat REAL, lng REAL,
  phone_public TEXT, gst_slab TEXT, source_id TEXT, fetched_at TEXT, confidence REAL, state TEXT NOT NULL,
  score REAL, provenance_json TEXT, matched_merchant_id TEXT
);
CREATE TABLE reps (
  rep_id TEXT PRIMARY KEY, name TEXT NOT NULL, base_lat REAL, base_lng REAL, shift_start TEXT, shift_end TEXT,
  languages_json TEXT NOT NULL, experience_months INTEGER, win_rates_json TEXT, training_json TEXT, stock_json TEXT
);
CREATE TABLE technicians (
  tech_id TEXT PRIMARY KEY, name TEXT NOT NULL, base_lat REAL, base_lng REAL, shift_start TEXT, shift_end TEXT,
  languages_json TEXT NOT NULL, skills_json TEXT, certifications_json TEXT, parts_json TEXT, max_jobs_per_day INTEGER
);
CREATE TABLE routes (
  route_id TEXT PRIMARY KEY, person_id TEXT NOT NULL, person_kind TEXT NOT NULL, route_date TEXT NOT NULL,
  plan_json TEXT NOT NULL, created_at TEXT NOT NULL
);

-- ---------- Judge Mode ----------
CREATE TABLE personas (
  persona_id TEXT PRIMARY KEY, title TEXT NOT NULL, merchant_id TEXT NOT NULL, description TEXT
);

-- Do-not-disturb registry (synthetic). Checked before any outbound contact to a lead.
CREATE TABLE dnd_registry (phone TEXT PRIMARY KEY);
