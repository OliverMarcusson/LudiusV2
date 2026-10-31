import { type RefObject, useEffect, useState } from "react"


export type VmState = "running" | "off" | "saved" | "paused" | "starting" | "stopping" | "saving" | "other"

export interface Vm {
  name: string
  state: VmState
  width: number
  height: number
}

export const STALE_MS = 5000

export function useVms(intervalMs = 5000): { vms: Vm[]; error: string | null; loaded: boolean } {
  const [vms, setVms] = useState<Vm[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    let stop = false
    let timer: number | undefined
    const tick = async () => {
      try {
        const res = await fetch("/api/vms", { cache: "no-store" })
        if (!res.ok) throw new Error(`host agent answered ${res.status}`)
        if (!stop) {
          setVms(((await res.json()) as Vm[]).filter((v) => !/-srv\d*$/.test(v.name)))
          setError(null)
        }
      } catch (e) {
        if (!stop) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!stop) {
          setLoaded(true)
          timer = window.setTimeout(tick, intervalMs)
        }
      }
    }
    void tick()
    return () => {
      stop = true
      window.clearTimeout(timer)
    }
  }, [intervalMs])
  return { vms, error, loaded }
}

export interface Calls {
  decision: number
  llm: number
  since: Date | null
}

export type PollStatus = "loading" | "ok" | "stale" | "unavailable"

export interface Polled<T> {
  status: PollStatus
  data: T | null
  at: number | null
  error: string | null
}

const POLL_TIMEOUT_MS = 5000

const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0

function record(data: unknown, what: string): Record<string, unknown> {
  if (typeof data !== "object" || data === null || Array.isArray(data)) throw new Error(`malformed ${what}`)
  return data as Record<string, unknown>
}

export function parseCalls(data: unknown): Calls {
  const { decision, jev, llm, since } = record(data, "counter data")
  const decisions = decision ?? jev
  if (!count(decisions) || !count(llm)) throw new Error("malformed counter data")
  const at = typeof since === "string" ? new Date(since) : null
  return { decision: decisions, llm, since: at && !Number.isNaN(at.getTime()) ? at : null }
}

export type AdmissionState = "running" | "completed" | "stale" | "idle" | "none"

export interface Admission {
  state: AdmissionState
  task: string | null
  variant: string | null
  run: number
  inRun: boolean
  step: number
  subgoal: string | null
  action: string | null
  result: string | null
  status: string | null
  done: number
  failed: number
  admitted: boolean | null
  stopped: boolean
  ageMs: number | null
  truncated: boolean
}

const ADMISSION_STATES: readonly string[] = ["running", "completed", "stale", "idle", "none"]
const TOKEN = /^[A-Za-z0-9_.:+-]{1,64}$/

export function parseAdmission(data: unknown): Admission {
  const r = record(data, "admission data")
  if (typeof r.state !== "string" || !ADMISSION_STATES.includes(r.state)) throw new Error("malformed admission data")
  const token = (v: unknown) => (typeof v === "string" && TOKEN.test(v) ? v : null)
  const num = (v: unknown) => (count(v) ? v : 0)
  const a: Admission = {
    state: r.state as AdmissionState,
    task: token(r.task),
    variant: token(r.variant),
    run: num(r.run),
    inRun: r.inRun === true,
    step: num(r.step),
    subgoal: token(r.subgoal),
    action: token(r.action),
    result: token(r.result),
    status: token(r.status),
    done: num(r.done),
    failed: num(r.failed),
    admitted: typeof r.admitted === "boolean" ? r.admitted : null,
    stopped: r.stopped === true,
    ageMs: count(r.ageMs) ? r.ageMs : null,
    truncated: r.truncated === true,
  }
  if (a.state !== "none" && (a.task === null || a.ageMs === null)) throw new Error("malformed admission data")
  return a
}

export function usePolled<T>(url: string, parse: (data: unknown) => T, intervalMs = 2000): Polled<T> {
  const [state, setState] = useState<Polled<T>>({ status: "loading", data: null, at: null, error: null })
  useEffect(() => {
    let stop = false
    let timer: number | undefined
    let inflight: AbortController | null = null
    const tick = async () => {
      const ctl = (inflight = new AbortController())
      const deadline = window.setTimeout(() => ctl.abort(), POLL_TIMEOUT_MS)
      try {
        const res = await fetch(url, { cache: "no-store", signal: ctl.signal })
        if (!res.ok) throw new Error(`host agent answered ${res.status}`)
        const data = parse(await res.json())
        if (!stop) setState({ status: "ok", data, at: Date.now(), error: null })
      } catch (e) {
        if (stop) return
        const error = ctl.signal.aborted ? "no answer within 5 s" : e instanceof Error ? e.message : String(e)
        setState((s) => ({ ...s, status: s.data ? "stale" : "unavailable", error }))
      } finally {
        window.clearTimeout(deadline)
        if (!stop) timer = window.setTimeout(tick, intervalMs)
      }
    }
    void tick()
    return () => {
      stop = true
      window.clearTimeout(timer)
      inflight?.abort()
    }
  }, [url, parse, intervalMs])
  return state
}

