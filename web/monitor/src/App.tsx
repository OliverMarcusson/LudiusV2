import { useEffect, useState } from "react"
import { ArrowLeft, Pause, Play } from "lucide-react"

import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Feed } from "@/components/feed"
import { type CallsState, useCalls, useNow, useVms } from "@/lib/feeds"
import { cn } from "@/lib/utils"

const COLUMNS = ["1", "2", "3", "4"] as const
const GRID: Record<string, string> = {
  "1": "grid-cols-1",
  "2": "grid-cols-1 md:grid-cols-2",
  "3": "grid-cols-1 md:grid-cols-2 xl:grid-cols-3",
  "4": "grid-cols-1 md:grid-cols-2 xl:grid-cols-4",
}

// Focus is part of the URL (#/vm/<name>) so a view can be shared and the browser's back button works.
function useFocused(): [string | null, (name: string | null) => void] {
  const read = () => {
    const m = /^#\/vm\/(.+)$/.exec(window.location.hash)
    return m ? decodeURIComponent(m[1]) : null
  }
  const [focused, setFocused] = useState(read)
  useEffect(() => {
    const on = () => setFocused(read())
    window.addEventListener("hashchange", on)
    return () => window.removeEventListener("hashchange", on)
  }, [])
  return [focused, (name) => (window.location.hash = name ? `#/vm/${encodeURIComponent(name)}` : "")]
}

// The two OpenRouter totals. Only an "ok" answer is shown as current; last known totals are kept but marked.
function CallCounters({ state, now }: { state: CallsState; now: number }) {
  const { status, calls, at, error } = state
  const value = (n: number | undefined) => (n === undefined ? "–" : n.toLocaleString())
  const title =
    status === "ok"
      ? calls?.since && `OpenRouter requests since ${calls.since.toLocaleString()}`
      : status === "stale"
        ? `Last update failed (${error}); totals from ${at === null ? "unknown" : new Date(at).toLocaleTimeString()}`
        : status === "unavailable"
          ? `Counters unavailable: ${error}`
          : "Loading counters"
  return (
    <dl
      className="flex items-baseline gap-3 text-sm text-muted-foreground tabular-nums"
      title={title || undefined}
      aria-busy={status === "loading"}
    >
      <div className="flex gap-1.5">
        <dt>Jev calls</dt>
        <dd className={cn("font-medium", status === "ok" && "text-foreground")}>{value(calls?.jev)}</dd>
      </div>
      <div className="flex gap-1.5">
        <dt>LLM calls</dt>
        <dd className={cn("font-medium", status === "ok" && "text-foreground")}>{value(calls?.llm)}</dd>
      </div>
      {status === "stale" && at !== null && (
        <div className="text-stale">stale, {Math.max(0, Math.round((now - at) / 1000))} s old</div>
      )}
      {status === "unavailable" && (
        <div className="text-lost">unavailable</div>
      )}
    </dl>
  )
}

export default function App() {
  const { vms, error, loaded } = useVms()
  const now = useNow()
  const calls = useCalls()
  const [paused, setPaused] = useState(false)
  const [columns, setColumns] = useState(() => localStorage.getItem("monitor.columns") ?? "3")
  const [focused, setFocused] = useFocused()
  const running = vms.filter((v) => v.state === "running").length
  const focusVm = focused ? vms.find((v) => v.name === focused) : undefined

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && focused && setFocused(null)
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })

  return (
    <div className="mx-auto flex min-h-svh max-w-[2400px] flex-col gap-6 px-4 py-5 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold tracking-tight">Ludius range monitor</h1>
          <p className="text-sm text-muted-foreground tabular-nums">
            {loaded ? `${vms.length} VM${vms.length === 1 ? "" : "s"}, ${running} running` : "Connecting"} · view
            only
          </p>
          <CallCounters state={calls} now={now} />
        </div>
        <div className="flex items-center gap-3">
          {!focusVm && (
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground" id="columns-label">
                Columns
              </span>
              <ToggleGroup
                type="single"
                variant="outline"
                size="sm"
                value={columns}
                aria-labelledby="columns-label"
                onValueChange={(v) => {
                  if (!v) return
                  setColumns(v)
                  localStorage.setItem("monitor.columns", v)
                }}
              >
                {COLUMNS.map((c) => (
                  <ToggleGroupItem key={c} value={c} className="w-8 tabular-nums">
                    {c}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            </div>
          )}
          <Button variant="outline" size="sm" onClick={() => setPaused((p) => !p)} aria-pressed={paused}>
            {paused ? <Play /> : <Pause />}
            {paused ? "Resume feeds" : "Pause feeds"}
          </Button>
        </div>
      </header>

      {error && (
        <p role="alert" className="rounded-md border border-lost/40 bg-lost/10 px-4 py-3 text-sm">
          Can't reach the host agent ({error}). Check that ludius-host-agent is running; retrying every 5 s.
        </p>
      )}

      {focused ? (
        <main className="flex flex-col gap-4">
          <div>
            <Button variant="ghost" size="sm" onClick={() => setFocused(null)}>
              <ArrowLeft />
              All VMs
            </Button>
          </div>
          {focusVm ? (
            <div className="mx-auto w-full max-w-[min(100%,calc((100svh-12rem)*16/9))]">
              <Feed vm={focusVm} now={now} paused={paused} maxWidth={1280} large />
            </div>
          ) : (
            loaded && <p className="text-sm text-muted-foreground">No VM named {focused} on this host.</p>
          )}
        </main>
      ) : (
        <main>
          {loaded && vms.length === 0 && !error && (
            <p className="text-sm text-muted-foreground">No virtual machines on this host.</p>
          )}
          <ul className={`grid gap-x-5 gap-y-7 ${GRID[columns] ?? GRID["3"]}`}>
            {vms.map((vm) => (
              <li key={vm.name}>
                <a
                  href={`#/vm/${encodeURIComponent(vm.name)}`}
                  aria-label={`Open ${vm.name}`}
                  className="group block rounded-lg outline-offset-4 focus-visible:outline-2 focus-visible:outline-ring"
                >
                  <Feed vm={vm} now={now} paused={paused} maxWidth={960} />
                </a>
              </li>
            ))}
          </ul>
        </main>
      )}
    </div>
  )
}
