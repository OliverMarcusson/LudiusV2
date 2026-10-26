import { useEffect, useRef, useState } from "react"

// Read-only views of the host agent's monitor API. Nothing here can send input to a VM.

export type VmState = "running" | "off" | "saved" | "paused" | "starting" | "stopping" | "saving" | "other"

export interface Vm {
  name: string
  state: VmState
  width: number
  height: number
}

export interface Frame {
  url: string | null
  at: number | null // ms epoch of the last frame received
  error: string | null
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

// Polls one VM's console frame. The next request starts only after the previous one finished, so a slow
// host never queues work; polling pauses while the tab is hidden.
export function useFrame(vm: string, width: number, intervalMs: number, enabled: boolean): Frame {
  const [frame, setFrame] = useState<Frame>({ url: null, at: null, error: null })
  const urls = useRef<string[]>([])
  const visible = usePageVisible()

  useEffect(() => {
    if (!enabled || !visible || width <= 0) return
    const ctrl = new AbortController()
    let timer: number | undefined
    const tick = async () => {
      try {
        const res = await fetch(`/api/vms/${encodeURIComponent(vm)}/frame?w=${width}`, {
          cache: "no-store",
          signal: ctrl.signal,
        })
        if (!res.ok) throw new Error(res.status === 409 ? "VM is not running" : `frame request failed (${res.status})`)
        const url = URL.createObjectURL(await res.blob())
        // Keep the previous URL alive until this one has replaced it on screen.
        urls.current.push(url)
        while (urls.current.length > 2) URL.revokeObjectURL(urls.current.shift()!)
        setFrame({ url, at: Date.now(), error: null })
      } catch (e) {
        if (ctrl.signal.aborted) return
        setFrame((f) => ({ ...f, error: e instanceof Error ? e.message : String(e) }))
      }
      if (!ctrl.signal.aborted) timer = window.setTimeout(tick, intervalMs)
    }
    void tick()
    return () => {
      ctrl.abort()
      window.clearTimeout(timer)
    }
  }, [vm, width, intervalMs, enabled, visible])

  useEffect(() => () => urls.current.forEach((u) => URL.revokeObjectURL(u)), [])
  return frame
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

// Request widths in 160 px steps so viewers of the same size share the agent's frame cache.
export function frameWidth(cssWidth: number, max: number): number {
  const px = Math.round(cssWidth * (window.devicePixelRatio || 1))
  return Math.min(max, Math.max(320, Math.ceil(px / 160) * 160))
}

export function age(at: number | null, now: number): string {
  if (at === null) return "no frame yet"
  const s = Math.max(0, Math.round((now - at) / 1000))
  if (s < 2) return "live"
  if (s < 60) return `${s} s ago`
  return `${Math.floor(s / 60)} min ago`
}
