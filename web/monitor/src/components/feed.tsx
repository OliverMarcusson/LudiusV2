import { PictureInPicture2 } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { STALE_MS, age, frameWidth, useStream, type Summary, type Vm } from "@/lib/feeds"

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
  large = false,
  summary,
  summaryNote = null,
}: {
  vm: Vm
  now: number
  paused: boolean
  maxWidth: number
  large?: boolean
  summary?: Summary
  summaryNote?: string | null // why the line may be out of date: summariser stopped, agent not answering
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

  const canvas = useRef<HTMLCanvasElement>(null)
  const bar = useRef<HTMLDivElement>(null)
  const drain = useRef<Animation | null>(null)
  // Freshness: every frame refills the line, which then drains over STALE_MS unless the next frame arrives.
  const onFrame = useCallback(() => {
    if (!bar.current || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return
    drain.current?.cancel()
    drain.current = bar.current.animate([{ transform: "scaleX(1)" }, { transform: "scaleX(0)" }], {
      duration: STALE_MS,
      easing: "linear",
      fill: "forwards",
    })
  }, [])

  // Picture-in-picture: the canvas as a 12 fps video stream, so the feed can be watched over other windows.
  const video = useRef<HTMLVideoElement>(null)
  const [pip, setPip] = useState(false)
  const togglePip = async () => {
    const v = video.current
    if (!v || !canvas.current) return
    if (document.pictureInPictureElement === v) return void (await document.exitPictureInPicture())
    v.srcObject = canvas.current.captureStream(12)
    await v.play()
    await v.requestPictureInPicture()
  }
  useEffect(() => {
    const v = video.current
    if (!v) return
    const on = () => setPip(true)
    const off = () => setPip(false)
    v.addEventListener("enterpictureinpicture", on)
    v.addEventListener("leavepictureinpicture", off)
    return () => {
      v.removeEventListener("enterpictureinpicture", on)
      v.removeEventListener("leavepictureinpicture", off)
      if (document.pictureInPictureElement === v) void document.exitPictureInPicture()
    }
  }, [])

  const running = vm.state === "running"
  const width = cssWidth ? frameWidth(cssWidth, maxWidth) : 0
  const frame = useStream(vm.name, width, running && !paused, canvas, onFrame, pip)
  const h = health(vm, frame.at, frame.error, now)
  const shown = running && frame.at !== null
  const ratio = vm.width && vm.height ? `${vm.width} / ${vm.height}` : "16 / 9"

  return (
    <figure className="flex min-w-0 flex-col gap-2">
      <div
        ref={box}
        className="relative overflow-hidden rounded-md bg-black ring-1 ring-border group-hover:ring-foreground/45"
        style={{ aspectRatio: ratio }}
      >
        <canvas
          ref={canvas}
          role="img"
          aria-label={`Desktop of ${vm.name}`}
          className={cn("size-full object-contain", !shown && "invisible", h === "lost" && "opacity-40 grayscale")}
        />
        {large && (
          <video
            ref={video}
            muted
            playsInline
            aria-hidden
            className="pointer-events-none absolute size-px opacity-0"
          />
        )}
        {(!shown || h === "lost") && (
          <div className="absolute inset-0 grid place-items-center p-4 text-center text-sm text-muted-foreground">
            {!running
              ? `${STATE_LABEL[vm.state]}: no desktop to show`
              : h === "lost"
                ? `Feed lost${frame.error ? `: ${frame.error}` : ""}`
                : "Waiting for the first frame"}
          </div>
        )}
      </div>
      {/* The caption carries the same facts as text (fps, or the frame's age). */}
      <div className="h-0.5 overflow-hidden rounded-full bg-border" aria-hidden>
        <div
          ref={bar}
          className={cn(
            "h-full origin-left",
            (!shown || paused) && "invisible",
            h === "live" && "bg-live",
            h === "stale" && "bg-stale",
            h === "lost" && "bg-lost",
          )}
        />
      </div>
      <figcaption className={cn("flex items-baseline justify-between gap-3", large ? "text-base" : "text-sm")}>
        <span className="truncate font-medium">{vm.name}</span>
        <span className="flex shrink-0 items-baseline gap-3 text-muted-foreground tabular-nums">
          {running && vm.width > 0 && (
            <span>
              {vm.width}×{vm.height}
            </span>
          )}
          {large && document.pictureInPictureEnabled && (
            <Button
              variant="ghost"
              size="icon-sm"
              className="self-center"
              disabled={!shown}
              onClick={() => void togglePip()}
              aria-pressed={pip}
              aria-label={pip ? "Close picture-in-picture" : "Open in picture-in-picture"}
              title={pip ? "Close picture-in-picture" : "Picture-in-picture"}
            >
              <PictureInPicture2 />
            </Button>
          )}
          <span className="flex items-center gap-1.5">
            <span className={cn("size-2 self-center rounded-full", DOT[h])} aria-hidden />
            <span className={cn(h === "lost" && "text-lost", h === "stale" && "text-stale")}>
              {!running ? STATE_LABEL[vm.state] : paused ? "Paused" : age(frame.at, now, frame.fps)}
            </span>
          </span>
        </span>
      </figcaption>
      <Activity summary={summary} note={summaryNote} large={large} />
    </figure>
  )
}

const hhmm = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })

// What the persona is doing, in one line from the controller's summariser. Only "ok" text reads as current;
// anything older or uncertain says so in the line itself.
function Activity({ summary, note, large }: { summary?: Summary; note: string | null; large: boolean }) {
  const s = summary
  const marker =
    note ??
    (s?.state === "stale"
      ? "stale: last update failed"
      : s?.state === "pending"
        ? "from the log, summary pending"
        : null)
  return (
    <p
      className={cn("text-muted-foreground", large ? "text-sm" : "text-xs")}
      title={s?.model ? `Written by ${s.model}${s.at ? ` at ${s.at.toLocaleString()}` : ""}` : undefined}
    >
      <span className={cn(s?.state === "ok" && !note && "text-foreground", s?.state === "paused" && "text-stale")}>
        {s ? s.text : "no activity summary"}
      </span>
      {s?.at && s.state !== "idle" && s.state !== "unknown" && <span className="tabular-nums"> · {hhmm(s.at)}</span>}
      {marker && <span className="text-stale"> · {marker}</span>}
    </p>
  )
}

const DOT: Record<Health, string> = {
  live: "bg-live",
  stale: "bg-stale",
  lost: "bg-lost",
  idle: "bg-muted-foreground/50",
}
