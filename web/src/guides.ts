/** What each screen is for, what to try on it, and what a good result looks like. One short line each, in plain words. Keyed by route id (App.tsx). */

export interface Guide {
  see: string
  try: string
  good: string
}

export const GUIDES: Record<string, Guide> = {
  floor: {
    see: 'Every case Munim is handling, as a row, grouped by state. A new case turns in as it opens.',
    try: 'Open Judge Mode, send a message as a merchant, then come back and watch the case appear.',
    good: 'Cases move toward Resolved. A case marked as held by a person is waiting for a human, not stuck.',
  },
  leads: {
    see: 'Businesses that are not Paytm merchants yet: how many were found, how many were dropped and why, and the rest ranked best first, with a map and a brief for each.',
    try: 'Click a lead on the map or in the list and read its brief. Open “Left out of the list” to see who was dropped.',
    good: 'No existing merchant is ever in the ranked list, every number in a brief names its source, and a lead with no consent is marked “not yet”.',
  },
  ops: {
    see: 'How a Soundbox fault is fixed: which rung of the ladder to start on and why, who does it, how the field day is planned, and a sandbox that checks a payment claim.',
    try: 'Plan the fix for a merchant, report a shop as closed, mark a person as running late, or check a payment claim.',
    good: 'A simple fault starts with a remote fix; a broken or repeat one skips up, and every number shows the assumption behind it.',
  },
  judge: {
    see: 'A shopkeeper’s counter on the left, the guard-off twin on the right, and a scoreboard counted from the ledger.',
    try: 'Pick a merchant and type as they would, or use a card under “Try to break it”, for example sharing an OTP.',
    good: 'Munim refuses or hands over, and “Forbidden executed” stays at 0.',
  },
  phone: {
    see: 'The merchant’s own view: chat and voice on a phone.',
    try: 'Type a question or hold the mic. Then look at what backs each reply.',
    good: 'Every fact in a reply traces to a system record. A reply Munim cannot back becomes a hand-over to a person.',
  },
  systems: {
    see: 'What each mock Paytm system holds for one merchant, in plain words, with the raw data one click away.',
    try: 'Change the merchant, or send a message in Judge Mode and watch a card update.',
    good: 'A card that just changed lights up and names the action that changed it.',
  },
  handoffs: {
    see: 'Cases a person has to carry, each with a packet of what was said, done and left.',
    try: 'Open a handoff and read its packet before acting on it.',
    good: 'Every return or approval shows up in the ledger with who did it.',
  },
  coverage: {
    see: 'How much of each kind of case Munim can be trusted with, from recorded results.',
    try: 'Find a playbook that is not supported and read the reason it gives.',
    good: 'Numbers come from recorded runs. “Not measured yet” is an honest answer, not an error.',
  },
  ladder: {
    see: 'How far each playbook is trusted, one level at a time.',
    try: 'Look for a playbook that dropped a level, and read why.',
    good: 'Levels go up one at a time and drop straight down when a floor breaks.',
  },
  arena: {
    see: 'Break Munim: a game where players try to make it do something it must never do.',
    try: 'Pick a level and try. The level clears only if the ledger shows it happened.',
    good: 'Nobody clears a level that needs a forbidden action to run.',
  },
  control: {
    see: 'Operator controls for the simulated world: the clock, failure switches, the kill switch and policy rules.',
    try: 'Switch the kill switch on and try an action. It should be refused.',
    good: 'Every control you use is written to the ledger, and the server can say no.',
  },
  proof: {
    see: 'The ledger: every decision, action and sentence, chained so any edit shows.',
    try: 'Press Verify chain and watch it check every entry.',
    good: 'The chain reports intact, with the number of entries it checked.',
  },
  design: {
    see: 'The building blocks every screen is made from.',
    try: 'Press Verify chain on the sample ledger.',
    good: 'The same stamp, counter and ledger row look the same everywhere.',
  },
}
