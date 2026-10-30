# Event kit (tickets 151, 154, 157, 158, 159, 160)

These tickets need a person: talking to shopkeepers, sending an email, recording, rehearsing, and submitting. Nothing here has been done for you. Each section is the material to do it with, taken from the Bible (sections 40, 48 and the demo script) and from what the product already does. Software checks for the morning of the event are automated: `python scripts/preflight.py`.

## 151. Shopkeeper interviews (1 to 3 conversations, about 20 minutes each)

**Who:** a nearby kirana or chemist with a Paytm Soundbox. A Paytm support or field person counts too. **Before you start:** ask permission. Take notes only. Do not record without clear consent. Write no personal data (no names, phone numbers or shop addresses); note the person's role only.

**Say first:** "I am building a tool to help shopkeepers get Paytm problems fixed faster. I would like to ask a few questions about how it works for you today. I will not write down your name or number. You can stop any time."

**The eight questions**
1. When your Soundbox stops announcing, what do you do first? Who do you call? How long does it take?
2. Has a settlement ever been late? How did you find out? What did support say?
3. When you signed up, what was the hardest step? Did anything stall?
4. Has a customer ever shown you a fake payment screen? What do you check now?
5. How should Paytm contact you: call, WhatsApp, Soundbox, app? Which language? What time?
6. Would you talk to an AI on the Soundbox button? What would make you trust it? What would make you hang up?
7. Has anyone offered you a loan through Paytm? What did you want to know first?
8. One thing you wish Paytm support did differently.

**Findings table** (copy into Bible section 40 when done; nothing has been collected yet)

| # | Finding | Role (not name) | Changes which playbook or slide |
|---|---|---|---|
| | | | |

**Pitch line:** "We asked [N] shopkeepers in [area]. [Finding]. That's why Munim [feature]." Fill it only from real findings. Slide 2 has a marked slot, `[MERCHANT QUOTE: pending the shopkeeper interviews]`, in `deck/slides.yaml`.

## 154. Request to the organisers (venue game)

The text is ready in the Bible ("Organiser request"). **It has not been sent, and I cannot send it for you.** Fill in the organiser's name, your team name and your phone number, send it, then record the answer here and in the Bible (B5).

- Sent on: ______  to: ______
- Answer: ______ (allowed / judges only / not allowed) on ______
- If not allowed: the arena is judges-only at your table; nothing else changes. The arena already has a kill switch, attempt caps and a spend cap (`tests/test_judge_arena.py`, `tests/test_venue_soak.py`).

## 157. Demo video (2:30)

**Rules from the Bible:** record an *unscripted* break-it session with a tester who has not seen the prompts. 1080p, clip-on mic, English captions for all Hindi and Kannada, a "SIMULATED DATA" watermark, no music under speech, two full takes. Record both 2A (Kannada voice) and 2B (Hindi voice) after the Sarvam spike and use the one the switch rule selects.

| # | Time | Shot | What in the product shows it | Ready? |
|---|---|---|---|---|
| 1 | 0:00 to 0:10 | Prop announces a payment, title card, cases board | prop (`prop/`), cases board `#/cases` | prop needs hardware |
| 2A | 0:10 to 0:35 | Kannada voice: Soundbox fault, checklist, replacement, device tile, Undo visible | phone `#/phone/<token>` or `#/simple/<token>`, systems panel | needs a live Sarvam key and a real microphone |
| 2B | 0:10 to 0:35 | Hindi-voice version if Kannada speech quality is low | same | same |
| 3 | 0:35 to 0:50 | Loan question: tools absent, `FIX-FIRST-001`, parked case | case view with the "Kyun nahi?" chip | yes |
| 4 | 0:50 to 1:00 | Scam Shield: fake screenshot gives "No payment received" | `tests/test_scam_shield.py` flow; print the fake screenshots | print needed |
| 5 | 1:00 to 1:10 | Risk hold: handoff packet, server-built approval view, risk reviewer | `#/handoffs`, approval dialog | yes |
| 6 | 1:10 to 1:50 | Unscripted break-it on a phone; guard-off twin on one attempt | `#/judge`, the twin banner | yes |
| 7 | 1:50 to 2:05 | Chaos: timeout after success gives one ticket; `verify_chain` passes | `#/control`, `python -m core.cli verify-ledger` | yes |
| 8 | 2:05 to 2:20 | Metrics with intervals, cost range, coverage matrix | `#/coverage`, `python -m core.cli eval --split dev` | **numbers wait for the held-out run** |
| 9 | 2:20 to 2:30 | Pi slide, tagline | deck slide 8 | after the deck |

Do not stage the tester: if the session is scripted it is not the "unscripted" session the video claims to be.

## 158. Rehearsal (under 5 minutes)

Run the whole live demo with a stopwatch and exercise every fallback. Tick when done and write the time.

| Run | Path | Pass when |
|---|---|---|
| 1 | Full demo with a volunteer judge | under 5:00 |
| 2 | **No volunteer:** ask the panel "tell me what to try", or draw three break-it cards at random in front of them | still under 5:00 |
| 3 | **Slow judge:** cap at 90 seconds, offer two one-tap prompts, then the guided tour (`#/judge` tour) | under 5:00 |
| 4 | **Network failure:** unplug, switch to the hotspot, then to the fallback model, text only, then the recording | demo continues |
| 5 | **Kill switch mid-demo:** pull it and show every action refused (`#/control`) | clear to the audience |

Say the simulation caveat out loud before anyone asks. Judge Mode is the highlight, never the only proof.

## 159. Pre-flight (the morning of the event)

Automated (run `python scripts/preflight.py`; it prints PASS, FAIL or MANUAL for each): keys present, dashboard built, frozen sets verified, a held-out run exists for the current code, ledger verifies, kill switch works, recordings on disk, deck figures all verified.

Manual (tick by hand): credits topped up; phone hotspot working; Soundbox prop charged; fake screenshots printed; persona cards printed; break-it card printed; QR code printed and tested with the real token; backups of the recordings on a second device.

## 160. Submission

**I cannot submit this for you.** The format is still open (Bible B4: judging criteria, timings, submission format come from the finalist email). When the email arrives: confirm what is wanted (prototype, video, presentation), build them in that format, submit, and record the confirmation here.

- Finalist email read on: ______  format: ______
- Prototype: ______  Video: ______  Presentation: ______ (build with `python -m core.eval.deck`, which refuses until every number has a source)
- Submitted on: ______  confirmation: ______
