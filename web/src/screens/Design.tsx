import { useState, type ReactNode } from 'react'
import { useLive, useResource } from '@old/hooks/live'
import type { VerifyResult } from '@old/api/types'
import { summarise, toneOf } from '../ledger'
import { useMotion } from '../motion'
import { Button, Counter, LedgerEntry, Magnetic, Readout, Reveal, Stamp, SurfaceBox, Sweep, Tag, Waveform } from '../ui'

/** The design system on one page: tokens, type, the stamp, the counter, the voice, the ledger and the verify sweep. Real ledger entries and a real chain
 *  verification come from the live backend; anything that is only an example is labelled SAMPLE. */

const SWATCHES = [['bg', 'ground'], ['surface', 'panels'], ['line', 'hairlines'], ['fg', 'text'], ['muted', 'secondary text'], ['ink', 'data and hashes'], ['stamp', 'the one accent']] as const

function Section({ index, title, note, children }: { index: string; title: string; note?: string; children: ReactNode }) {
  return (
    <Reveal>
      <section className="rule-t pt-8">
        <div className="text-label text-muted">{index}</div>
        <h2 className="text-h1 mt-1">{title}</h2>
        {note && <p className="text-body text-muted mt-3 max-w-[60ch]">{note}</p>}
        <div className="mt-8">{children}</div>
      </section>
    </Reveal>
  )
}

function Swatches({ surface }: { surface: 'night' | 'paper' }) {
  return (
    <SurfaceBox surface={surface} className="border border-line p-5">
      <div className="text-label text-muted mb-4">{surface === 'night' ? 'The Back Office (night)' : 'The Counter (paper)'}</div>
      <ul className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {SWATCHES.map(([token, use]) => (
          <li key={token}>
            <span className="block h-14 w-full border border-line" style={{ background: `var(--${token})` }} aria-hidden="true" />
            <span className="text-label mt-2 block">{token}</span>
            <span className="text-small text-muted">{use}</span>
          </li>
        ))}
      </ul>
    </SurfaceBox>
  )
}

