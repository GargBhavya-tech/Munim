/** Plain meanings for the words and codes the screens show. Written for a judge or a shopkeeper who has never seen this system.
 *  A code that is not listed here is shown as it is: nothing is guessed. Keys are upper-case with underscores (see ``key()``). */

export const key = (term: string): string => term.trim().toUpperCase().replace(/[\s-]+/g, '_')

export const GLOSSARY: Record<string, string> = {
  // money and safety
  HELD_RISK: 'Held for a safety check. The money is paused while a risk reviewer looks at it. Munim cannot release it; only a risk reviewer can.',
  RISK_REVIEW: 'A person on the risk team looks at this payment before it is released.',
  SETTLEMENT: 'The payout of the money a shop collected, from Paytm to the shop’s bank account.',
  SETTLED: 'The money has reached the shop’s bank account.',
  SCHEDULED: 'The payout is planned for a set time and has not been sent yet.',
  PENDING: 'Waiting for something to finish. Nothing has failed.',
  KYC: 'Know Your Customer: the check that confirms who the shopkeeper is, using their documents.',
  IFSC: 'The code that identifies a bank branch.',
  DOC_BLURRY: 'The photo of the document was too blurry to read.',
  TIMEOUT: 'The other system did not answer in time. Nothing was lost; it can be tried again.',
  SOUNDBOX: 'The small speaker on the counter that says out loud when a payment arrives.',
  HEARTBEAT: 'The device’s last check-in with Paytm. A recent one means it is switched on and connected.',
  WAIVER: 'A fee that is cancelled for a period of time.',
  // finding new merchants
  PRIORITY_SCORE: 'How worth reaching a lead is: the expected monthly volume, times the chance they switch, times the chance of a later loan, divided by what it costs to reach them. Higher means sooner. The rates are assumptions on synthetic data.',
  DO_NOT_DISTURB: 'The national list of numbers that must not be contacted. A lead on it is never contacted, whatever else is true.',
  CONSENT: 'Permission, on record, to contact this person on a given channel. Without it nothing is sent.',
  SAVE_ATTEMPT: 'One try to keep a merchant who is leaving: a rep visit or an offer inside the approved limit. There is only ever one; after it, Munim lets them go.',
  CHANCE_OF_WORKING: 'How likely this fix is to clear the fault. These are assumptions on synthetic data, lowered when a device is clearly broken or the fault keeps coming back.',
  // how Munim works
  LEDGER: 'The permanent record. Every decision, action and sentence is written here, in order, and cannot be edited without it showing.',
  HASH_CHAIN: 'Each ledger entry carries a fingerprint of the one before it. Change any entry and every fingerprint after it stops matching.',
  HASH: 'A fingerprint of an entry. The same content always gives the same fingerprint.',
  IDEMPOTENCY_KEY: 'A fingerprint of an action. If the same action is sent twice, it is recognised and done only once.',
  KEY: 'A fingerprint of the action. If the same action is sent twice, it is recognised and done only once.',
  PLAYBOOK: 'The step-by-step recipe for one kind of case. Munim can only do what the current step allows.',
  LANE: 'How urgent a case is. P0 is the most urgent.',
  CRM: 'The customer record: the cases open for this shop and the language they use.',
  HANDOFF: 'A packet that gives a person everything they need to take a case over, so they do not start from zero.',
  ESCALATED: 'A person has taken over this case. Munim stops acting on it.',
  ACTIVE: 'Munim is working on this case now.',
  WAITING: 'Waiting for the merchant or for a timer.',
  RESOLVED: 'The case is finished.',
  // the gate and the guard
  GROUNDING_GATE: 'The checkpoint every sentence passes before a merchant sees it. Each fact in it must come from a system record.',
  ENTAILMENT: 'A check that the source really says the claim, not just something close to it.',
  SUPPORTED: 'A system record backs this claim, so it may be said.',
  UNSUPPORTED: 'Nothing in the systems backs this claim, so it is never said.',
  CORROBORATED: 'More than one independent source backs this claim.',
  FIXED_PHRASE: 'A pre-approved sentence that code sends word for word. The model cannot change it.',
  FIX_FIRST: 'While a service problem is open, Munim does not offer or sell anything.',
  UNKNOWN_ACTION: 'The model asked for an action that does not exist, so it was dropped.',
  MODEL_WRITTEN_TEXT: 'The model tried to write the message words itself. Only the checked writer may do that, so this was refused.',
  WRONG_SUBJECT: 'The action named a different merchant than the one this case is about. It was refused and a person was called in.',
  INVALID_ARGS: 'The action’s details were missing or malformed, so it was not run.',
  NOT_ALLOWLISTED: 'That action is not allowed at this step of the playbook.',
  ARG_FROM_MERCHANT: 'A number the merchant typed was about to be used as an action detail without any system backing it.',
  // judge mode
  FORBIDDEN_EXECUTED: 'Forbidden actions (such as approving KYC or changing bank details) that actually ran. This must always be 0.',
  GUARD_OFF_TWIN: 'A copy of Munim with every safety rule removed, answering the same message, to show what the rules prevent.',
  KILL_SWITCH: 'One switch that stops every write at once.',
  SIMULATED: 'All data here is synthetic. No real merchant, money or Paytm system is involved.',
}

export const explain = (term: string): string | undefined => GLOSSARY[key(term)]

/** Plain names for the playbook ids and codes the engine uses. Unknown ids are shown as they are. */
export const PLAYBOOK_NAMES: Record<string, string> = {
  C1_SETTLEMENT_DELAY: 'Settlement delay',
  C2_SOUNDBOX_FAULT: 'Soundbox fault',
  C3_PAYMENT_FAILED: 'Payment failed',
  C4_MERCHANT_REFUND: 'Merchant refund',
  C5_DETAIL_UPDATE: 'Detail update',
  C6_CHARGE_DISPUTE: 'Charge dispute',
  C7_RETURN_CLOSURE: 'Return or closure',
  C8_HOWTO: 'How-to question',
  C9_COMPLAINT: 'Complaint',
  C10_SCAM_SHIELD: 'Scam shield',
  S1_ACTIVATION: 'Activation',
  S2_SOUNDBOX_UPSELL: 'Soundbox upsell',
  S3_LOAN: 'Loan',
  S4_PLAN_CHANGE: 'Plan change',
  S5_WINBACK: 'Win-back',
  X0_UNSUPPORTED: 'Unsupported request',
  G0_GREETING: 'Greeting',
}

export const LANGUAGE_NAMES: Record<string, string> = { 'en-IN': 'English', 'hi-IN': 'Hindi', 'kn-IN': 'Kannada', hinglish: 'Hinglish' }
