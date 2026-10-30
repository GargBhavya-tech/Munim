import type { WsMessage } from '../api/types'

export type ConnStatus = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'mock'

export const backoffMs = (attempt: number, base = 500, cap = 15000): number => Math.min(cap, base * 2 ** attempt)

export interface SocketLike {
  onopen: ((ev: unknown) => void) | null
  onclose: ((ev: unknown) => void) | null
  onerror: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  send(data: string): void
  close(): void
}

export interface ReconnectOptions {
  url: string
  factory: (url: string) => SocketLike
  onMessage: (m: WsMessage) => void
  onStatus: (s: ConnStatus, attempt: number) => void
  setTimer?: (fn: () => void, ms: number) => unknown
  clearTimer?: (h: unknown) => void
}

/** WebSocket that reconnects with exponential backoff; the attempt counter resets once a connection opens. */
export class ReconnectingSocket {
  private sock: SocketLike | null = null
  private attempt = 0
  private timer: unknown = null
  private stopped = false
  private sub: string | null = null
  private isOpen = false
  private o: ReconnectOptions
  constructor(o: ReconnectOptions) {
    this.o = o
  }

  start() {
    this.stopped = false
    this.connect()
  }

  subscribe(caseId: string | null) {
    this.sub = caseId
    this.sendSub()
  }

  private sendSub() {
    if (this.sock && this.isOpen && this.sub !== null) this.sock.send(JSON.stringify({ subscribe: { case_id: this.sub } }))
  }

  private connect() {
    this.o.onStatus(this.attempt === 0 ? 'connecting' : 'reconnecting', this.attempt)
    const s = this.o.factory(this.o.url)
    this.sock = s
    this.isOpen = false
    s.onopen = () => {
      this.isOpen = true
      this.attempt = 0
      this.o.onStatus('open', 0)
      this.sendSub()
    }
    s.onmessage = (ev) => {
      try {
        const m = JSON.parse(String(ev.data)) as WsMessage
        if (m && typeof m === 'object' && 'type' in m) this.o.onMessage(m)
      } catch {
        /* ignore malformed frames */
      }
    }
    s.onclose = () => {
      if (this.sock !== s) return
      this.sock = null
      if (this.stopped) return
      const wait = backoffMs(this.attempt)
      this.attempt += 1
      this.o.onStatus('reconnecting', this.attempt)
      this.timer = (this.o.setTimer ?? ((f, ms) => setTimeout(f, ms)))(() => this.connect(), wait)
    }
    s.onerror = () => {
      /* onclose follows */
    }
  }

  stop() {
    this.stopped = true
    if (this.timer !== null) (this.o.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>)))(this.timer)
    const s = this.sock
    this.sock = null
    s?.close()
    this.o.onStatus('closed', this.attempt)
  }
}

export const wsUrl = (): string => {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws`
}
