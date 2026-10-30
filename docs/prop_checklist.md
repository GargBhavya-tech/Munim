# Soundbox prop: manual checklist (#133)

Software side is tested with an injected transport (`tests/test_prop.py`). These checks need the real hardware and network. SIMULATED data only.

## Before the venue
- [ ] Speaker/phone or Pi/ESP32 + speaker powers on from battery and a power bank; volume is audible at 3 m.
- [ ] Prop shows or says "SIMULATED" on boot; it never announces real money.
- [ ] Prop clock/config uses the demo SimClock source, not the device wall clock.
- [ ] Hindi, Kannada and Hinglish announcement lines are played to a native speaker (flag unclear phrases, do not ship them).

## Venue-like Wi-Fi
- [ ] Join a captive-portal or client-isolated network: confirm the prop can still reach the API host (if not, use the hotspot).
- [ ] 5 GHz-only network: confirm the module supports it, else use 2.4 GHz.
- [ ] Simulate a payment: announcement is heard within 1 s of the event (stopwatch, 5 runs).
- [ ] Drop Wi-Fi for 20 s mid-demo, restore: prop reconnects by itself and does not repeat a payment already announced.
- [ ] Roam between two access points: no duplicate announcement, no silence over 30 s.

## Phone hotspot (fallback)
- [ ] Prop and API laptop both on the hotspot; repeat the payment, drop/restore and duplicate checks above.
- [ ] Hotspot data cap/auto-off disabled; battery saver off.

## Button and brief
- [ ] Hold button, speak, release: transcript reaches the read-back gate; nothing happens when no read-back is pending.
- [ ] Play the morning brief; after it, say nothing for the read-back timeout: confirm NO action in the ledger.
- [ ] Say "nahi": no action. Say "haan": exactly one action in the ledger.
- [ ] Brief contains only facts visible on the dashboard for that merchant (compare each sentence).
