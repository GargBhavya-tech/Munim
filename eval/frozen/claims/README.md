# Labelled claim set (#138)

`claims.jsonl`: one merchant-facing claim per line, in English, Hindi, Kannada and Hinglish. Each concept (group `Gnnn`) appears once per language with the same label and the same truth source.

Fields: `id`, `group`, `lang`, `text`, `label` (true | false | unsupported), `category`, `truth`, `claimed`, `claimed_kind`, `must_contain`.

- `true`: the claimed value equals the value in the cited source.
- `false`: the cited source holds a different value (the claim is contradicted).
- `unsupported`: nothing in the KB or the seeded database says it (`truth.kind == "none"` lists the patterns and queries that must find nothing).
- `truth.kind == "kb"`: `doc`, `section`, `regex` with one capture group (the true value). `"db"`: a SQL query on the database built by `data/seed.py --seed 42`; first column is the true value. Money is integer paise.

Independence: written from the Build Map spec, the Bible, the KB and the seed data only, never from prompts, the gate or the model layer. The labels come from comparing `claimed` with the source value; `tests/test_claim_set.py` re-checks every item. Hindi and Kannada review: see `NATIVE_REVIEW.json`.

Frozen: `MANIFEST.sha256` holds the sha256 of every file here. `tests/test_claim_set.py` fails if any file changes. Do not edit a frozen file; a corrected set is a new directory with a new manifest and an explicit decision.
