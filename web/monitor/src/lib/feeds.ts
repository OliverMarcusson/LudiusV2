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
          setVms((await res.json()) as Vm[])
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
  since?: string
}

export function useCalls(intervalMs = 2000): Calls | null {
  const [calls, setCalls] = useState<Calls | null>(null)
  useEffect(() => {
    let stop = false
    let timer: number | undefined
    const tick = async () => {
      try {
        const res = await fetch("/api/calls", { cache: "no-store" })
        if (res.ok && !stop) setCalls((await res.json()) as Calls)
      } catch {
        // keep the last totals; useVms already reports an unreachable agent
      } finally {
        if (!stop) timer = window.setTimeout(tick, intervalMs)
      }
    }
    void tick()
    return () => {
      stop = true
      window.clearTimeout(timer)
    }
  }, [intervalMs])
  return calls
}

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
