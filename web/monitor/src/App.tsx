import { useEffect, useState } from "react"
import { ArrowLeft, Pause, Play } from "lucide-react"

import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Feed } from "@/components/feed"
import { type Admission, type Calls, type Polled, useAdmission, useCalls, useNow, useVms } from "@/lib/feeds"
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
function CallCounters({ state, now }: { state: Polled<Calls>; now: number }) {
  const { status, data: calls, at, error } = state
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
        <dt>Decision calls</dt>
        <dd className={cn("font-medium", status === "ok" && "text-foreground")}>{value(calls?.decision)}</dd>
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

const ACTIVE_MS = 90_000 // matches the agent's running window

function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s} s`
  if (s < 3600) return `${Math.floor(s / 60)} min`
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`
}

// Progress of the newest admission log: running only while events keep arriving; never inferred from call counts.
function AdmissionProgress({ state, now }: { state: Polled<Admission>; now: number }) {
  const { status, data: a, at, error } = state
  if (!a || at === null) {
    return (
      <section aria-label="Admission progress" aria-busy={status === "loading"} className="text-sm text-muted-foreground">
        <span className="font-medium text-foreground">Admission</span>{" "}
        {status === "loading" ? "loading" : <span className="text-lost">unavailable ({error})</span>}
      </section>
    )
  }
  // The answer ages while polls fail; a run that was active when last seen goes stale here too.
  const age = a.ageMs === null ? null : a.ageMs + (now - at)
  const st = a.state === "running" && age !== null && age >= ACTIVE_MS ? (a.inRun ? "stale" : "idle") : a.state
  const label =
    st === "none"
      ? "No admission runs yet"
      : st === "running"
        ? a.inRun
          ? "Running"
          : "Running, between runs"
        : st === "completed"
          ? a.stopped
            ? "Stopped early, not admitted"
            : a.admitted === null
              ? "Completed"
              : a.admitted
                ? "Completed, admitted"
                : "Completed, rejected"
          : st === "stale"
            ? "No events mid-run: may be stuck or stopped"
            : "Idle: last run ended, batch finished or stopped"
  const dot = st === "running" ? "bg-live" : st === "stale" ? "bg-stale" : "bg-muted-foreground/50"
  return (
    <section
      aria-label="Admission progress"
      className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm text-muted-foreground tabular-nums"
    >
      <span className="font-medium text-foreground">Admission</span>
      <span className="flex items-center gap-1.5">
        <span className={cn("size-2 self-center rounded-full", dot)} aria-hidden />
        <span className={cn(st === "stale" && "text-stale", st === "running" && "text-foreground")}>{label}</span>
      </span>
      {a.task && <span className="font-medium text-foreground">{a.task}</span>}
      {a.variant !== null && a.run > 0 && (
        <span>
          {a.inRun || st === "running" ? "" : "last: "}variant {a.variant}, run {a.run}
        </span>
      )}
      {a.inRun && a.subgoal && (
        <span>
          step {a.step} · {a.subgoal}
          {a.action && ` · ${a.action}${a.result ? ` → ${a.result}` : ""}`}
        </span>
      )}
      {!a.inRun && a.status && <span>last run {a.status}</span>}
      {st !== "none" && (
        <span>
          {a.done} done, <span className={cn(a.failed > 0 && "text-stale")}>{a.failed} failed</span>
        </span>
      )}
      {age !== null && st !== "none" && <span>last event {ago(age)} ago</span>}
      {a.truncated && <span title="The log is larger than the agent reads; counts cover its end only">partial log</span>}
      {status === "stale" && (
        <span className="text-stale" title={error ?? undefined}>
          agent not answering, shown as of {ago(now - at)} ago
        </span>
      )}
    </section>
  )
}

export default function App() {
  const { vms, error, loaded } = useVms()
  const now = useNow()
  const calls = useCalls()
  const admission = useAdmission()
  const [paused, setPaused] = useState(false)
  const [columns, setColumns] = useState(() => localStorage.getItem("monitor.columns") ?? "3")
  const [focused, setFocused] = useFocused()
  const runningVms = vms.filter((v) => v.state === "running")
  const running = runningVms.length
  const focusVm = focused ? runningVms.find((v) => v.name === focused) : undefined

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

      <AdmissionProgress state={admission} now={now} />

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
            loaded && <p className="text-sm text-muted-foreground">VM {focused} is not running or is unavailable.</p>
          )}
        </main>
      ) : (
        <main>
          {loaded && runningVms.length === 0 && !error && (
            <p className="text-sm text-muted-foreground">No running virtual machines.</p>
          )}
          <ul className={`grid gap-x-5 gap-y-7 ${GRID[columns] ?? GRID["3"]}`}>
            {runningVms.map((vm) => (
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
