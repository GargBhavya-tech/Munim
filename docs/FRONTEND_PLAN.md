# Frontend plan: the judge-facing Munim (written 2026-10-31, finals day)

**Status: PLAN ONLY. Nothing is built.** The existing dashboard (`dashboard/`, port 5173) is left exactly as it is and stays the fallback. The new frontend is a separate app on its own port that talks to the same backend.

Sources: the portfolio project (`C:\Master_Brain\Projects\portfolio`: its `DESIGN.md`, tokens and `fx.tsx` / `client.tsx` / `ui.tsx` components), the Bible demo script (section 46), and the design, motion and anti-slop skills.

## 1. What this is for, and the constraints

- **Job:** in five minutes a judge must see that Munim finishes cases, refuses to break its rules, and proves every sentence. The screens exist to make those three things obvious and memorable, not to administer a system.
- **Audience:** Paytm and hackathon judges on a laptop or projector, plus one judge's phone (QR). A shopkeeper's phone is the product's real user, so the merchant side must look like a phone.
- **Hard constraints:** the same backend API (`docs/API_CONTRACT.md`), no change to `core/`, everything labelled SIMULATED, nothing invented (every number comes from the ledger or a read), works offline and on a hotspot (no CDN fonts), reduced motion respected.
- **Today's reality:** finals are today. The plan is tiered so a useful, polished slice ships first and the rest is optional. The old dashboard remains the fallback if anything is not ready.

## 2. Design direction

**Concept: "The Ledger"** (bahi-khata). A *munim* is the traditional shop accountant who keeps the books. The whole interface is a ledger that writes itself: every case, action and sentence becomes an entry; every claim gets **stamped** with its source; the hash chain is a bound book you can verify. It is specific to the product's name, to Paytm's merchants, and to the pitch ("every word, proven"), so it does not read as a generic AI dashboard.

**Two surfaces, one idea** (this is the reason for dark and light, not a trend):
- **The Counter (paper, light):** the merchant's side: the phone chat, the simple client, anything a shopkeeper sees. Daylight, ruled paper, ink.
- **The Back Office (night, dark):** the stage view for judges: case floor, Judge Mode scoreboard, proof ledger, metrics. High contrast for a projector and for the recorded video.
- The **guard-off twin** uses the contrast on purpose: the real Munim on paper, the twin on a red-edged "GUARDS OFF - SIMULATED TWIN" night panel.

**The memorable detail: the stamp.** When a sentence is proven, a small ink stamp lands on it (source id, rule id or "verified"). A refused action gets a vermilion REFUSED stamp with its rule id. One idea, used everywhere, and it is the pitch made visible.

**Identity line:** *Every merchant case, finished. Every word, proven.*

### Tokens (adapted from the portfolio's `globals.css`; same discipline: tokens on `:root`, a theme switch, one accent)

| Token | Night (Back Office) | Paper (Counter) | Use |
|---|---|---|---|
| `bg` | `#0B0D12` | `#F1ECDF` | ground |
| `surface` | `#12161D` | `#FAF7EE` | panels |
| `line` | `#232A35` | `#D6CFBC` | hairlines (no card shadows) |
| `fg` | `#ECE8DD` | `#141821` | text |
| `muted` | `#8F98A5` | `#565B66` | secondary text (AA) |
| `ink` | `#7FA8FF` | `#1F3A93` | data, links, hash text |
| `stamp` | `#FF5A36` | `#C2381C` | THE accent: stamps, refusals, the one primary action |
| `ok` / `warn` / `bad` | reserved status colours, always with an icon and a label, never reused for series |

The palette is deliberately its own (indigo ink and vermilion stamp on warm paper), not Paytm's brand blue and not the portfolio's cyan and orange, so nothing imitates a real organisation and the app does not look like the portfolio.