export interface Interaction {
  id: string
  from: string
  to: string
  kind: "mail" | "reply" | "invite" | "chat" | "file" | "request" | "request_done"
  label: string
  logical: string | null
}

const KINDS: readonly string[] = ["mail", "reply", "invite", "chat", "file", "request", "request_done"]

function parseInteractions(v: unknown): Interaction[] {
  if (!Array.isArray(v)) return []
  return v.flatMap((x) => {
    const i = (x ?? {}) as Record<string, unknown>
    const p = i.personas
    if (typeof i.id !== "string" || !Array.isArray(p) || typeof p[0] !== "string" || typeof p[1] !== "string") return []
    if (typeof i.kind !== "string" || !KINDS.includes(i.kind)) return []
    return [{
      id: i.id,
      from: p[0],
      to: p[1],
      kind: i.kind as Interaction["kind"],
      label: typeof i.label === "string" ? i.label.slice(0, 120) : "",
      logical: typeof i.logical === "string" ? i.logical : null,
    }]
  })
}

export const useCalls = () => usePolled("/api/calls", parseCalls)
const parseInteractionList = (data: unknown) => parseInteractions(record(data, "interaction data").interactions)
export const useInteractions = () => usePolled("/api/interactions", parseInteractionList, 5000)
export const useAdmission = () => usePolled("/api/admission", parseAdmission)

export interface Stream {
  at: number | null
  fps: number
  error: string | null
}

export function useStream(
  vm: string,
  width: number,
  enabled: boolean,
  canvas: RefObject<HTMLCanvasElement | null>,
  onFrame: () => void,
  keepAlive = false,
): Stream {
  const [stats, setStats] = useState<Stream>({ at: null, fps: 0, error: null })
  const visible = usePageVisible()

  useEffect(() => {
    if (!enabled || (!visible && !keepAlive) || width <= 0) return
    let ws: WebSocket | null = null
    let retry: number | undefined
    let stopped = false
    let drawing = false
    let count = 0
    let last: number | null = null
    let error: string | null = null

    const open = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws"
      ws = new WebSocket(`${scheme}://${window.location.host}/api/vms/${encodeURIComponent(vm)}/stream?w=${width}`)
      ws.binaryType = "blob"
      ws.onmessage = async (e: MessageEvent<Blob | string>) => {
        if (typeof e.data === "string") {
          error = e.data
          return
        }
        if (drawing) return
        drawing = true
        try {
          const bmp = await createImageBitmap(e.data)
          const c = canvas.current
          if (c) {
            if (c.width !== bmp.width || c.height !== bmp.height) {
              c.width = bmp.width
              c.height = bmp.height
            }
            c.getContext("2d")?.drawImage(bmp, 0, 0)
          }
          bmp.close()
          count++
          last = Date.now()
          error = null
          onFrame()
        } catch {
        } finally {
          drawing = false
        }
      }
      ws.onclose = () => {
        if (stopped) return
        error ??= "connection lost"
        retry = window.setTimeout(open, 2000)
      }
    }
    open()

    let since = Date.now()
    const tick = window.setInterval(() => {
      const now = Date.now()
      setStats({ at: last, fps: Math.round((count * 1000) / (now - since)), error })
      count = 0
      since = now
    }, 1000)
    return () => {
      stopped = true
      window.clearTimeout(retry)
      window.clearInterval(tick)
      ws?.close()
    }
  }, [vm, width, enabled, visible, keepAlive, canvas, onFrame])

  return stats
}

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(t)
  }, [intervalMs])
  return now
}

function usePageVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState === "visible")
  useEffect(() => {
    const on = () => setVisible(document.visibilityState === "visible")
    document.addEventListener("visibilitychange", on)
    return () => document.removeEventListener("visibilitychange", on)
  }, [])
  return visible
}

export function frameWidth(cssWidth: number, max: number): number {
  const px = Math.round(cssWidth * (window.devicePixelRatio || 1))
  return Math.min(max, Math.max(320, Math.ceil(px / 160) * 160))
}

export function age(at: number | null, now: number, fps: number): string {
  if (at === null) return "no frame yet"
  const s = Math.max(0, Math.round((now - at) / 1000))
  if (now - at < STALE_MS) return `${fps} fps`
  if (s < 60) return `${s} s ago`
  return `${Math.floor(s / 60)} min ago`
}
