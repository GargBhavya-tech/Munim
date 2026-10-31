# Sales: what needs to be built (and where it stands)

This lists WHAT the sales story promises, one capability per line, and whether it exists today. It says nothing about how to build anything. The same story is explained, with the real data behind each step, on the **Sales** screen of the dashboard (`#/sales`).

How the status was read (2026-10-31): from the code, the playbooks and the tests in the repository.

- **Built**: it exists and has its own tests, written before the last build.
- **Written, not run**: written on 2026-10-31 together with its tests, and **none of it has been run**: no test, no type check, no browser. Treat it as unverified until the tests pass.
- **Partly** / **Not built**: stated with the gap.

## A. Is the dip real?

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 1 | Compare a merchant's payments with their own normal: the same weekdays, with festivals and month-ends allowed for | Built | None |
| 2 | Compare them with their neighbours; if the whole area dipped, say nothing is wrong with them and stay quiet | Built | None |
| 3 | Wait to see whether the dip lasts before doing anything | Built | None. It needs three completed days below the threshold |

## B. Why is it happening?

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 4 | Tell the cause apart: a device offline is a service case, failing settlements go to support | Built | None |
| 5 | Spot a competitor switch: fewer customers plus a rival's QR seen in a street photo from a field rep | Written, not run | A rep's photo reading (fields only, never a verdict) is confirmed by code when a QR is visible, the network is a known rival and the reader is 70% sure or more; an unsure one waits for a sales lead. A confirmed one writes the fact the dip diagnosis already uses. The vision reader itself is injected and not wired to a live model, and the rival networks are synthetic placeholders |

## C. The fight

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 6 | Rank merchants at risk by money at risk times the chance of losing them, on the same list as new leads | Written, not run | Both are "paise of monthly volume won or kept per paise of cost", so they sort together. The chances of leaving and of a save working are assumptions, returned with the list. The at-risk side comes from the health job run on a throwaway copy, so looking at the list changes nothing |
| 7 | The save fits the person: a rep with a briefing, or an offer within approved limits, with a human signing off anything bigger | Written, not run | A rep when the volume is worth a day, otherwise an offer; inside the approved band no one signs, one month above it needs a sales lead; the offer goes through the executor and policy can still refuse it |
| 8 | Contact the merchant in their quietest hour, in their language | Built | None |
| 9 | One save attempt; if they still want to leave, let them go | Written, not run | One attempt per merchant, enforced by a UNIQUE column in the database; letting them go leaves a fact and no second attempt is ever planned |

## D. New merchants: starting at home

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 10 | Start from Paytm's own data | Built | None |
| 11 | Only after that, official registries and permitted public sources | Built | None |
| 12 | Give every lead a value and rank by it | Built | The rates are assumptions on synthetic data, and a weekly retrain is not scheduled |
| 13 | Choose the approach by value and by how little we know | Written, not run | Value sets the level; a lead whose estimate rests on one signal moves one step toward a person; an online brand tops out at a human call, a bank is always a human |
| 14 | A high-value shop gets a rep visit already planned on a route | Built | The routing engine's own tests were last recorded as not re-run |
| 15 | The style changes with the person: a 20-second voice note for a shopkeeper, a data-led email for a D2C founder, a human for a bank | Written, not run | A voice-note script (one plain idea from the brief, under 45 words, no trade jargon, from approved phrases), a data-led email from the brief's sourced facts, a human for a bank. No audio is made unless a speaker is connected, a non-English idea sentence needs a translator and the faithfulness check, and the lead's language is never guessed |
| 16 | Keep testing which approach works for each segment, judged by real conversations and not just replies | Built | The reward is a qualified conversation or a conversion, never a reply. Whether outreach calls it end to end was not checked |

## E. When a rep goes

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 17 | Match reps to leads by language and by their own win rate in that segment | Built | None |
| 18 | The briefing fits the rep: a new rep gets every step, a veteran gets three lines | Built | None |
| 19 | Part of a rep's incentive follows merchant activity over the first months | Written, not run | A statement per rep: a verified, converted visit counts only when the lead is won and its first settlement reached the bank; then 30% is earned, and the rest month by month in step with what the merchant processes. It pays nothing, and the pool and shares are assumptions |
| 20 | A sale is won when the first settlement reaches the bank, not at "yes" | Built | None |

## Count

20 capabilities: **13 built**, **7 written but not run**, **0 partly**, **0 not built**.

Still open: everything marked "Written, not run" is unverified. Nothing connects a live vision model to the sighting reader, nothing makes audio for a voice note, and nothing schedules the health job.
