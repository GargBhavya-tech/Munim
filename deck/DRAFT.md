# Munim finals deck (DRAFT: numbers marked PENDING have no source yet.)

## Slide 1: Every merchant case, finished. Every word, proven.
*Title*

- One AI teammate that finishes Paytm merchant cases across sales, customer service and field operations, in Hindi, Kannada, Hinglish and English.
- [TEAM NAMES: add before submission]

## Slide 2: The problem in Paytm's terms
*Problem*

- 1.64 crore merchants on subscription plans, and the cases arrive mixed: a silent Soundbox, a late settlement and a loan question in one message.
- Answering is mostly solved. Finishing is not: a real fix is an action across systems.
- The fake-screenshot scam: a customer shows a payment that never arrived.
- [MERCHANT QUOTE: pending the shopkeeper interviews]

## Slide 3: What Munim does
*Solution*

- The case catalogue, across 16 playbooks in the merchant pack. One merchant, three cases, all finished.
- Growth: a lead map, an account brief with every number sourced, a consented first contact.
- Field: routes for reps and technicians, re-planned live when a shop closes.

## Slide 4: How it decides
*Tech stack*

- Who decides: the model reads and proposes; code enforces. Every number a merchant sees comes from a system read.
- The pipeline: understand, decide, policy, act through one executor, say only what is proven.
- Sarvam end to end for speech, language and translation; the gate checks Hindi and Kannada by back-translation.

## Slide 5: Watch it, try it
*USP*

- Judge Mode on your own phone: try to break it, or get a zero-percent loan out of it.
- Claim chips show the source of every sentence; the systems panel shows what really changed.
- The guard-off twin shows what the same attempt does without the guards.
- Scam Shield checks a payment screenshot against the merchant's own transactions.

## Slide 6: Safe to deploy
*USP*

- 18 policy rules, each with an ID you can read, default-deny, no admin bypass.
- Undo for reversible and compensable actions; server-built approval views; roles; a hash-chained ledger anyone can verify.
- A rollout ladder per playbook with automatic demotion.

## Slide 7: Measured, honestly
*Impact*

- Unsupported claims sent: [PENDING: held-out run].
- True replies wrongly blocked: [PENDING: held-out run].
- Forbidden actions executed: [PENDING: held-out run].
- Escalation recall on cases that needed a person: [PENDING: held-out run].
- Scenarios resolved, held-out: [PENDING: held-out run]; every held-out scenario passes in all runs: [PENDING: held-out run].
- All of it is simulated data, labelled, and every figure is from one frozen held-out run.

## Slide 8: Business model and Pi
*Business model*

- Who pays and outcome pricing: paid when a case is finished, not per message.
- Munim Core for Paytm Intelligence: the same safe layer for banks, lenders and insurers.
- Every merchant case, finished. Every word, proven.

## Backup slides

- **Privacy and data handling**: What is never collected, what is masked, what is logged. Synthetic data only in the demo.
- **Coverage matrix**: Per playbook: supported or not, with the reason. Computed from the held-out run.
- **Decision rules**: The policy rules, their IDs and sources.
- **Ablation table**: Each guard removed in turn: what then goes wrong.
- **Regulatory register**: What was read in the primary texts and what is still generic.
- **Competitive table**: Where Munim differs from answering bots and cold-outreach agents.

## Open problems

- slide seven: metric 'unsupported_claim_rate' needs the held-out run for the current configuration
- slide seven: metric 'grounding_false_block' needs the held-out run for the current configuration
- slide seven: metric 'forbidden_executed' needs the held-out run for the current configuration
- slide seven: metric 'escalation_recall' needs the held-out run for the current configuration
- slide seven: metric 'resolution_rate' needs the held-out run for the current configuration
- slide seven: metric 'pass_k' needs the held-out run for the current configuration

## Where every number comes from

- slide two, `fig:subscription_merchants_q2` = 1.64 crore merchants on subscription plans (https://paytm.com/blog/investor-relations/paytm-q2-fy27-results-pat-225-crore-ebitda-rises-90-to-270-crore/)
- slide three, `repo:playbooks` = 16 (counted from the repository now)
- slide six, `repo:policy_rules` = 18 (counted from the repository now)
