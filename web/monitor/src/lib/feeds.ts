import { type RefObject, useEffect, useState } from "react"

// Read-only views of the host agent's monitor API. Nothing here can send input to a VM.

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
          // Range servers (ludius-srv01) host no persona: nothing to watch there.
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

// OpenRouter requests the controller has sent, cumulative across runs (runs/openrouter-calls.json).
// Polling only reads the totals; it never makes a request of its own to OpenRouter.
export interface Calls {
  jev: number
  llm: number
  since: Date | null // when counting started; null before the first call
}

// loading: no answer yet. ok: `data` is current. stale: the last poll failed, `data` is the last good answer
// from `at`. unavailable: no good answer yet, `error` says why.
export type PollStatus = "loading" | "ok" | "stale" | "unavailable"

export interface Polled<T> {
  status: PollStatus
  data: T | null
  at: number | null // ms epoch of the last good answer
  error: string | null
}

const POLL_TIMEOUT_MS = 5000

const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0

function record(data: unknown, what: string): Record<string, unknown> {
  if (typeof data !== "object" || data === null || Array.isArray(data)) throw new Error(`malformed ${what}`)
  return data as Record<string, unknown>
}

// Accept only what the counters can show truthfully: two non-negative integers and an optional valid date.
export function parseCalls(data: unknown): Calls {
  const { jev, llm, since } = record(data, "counter data")
  if (!count(jev) || !count(llm)) throw new Error("malformed counter data")
  const at = typeof since === "string" ? new Date(since) : null
  return { jev, llm, since: at && !Number.isNaN(at.getTime()) ? at : null }
}

// running: an event in the last 90 s. completed: the log ends with an admission_end record. stale: quiet in the
// middle of a run, so the worker may be stuck or gone. idle: quiet after a run ended, so the batch finished or
// stopped; the log alone can't tell which. none: no admission log yet.
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
  status: string | null // how the last finished run ended, e.g. done or abandoned
  done: number
  failed: number
  admitted: boolean | null
  stopped: boolean // admission ended early, e.g. a failed checkpoint restore
  ageMs: number | null
  truncated: boolean
}

const ADMISSION_STATES: readonly string[] = ["running", "completed", "stale", "idle", "none"]
const TOKEN = /^[A-Za-z0-9_.:+-]{1,64}$/

// The agent already sends identifiers only; check again so nothing else can reach the page.
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

// Polls a read-only JSON route. Each request is aborted after 5 s; a failed poll keeps the last good answer but
// marks it stale. `parse` must be stable (a module-level function) and throw on anything it can't show.
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

export const useCalls = () => usePolled("/api/calls", parseCalls)
export const useAdmission = () => usePolled("/api/admission", parseAdmission)

export interface Stream {
  at: number | null // ms epoch of the last frame drawn
  fps: number // frames drawn during the last second
  error: string | null
}

// A VM's 12 fps feed over a WebSocket: each binary message is a JPEG, drawn straight onto the canvas so the
// page doesn't re-render per frame. Stats update once a second. Closed while paused or the tab is hidden;
// reconnects after 2 s when the connection drops.
export function useStream(
  vm: string,
  width: number,
  enabled: boolean,
  canvas: RefObject<HTMLCanvasElement | null>,
  onFrame: () => void, // must be stable (useCallback)
): Stream {
  const [stats, setStats] = useState<Stream>({ at: null, fps: 0, error: null })
  const visible = usePageVisible()

  useEffect(() => {
    if (!enabled || !visible || width <= 0) return
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
        if (drawing) return // still decoding the previous frame: drop this one rather than queue
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
          // a corrupt frame: skip it
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
  }, [vm, width, enabled, visible, canvas, onFrame])

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

// Request widths in 160 px steps so viewers of the same size share one capture loop on the agent.
export function frameWidth(cssWidth: number, max: number): number {
  const px = Math.round(cssWidth * (window.devicePixelRatio || 1))
  return Math.min(max, Math.max(320, Math.ceil(px / 160) * 160))
}

export function age(at: number | null, now: number, fps: number): string {
  if (at === null) return "no frame yet"
  const s = Math.max(0, Math.round((now - at) / 1000))
  if (s < 2) return `${fps} fps`
  if (s < 60) return `${s} s ago`
  return `${Math.floor(s / 60)} min ago`
}
