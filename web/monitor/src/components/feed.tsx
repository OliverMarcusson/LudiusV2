import { useEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"
import { STALE_MS, age, frameWidth, useFrame, type Vm } from "@/lib/feeds"

type Health = "live" | "stale" | "lost" | "idle"

function health(vm: Vm, at: number | null, error: string | null, now: number): Health {
  if (vm.state !== "running") return "idle"
  if (at === null) return error ? "lost" : "idle"
  const ms = now - at
  if (ms > STALE_MS * 3) return "lost"
  return ms > STALE_MS || error ? "stale" : "live"
}

const STATE_LABEL: Record<string, string> = {
  running: "Running",
  off: "Off",
  saved: "Saved",
  paused: "Paused",
  starting: "Starting",
  stopping: "Stopping",
  saving: "Saving",
  other: "Unknown",
}

// One VM's desktop: the frame, a freshness line that drains between frames, and its caption.
export function Feed({
  vm,
  now,
  paused,
  maxWidth,
  intervalMs,
  large = false,
}: {
  vm: Vm
  now: number
  paused: boolean
  maxWidth: number
  intervalMs: number
  large?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  const [cssWidth, setCssWidth] = useState(0)
  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setCssWidth(e.contentRect.width))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const running = vm.state === "running"
  const frame = useFrame(vm.name, cssWidth ? frameWidth(cssWidth, maxWidth) : 0, intervalMs, running && !paused)
  const h = health(vm, frame.at, frame.error, now)
  const ratio = vm.width && vm.height ? `${vm.width} / ${vm.height}` : "16 / 9"

  return (
    <figure className="flex min-w-0 flex-col gap-2">
      <div
        ref={box}
        className="relative overflow-hidden rounded-md bg-black ring-1 ring-border group-hover:ring-foreground/45"
        style={{ aspectRatio: ratio }}
      >
        {running && frame.url ? (
          <img
            src={frame.url}
            alt={`Desktop of ${vm.name}`}
            draggable={false}
            className={cn("size-full select-none object-contain", h === "lost" && "opacity-40 grayscale")}
          />
        ) : null}
        {(!running || !frame.url || h === "lost") && (
          <div className="absolute inset-0 grid place-items-center p-4 text-center text-sm text-muted-foreground">
            {!running
              ? `${STATE_LABEL[vm.state]}: no desktop to show`
              : h === "lost"
                ? `Feed lost${frame.error ? `: ${frame.error}` : ""}`
                : "Waiting for the first frame"}
          </div>
        )}
      </div>
      {/* Freshness: drains over STALE_MS and restarts with every frame; the caption carries the same fact as text. */}
      <div className="h-0.5 overflow-hidden rounded-full bg-border" aria-hidden>
        {running && frame.at !== null && !paused && (
          <div
            key={frame.at}
            className={cn(
              "freshness h-full origin-left motion-reduce:animate-none",
              h === "live" && "bg-live",
              h === "stale" && "bg-stale",
              h === "lost" && "bg-lost",
            )}
            style={{ animationDuration: `${STALE_MS}ms` }}
          />
        )}
      </div>
      <figcaption className={cn("flex items-baseline justify-between gap-3", large ? "text-base" : "text-sm")}>
        <span className="truncate font-medium">{vm.name}</span>
        <span className="flex shrink-0 items-baseline gap-3 text-muted-foreground tabular-nums">
          {running && vm.width > 0 && (
            <span>
              {vm.width}×{vm.height}
            </span>
          )}
          <span className="flex items-center gap-1.5">
            <span className={cn("size-2 self-center rounded-full", DOT[h])} aria-hidden />
            <span className={cn(h === "lost" && "text-lost", h === "stale" && "text-stale")}>
              {!running ? STATE_LABEL[vm.state] : paused ? "Paused" : age(frame.at, now)}
            </span>
          </span>
        </span>
      </figcaption>
    </figure>
  )
}

const DOT: Record<Health, string> = {
  live: "bg-live",
  stale: "bg-stale",
  lost: "bg-lost",
  idle: "bg-muted-foreground/50",
}