### Type
- **Archivo** (variable width) for UI and headlines; **IBM Plex Mono** for measured values, ids and hashes (the portfolio's "readout" idea); **Instrument Serif** italic for **Munim's own voice** (the machine speaks in sans and mono; the assistant speaks in serif).
- **Hindi and Kannada are first-class:** Noto Sans / Noto Serif Devanagari and Kannada, self-hosted (`@fontsource`), so Indic text renders crisply and offline. Merchant and reply text always shows in its own script with an English caption underneath (the same "English back-translation" the gate already produces).
- Fluid scale, `text-wrap: balance` on titles, `tabular-nums` on every counter. Minimum 18px body on stage screens (projector).

## 3. Motion system (from the motion-ui skill and the portfolio's tokens)

Rule: **motion must guide attention, communicate state or keep spatial continuity, or it is removed.** One import source (`motion/react`).

| Token | Value | Use |
|---|---|---|
| `ease-out` | cubic-bezier(0.16, 1, 0.3, 1) | everything entering |
| `ease-in-out` | cubic-bezier(0.65, 0, 0.35, 1) | page and view changes |
| fast / base / slow / scene | 140 / 280 / 600 / 1200 ms | hover, small reveal, section reveal, hero and verify sweep |

Signature motifs (each runs **once**, on a state change, never as an idle loop):
1. **Stamp:** scale 1.15 to 1, a 1 degree rotation settle, ink opacity up. Lands on a claim when it is verified, or on an action when it is refused.
2. **Ledger turn:** a new ledger or case entry slides in from below with a ruled line drawing under it; a hash link draws from the previous entry (transform and opacity only).
3. **Verify sweep:** the portfolio's scan-sweep, re-themed as an ink line passing down the chain once when "Verify chain" runs; each row ticks green as it passes.
4. **Counter:** scoreboard numbers tween to the new value (tabular numerals, no layout shift).
5. **Voice:** a waveform that moves **only while Munim is speaking or the mic is open** (state, not decoration).

Rules: stagger at most 0.08 s; `AnimatePresence mode` always explicit; only `transform` and `opacity`; no `transition: all`; `layout` only on small elements; `useReducedMotion` plus the CSS media query turns every motif into an instant state change; a low-end device check halves durations. **Smooth scroll (Lenis) only on the opening story screen**, never on tool screens (it makes data screens feel laggy). Press feedback `scale(.97)`, 40px minimum hit areas, concentric radii, images with neutral outlines.

## 4. Screens, in the order a judge sees them

Priority: **P0** must ship, **P1** should, **P2** if time, **P3** stretch. "Old" = the screen exists in `dashboard/` today.

| # | Screen | What a judge sees | Key components and motion | Backend | Tier |
|---|---|---|---|---|---|
| 0 | **Shell and opening** | Dark stage, SIMULATED stamp, sim clock, connection light, kill-switch state. First 8 seconds: the Soundbox line "₹240 प्राप्त हुए" as a waveform, a ledger line writes itself, then the Case Floor | Preloader (ported), word reveal, stamp | `/api/control`, `/ws` | P0 |
| 1 | **Case Floor** (old: Cases) | Cases as ledger rows grouped by state, lane chip, language tag, "a person has this" marker; new entries turn in live | ledger turn, filter chips | `/api/cases`, `/ws` | P0 |
| 2 | **Merchant phone** (old: Phone/Simple) | A real phone frame on paper: chat in the merchant's script with English caption, voice button with live waveform, replies in serif, **claim chips that stamp** and open a source pop-over, Undo, "Kyun nahi?" drawer on any refusal | stamp, waveform, drawer, bottom sheet | `/api/merchants/{mid}/messages`, `/ws/voice`, `/api/why/{seq}`, speak | P0 |
| 3 | **Judge Mode** (old: Judge) | Persona cards, the break-it deck (cards that flip into the chat), **scoreboard from the ledger** (attempts, blocked claims, refused actions, forbidden executed = 0), the guided tour with progress | counter tween, card flip, stamp on every refusal | `/api/judge/*`, `/api/ledger` | P0 |
| 4 | **Guard-off twin** | One input, two columns: real (paper) vs twin (red-edged night, "GUARDS OFF"). The twin promises 0%; the real one refuses with the rule id; actions requested that **do not exist** are listed | split view, stamp vs "would have done" | `/api/twin/*` | P0 |
| 5 | **Proof Ledger** (old: Audit) | The hash chain as a bound book; **Verify chain** runs the sweep; "tamper a copy" shows the first bad entry; per-case replay; Questions-to-the-ledger box | verify sweep, ledger turn | `/api/ledger`, `/verify`, `/ask`, replay | P0 |
| 6 | **Handoff and approval** (old: Handoffs) | A risk hold becomes a packet: everything the person needs, server-built approval view, role chip | packet unfold, stamp | `/api/handoffs`, `/approvals` | P1 |
| 7 | **Systems panel** (old: Systems) | The mock systems as tiles that flash when an action changes them (device online, settlement released); Undo window countdown | tile flash (once), countdown | `/api/systems` | P1 |
| 8 | **Break Munim arena** (old: Arena) | Public level list, leaderboard, per-player attempts, budget bar, kill switch for the operator | counter, leaderboard re-sort (small layout) | `/api/arena/*` | P1 |
| 9 | **Metrics** (old: Coverage, Ladder) | Coverage matrix as a heatmap (single-hue sequential, validated), Tier A and B with **95% interval bars**, rollout ladder, cost range. Numbers only from the held-out run, else a visible "not measured yet" | interval chart, heatmap fade-in | `/api/coverage`, `/api/ladder` | P1 |
| 10 | **Presenter mode** | A small overlay for the demo driver: beat timeline from section 46, fallback buttons (switch to recording, fallback model, hotspot hint), big kill switch, latency chip | keyboard shortcuts, no motion | `/api/control` | P1 |
| 11 | **Simple client** (old: Simple) | Voice and icons only, rebuilt with large stamps and spoken replies | big icon buttons, waveform | as 2 | P2 |
| 12 | **Lead map and field day** (N1, N2) | Scored leads on a ward map, brief with sourced numbers, routes that re-plan when a shop closes | map, route draw | `/api/*` (partly missing) | P3 |

## 5. Architecture and reuse

- **New app:** `web/` beside `dashboard/`: Vite + React 19 + TypeScript + Tailwind 4 + `motion`, on **port 5174** (the old dashboard stays on 5173). Same proxy for `/api`, `/approvals`, `/ws`. Vite rather than Next.js because the backend already serves the API and there is nothing to server-render, the toolchain is identical to `dashboard/`, and the WebSocket proxy already works.
- **Reuse from the old dashboard (read-only, by path alias, no edits there):** `api/types.ts`, `api/client.ts`, the offline mock (`?mock=1`), `lib/wav.ts`, `lib/pushToTalk.ts`, `lib/spoken.ts`.
- **Port from the portfolio** (plain React plus `motion`; only `next/link` and `next/font` need replacing): the CSS token structure and `@theme`, theme toggle and `useReducedMotion`, `Reveal`, `ScanReveal`, `Magnetic`, `WordReveal`, `Marquee` (for the language strip), `Preloader`, `FloatingMenu`, the Lenis wrapper, the reticle cursor (optional, off on touch).
- **Do not port:** the three.js LiDAR city (heavy, off-brand here, risky on venue hardware). If a hero visual is wanted, use a small canvas or SVG ledger-line drawing.
- **Launch:** add `web-win` to `.claude/launch.json` (port 5174) beside the existing entries.
- **Offline:** fonts bundled with `@fontsource`, no external requests; a visible "RECORDED" band if the mock API is ever used so a judge is never shown canned data as live.

## 6. Honest-data and anti-slop rules (checked against the anti-slop UI skill)

No invented numbers, deltas, feeds or testimonials; no logo bar; empty, loading and error states say the cause and the next action; every number on the metrics screen comes from the held-out run or shows "not measured yet" (this matches `core/eval/deck.py`); no decorative glow, glass, emoji, endless pulses, left-stripe cards, bento mosaic, or purple gradients; accent used at key moments only; section compositions vary; every nav item has a real destination.

## 7. Verification plan (when tests are allowed again)

Vitest for components with the mock API; a browser pass over every screen against the live backend; keyboard-only run; reduced-motion run; contrast check on every token pair; the dataviz palette validator on the heatmap and interval colours; a 1080p projector legibility check and a 390px phone check; Lighthouse accessibility.

## 8. Phasing and cut line (for today)

| Phase | Scope | Rough time | Cut line |
|---|---|---|---|
| A | Foundation: tokens, fonts, shell, theme, stamp, ledger-entry, counter, waveform components; port the portfolio pieces | 1.5 h | **Ship nothing until A is solid.** |
| B | Screens 0 to 5 (P0): opening, Case Floor, merchant phone, Judge Mode, twin, Proof Ledger | 3 to 4 h | This is the minimum worth switching to. |
| C | Screens 6 to 10 (P1) | 2 to 3 h | If late, stop here and use the old screens for these. |
| D | Screen 11, then 12 | stretch | Only if asked. |

If B is not finished and verified before the event, the demo runs on the old dashboard.

## 9. Risks and open decisions

- **The live model key** is empty on this machine, so the model-dependent screens (3, 4, the phone chat) show hand-over lines until the key is added; the plan does not change that, but rehearsal needs it.
- **Time:** the P0 slice is a few hours of focused work; the full plan is more than a day. Scope is the main risk, hence the cut line.
- **Decisions needed:** (1) the concept (Ledger vs the portfolio's night-scan look), (2) the stack (Vite as planned vs Next.js like the portfolio), (3) how much to build today.

## 10. Decisions taken (2026-10-31)

- **Concept: The Ledger** (paper Counter and night Back Office, ink-blue data, one vermilion stamp accent).
- **Stack: Vite + React 19 + TypeScript + Tailwind 4 + motion**, new app in `web/` on port 5174; the old dashboard is untouched.
- **Scope today: Phase A (foundation) plus Phase B (screens 0 to 5):** opening, Case Floor, merchant phone, Judge Mode, guard-off twin, Proof Ledger. Everything else keeps using the old dashboard.
- **Phase A DONE (2026-10-31), not yet verified by tests (owner: no test runs today).** Phase B is built (section 12).

## 11. Phase A status (2026-10-31)

Built in `web/` (port 5174, `web-win` in `.claude/launch.json`); the old dashboard is untouched and is reused read-only through the `@old` alias (API client, mock, `LiveProvider`, `useResource`).

- **Design tokens and type** (`src/styles.css`): Night and Paper surfaces, one stamp accent, fluid type scale, motion tokens, the ruled-line, stamp and verify-sweep styles, reduced-motion rules. Fonts bundled with `@fontsource`; Hindi and Kannada confirmed rendering in shipped faces.
- **Motion** (`src/motion.ts`): tokens, `useMotion` (reduced motion plus a low-end halving of durations).
- **Components** (`src/ui.tsx`): `Stamp`, `Counter`, `Readout`, `Waveform` (moves only while live), `LedgerEntry` with its chain rail, `Sweep`, `Button`, `Tag`, `Reveal`, `Magnetic`, `SurfaceBox`. `src/ledger.ts` turns a ledger entry into one honest line.
- **Shell** (`src/shell.tsx`): wordmark, nav, always-visible SIMULATED stamp, server sim clock, connection light, kill-switch state, role, page transition; an on-screen "recorded data, not live" stamp if the mock API is ever used.
- **Showcase** (`#/design`): every component, with the ledger and "Verify chain" running against the live backend.

Checked: `tsc --noEmit` clean, `vite build` passes, and a browser pass of `#/design` against the live backend (stamp, counters, waveform, Hindi and Kannada text, real ledger entries, chain verify "intact, 205 entries"). Two bugs found and fixed in that pass: an unlayered `button { color: inherit }` overrode Tailwind text colours (button text was low contrast on the accent), and stamps forced uppercase, which would have rewritten case-sensitive ids.

**Not done:** contrast was judged by eye, not measured; keyboard-only and reduced-motion runs not done; narrow phone widths only glanced at; no automated tests (none run today). Phase B (screens 0 to 5) is next.

## 12. Phase B status (2026-10-31)

Screens 0 to 5 are built in `web/src/screens/`: `Opening` (once per tab, skippable, off under reduced motion), `Floor` (cases grouped by state, open a case for what was said and done, Undo while its window is open), `Phone` (paper counter, mic, spoken replies, `jt_` token redeem), `Judge` (message, scoreboard, tour, twin split, break-it deck), `Proof` (ledger, Verify chain sweep, filters, "Ask the ledger", Kyun nahi? drawer). `src/chat.tsx` holds the shared conversation hook and proof stamps; stamps come only from render sentences and ledger rows, never invented.

For the demo, `npm run build && npm run preview` serves the built app on 5174 with the same proxy, so nothing recompiles and no stale CSS can appear mid-demo.

Checked: `tsc --noEmit` clean, `vite build` passes. Browser pass against the live backend: Judge (hand-over reply with stamps, scoreboard, tour, twin split), Proof (verify "chain intact, 241 entries"), Phone (persona session loads), Floor (cases listed by state).

**Not verified:** opening a case on the Floor (Undo untested), voice, Kyun nahi? with a real refusal (needs the model, Sarvam key is empty here), break-it deck sends, the `jt_` flow, narrow widths, measured contrast, keyboard and reduced-motion passes. No automated tests (none run today).

## 13. Parity with the original dashboard (2026-10-31)

Everything the original dashboard (port 5173) offered now exists in the new app (port 5174), in The Ledger design, with the same API calls and no backend change:

- **Systems** (all ten tiles, merchant picker, flash on change, "last change" cause), **Handoffs** (queue by deadline, packet, return to agent, needed or not needed, approvals with the full approval dialog and the server's refusal word for word), **Coverage** (with "Kyun nahi?"), **Ladder**, **Arena** (join, attack, leaderboard, levels, budget), **Control** (kill switch with confirm, sim clock +1h/+24h/+48h, chaos per system, customer pays, cost meter, **policy compiler** which the original never placed on a screen).
- **Case Floor** case detail now also has the English back-translation toggle, per-message play, merchant receipts, the gate's claims with evidence, and the full decision timeline. **Proof Ledger** now has case replay from the ledger alone and the evidence-pack link (it already had verify, kind and case filters, and "Kyun nahi?").
- **Simple client** at `#/simple/<token>`: no chrome, voice and large buttons only.
- Old links keep working: `#/cases`, `#/case/<id>` go to the Case Floor, `#/audit` to the Proof Ledger.

Checked: `tsc --noEmit` clean and `vite build` passes; browser pass against the live backend of Systems, Handoffs (3 open handoffs), Coverage, Ladder, Control, Arena, the Case Floor detail, and Simple with an invalid token (shows UNKNOWN_TOKEN honestly). **Not exercised:** Handoff return/verdict and approve/reject, kill switch, clock advance, chaos apply, customer pays, policy compile/approve, arena join and attempt, replay, Simple with a real token and voice. No automated tests were run (none run today). Coverage reads "0 of 17 supported" because `munim eval` has not been run this session.