export function Design() {
  const { api } = useLive()
  const m = useMotion()
  const [count, setCount] = useState(3)
  const [voice, setVoice] = useState(false)
  const [sweep, setSweep] = useState(0)
  const [stampKey, setStampKey] = useState(0)
  const [verdict, setVerdict] = useState<VerifyResult | null>(null)
  const [verifyErr, setVerifyErr] = useState<string | null>(null)
  const ledger = useResource(() => api.ledger(0, 500), [api])
  const rows = ledger.data?.entries.slice(-8) ?? []

  const verify = () => {
    setVerifyErr(null)
    setSweep((n) => n + 1)
    api.verify().then(setVerdict, (e: unknown) => setVerifyErr(e instanceof Error ? e.message : String(e)))
  }

  return (
    <div className="mx-auto max-w-6xl space-y-20 px-6 py-14">
      <header>
        <div className="text-label text-muted">The Ledger · design system</div>
        <h1 className="text-display mt-3">Every merchant case, finished.</h1>
        <p className="text-voice mt-4 text-ink">Every word, proven.</p>
        <p className="text-body text-muted mt-6 max-w-[62ch]">
          A <em>munim</em> keeps the books. This interface is a ledger that writes itself: every case, action and sentence becomes an entry, and every proven sentence is stamped with its source.
          Two surfaces, one idea: night for the people watching, paper for the shopkeeper.
        </p>
      </header>

      <Section index="01 · Surfaces" title="Night and paper" note="Put data-surface on any element and everything inside re-skins itself. The guard-off twin uses this contrast on purpose.">
        <div className="grid gap-6 md:grid-cols-2">
          <Swatches surface="night" />
          <Swatches surface="paper" />
        </div>
      </Section>

      <Section index="02 · Type" title="Machine in sans, Munim in serif" note="Archivo for the interface, IBM Plex Mono for measured values, Instrument Serif for Munim's own voice. Hindi and Kannada render in faces we ship, so they work offline.">
        <div className="space-y-6">
          <div><div className="text-label text-muted">display</div><div className="text-h1">Case finished</div></div>
          <div><div className="text-label text-muted">voice (serif)</div><div className="text-voice">I am passing this to a colleague who will help you.</div></div>
          <div className="grid gap-6 sm:grid-cols-2">
            <div><div className="text-label text-muted">hindi</div><div className="text-h2">नमस्ते, मेरा साउंडबॉक्स बंद है</div></div>
            <div><div className="text-label text-muted">kannada</div><div className="text-h2">ನಮಸ್ಕಾರ, ಧನ್ಯವಾದ</div></div>
          </div>
          <div><div className="text-label text-muted">readout</div><div className="text-num text-h1 text-ink">1,24,000</div></div>
        </div>
      </Section>

      <Section index="03 · The stamp" title="Proof you can see" note="A stamp lands once: on a sentence that is proven, or on an action that is refused. These stamps are SAMPLES of the wording the screens will use.">
        <div className="flex flex-wrap items-center gap-5" key={stampKey}>
          <Stamp tone="ok" delay={0}>verified · kb:fee_schedule</Stamp>
          <Stamp tone="ink" delay={0.12}>source · tool:get_device_status</Stamp>
          <Stamp delay={0.24}>refused · KYC-NO-CHAT-001</Stamp>
          <Stamp tone="bad" delay={0.36}>kill switch on</Stamp>
          <Button onClick={() => setStampKey((k) => k + 1)}>Stamp again</Button>
        </div>
      </Section>

      <Section index="04 · Numbers and voice" title="Counters and the waveform" note="Numbers tween to their new value and never shift width. The waveform moves only while a voice is live.">
        <div className="grid items-end gap-8 sm:grid-cols-3">
          <Readout label="Refused actions (sample)" value={count} tone="stamp" size="lg" />
          <Readout label="Not measured yet" value="no held-out run" />
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={() => setCount((c) => c + 1)}>+1</Button>
            <Button onClick={() => setCount((c) => Math.max(0, c - 1))}>−1</Button>
            <span className="text-small text-muted">now <Counter value={count} /></span>
          </div>
        </div>
        <div className="mt-8 flex flex-wrap items-center gap-5 text-ink">
          <Waveform live={voice} />
          <Magnetic><Button variant="primary" onClick={() => setVoice((v) => !v)}>{voice ? 'Stop voice' : 'Start voice'}</Button></Magnetic>
          <span className="text-small text-muted">{m.reduce ? 'Reduced motion is on: the waveform stays still.' : voice ? 'Live' : 'Idle: it does not move.'}</span>
        </div>
      </Section>

      <Section index="05 · The ledger" title="A book you can verify" note="These are the latest real entries from the running backend. Verify walks the whole chain once; an ink line passes down the rows.">
        <div className="mb-5 flex flex-wrap items-center gap-4">
          <Button variant="primary" onClick={verify}>Verify chain</Button>
          {verdict && (verdict.ok ? <Stamp tone="ok">chain intact · {verdict.entries} entries</Stamp> : <Stamp tone="bad">broken at entry {verdict.first_mismatch}</Stamp>)}
          {verifyErr && <span role="alert" className="text-bad text-small">Could not verify: {verifyErr}</span>}
          <Tag>live data</Tag>
        </div>
        <Sweep run={sweep} className="border border-line bg-surface">
          {ledger.error && <p role="alert" className="p-5 text-bad">The ledger could not be read: {ledger.error}. Is the backend running on port 8000?</p>}
          {!ledger.error && rows.length === 0 && <p className="p-5 text-muted">No entries yet. Send a message in Judge Mode and the first entry will turn in here.</p>}
          <ol>
            {rows.map((e, i) => (
              <LedgerEntry key={e.seq} seq={e.seq} kind={e.kind} actor={e.actor} summary={summarise(e)} hash={e.hash} first={i === 0} tone={toneOf(e)} />
            ))}
          </ol>
        </Sweep>
      </Section>

      <Section index="06 · The Counter" title="What the shopkeeper sees" note="Paper, ruled lines, Munim's voice in serif, and a stamp on the sentence that is proven. This conversation is a SAMPLE of the layout.">
        <SurfaceBox surface="paper" className="mx-auto max-w-sm rounded-[36px] border border-line p-6">
          <div className="text-label text-muted">Sample · phone</div>
          <div className="ruled mt-4 space-y-5 p-1">
            <p className="ml-auto max-w-[85%] bg-surface px-4 py-3 text-right">नमस्ते, मेरा साउंडबॉक्स बंद है</p>
            <div className="max-w-[92%]">
              <p className="text-voice">I am passing this to a colleague who will help you.</p>
              <div className="mt-2"><Stamp tone="muted">fixed phrase</Stamp></div>
            </div>
          </div>
        </SurfaceBox>
      </Section>
    </div>
  )
}
