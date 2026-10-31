# Customer service: what needs to be built (and where it stands)

This lists WHAT the customer-service story promises, one capability per line, and whether it exists today. It says nothing about how to build anything.

How the status was read (2026-10-31): from the code, the playbooks and the tests in the repository.

- **Built**: it exists and has its own tests, written before this session's last build.
- **Written, not run**: written on 2026-10-31 together with its tests, and **none of it has been run**: no test, no type check, no browser. Treat it as unverified until the tests pass.
- **Partly**: some of the capability exists; the gap is stated.
- **Not built**: nothing meaningful exists.

## A. Choosing the fix (the three rungs)

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 1 | Rung one: fix the fault remotely or in chat (restart, reset volume, push settings with a way back, check the connection), then confirm | Built | None |
| 2 | Rung two: an on-call human helper who guides the merchant by voice or video | Written, not run | A roster of on-call helpers, chosen by language, who is on call now, voice or video, and the lighter day; the call is booked through the executor. The call itself is a person's: there is no live voice or video session |
| 3 | Rung three: a field visit, from a fault to a finished visit | Written, not run | The plan names the technician (skill, a unit in the van, shift, durable fix record) and books the visit in an open shop through the executor. Visit logging and verifying already existed |
| 4 | Pick the rung by weighing what each fix costs, how likely it is to work, and what the merchant loses for every hour of waiting | Written, not run | Costs, chances and hours are assumptions on synthetic data, shown with every plan; the waiting loss is a share of the merchant's day, over their open hours |
| 5 | A repeat fault, or a clearly broken device, skips straight up the ladder | Written, not run | It falls out of the arithmetic: a repeat or broken device makes the cheap rungs unlikely to work. The plan says which fact caused the skip |
| 6 | A simple swap needs no technician: a courier, or any certified person nearby, can do it | Written, not run | Couriers (synthetic) and certified people with a unit, cheapest in reach first. A repeat fault is never treated as a simple swap |

## B. Knowing everyone

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 7 | A live profile of every merchant: language, shop hours, device history | Written, not run | Shop hours are reported or assumed, and say which. Nothing asks a merchant for them yet: they are entered through the screen or the API |
| 8 | A live profile of every field person: languages, certifications, parts in the van, where they are right now | Partly | Languages, skills, certifications, parts, shift and base exist, and the latest recent position is shown. An API route to report a position now exists (stored only inside the shift, on an active route), but there is no phone client that calls it |
| 9 | Traffic-aware travel times | Built | The real map providers are tested only against pretend services. A labelled SIMULATED provider (slower in the morning and evening peaks) was written for the demo, so the demo plans routes without a map key |
| 10 | Score people on how long their fixes last, adjusted for how hard the job was | Built | None |
| 11 | Plan the whole day for everyone at once | Written, not run | Every visit of the day is assigned by the technician rules, then routed per person inside shop hours, from a live position if known. The routing engine's own tests were last recorded as not re-run |
| 12 | Reshuffle in seconds when a shop is closed or a visit runs long | Written, not run | A person reports "shop is closed" or "running 30 minutes late" on the screen and the day is re-planned in the same call. Nothing detects either by itself |

## C. One incident, not thirty tickets

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 13 | Notice an area outage: ten or more devices in one pincode going silent within ten minutes, or many settlements failing on one bank | Built | None |
| 14 | Treat it as one incident: order no replacements, and send each affected merchant exactly one message | Built | The message wording needs a native-speaker review |
| 15 | A merchant who keeps coming back: stop repeating the fix and ask why. Device cause: replace it. Repeated how-to: send a short video. Otherwise: a senior owner | Written, not run | The short video is now a job: an approved video, a fixed message, the executor, consent. The video links are simulated and open nothing, and the hi, hinglish and kn wording needs a native review |
| 16 | Act before the merchant calls: a weakening device or a sudden usage drop triggers a check by itself | Written, not run | A weakening device now opens a watch case that sends one heads-up and never repairs. Nothing runs the health job on a schedule: it has to be started |

## D. Closing the loop

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 17 | "It's working now" counts only when the device heartbeat and a test announcement confirm it | Built | None |
| 18 | A scam check that builds a sandbox and puts the merchant's demand into it, to tell a scam from a real person with a real problem | Written, not run | A throwaway in-memory copy of the merchant's recent payments, the claim placed in it, signals read, the copy discarded, a verdict by code. It is a screen and an API; Scam Shield's own flow does not call it yet |

## E. Trust

| # | What has to exist | Status | The gap |
|---|---|---|---|
| 19 | The AI never writes to the merchant directly; every claim carries a source; a sentence with no source is never said | Built | None |
| 20 | A supervisor can tap any sentence and see its proof | Built | In the chat, ledger and "why not" views. Not confirmed on the handoff packet a supervisor opens |

## Count

20 capabilities: **8 built**, **11 written but not run**, **1 partly**, **0 not built**.

Still open after the build: #8 (nothing reports a person's position), and the unverified state of everything marked "Written, not run".
