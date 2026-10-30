import { useEffect, useState } from "react"
import { ArrowLeft, Pause, Play } from "lucide-react"
import { motion } from "motion/react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Toaster } from "@/components/ui/sonner"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Feed } from "@/components/feed"
import { Portrait } from "@/components/portraits"
import {
  type Admission,
  type Calls,
  type Interaction,
  type Polled,
  useAdmission,
  useCalls,
  useInteractions,
  useNow,
  useVms
} from "@/lib/feeds"
import { cn } from "@/lib/utils"
import { type Catalog, dayLink, activityName, personLink } from "@/lib/console"

const COLUMNS = ["1", "2", "3", "4"] as const
const GRID: Record<string, string> = {
  "1": "grid-cols-1",
  "2": "grid-cols-1 md:grid-cols-2",
  "3": "grid-cols-1 md:grid-cols-2 xl:grid-cols-3",
  "4": "grid-cols-1 md:grid-cols-2 xl:grid-cols-4"
}

function useFocused(): [string | null, (name: string | null) => void] {
  const read = () => {
    const m = /^#\/vm\/(.+)$/.exec(window.location.hash)
    try {
      return m ? decodeURIComponent(m[1]) : null
    } catch {
      return null
    }
  }
  const [focused, setFocused] = useState(read)
  useEffect(() => {
    const on = () => setFocused(read())
    window.addEventListener("hashchange", on)
    return () => window.removeEventListener("hashchange", on)
  }, [])
  return [focused, (name) => (window.location.hash = name ? `#/vm/${encodeURIComponent(name)}` : "")]
}

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
      {status === "unavailable" && <div className="text-lost">unavailable</div>}
    </dl>
  )
}

const ACTIVE_MS = 90_000

function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s} s`
  if (s < 3600) return `${Math.floor(s / 60)} min`
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`
}

function AdmissionProgress({ state, now }: { state: Polled<Admission>; now: number }) {
  const { status, data: a, at, error } = state
  if (a?.state === "none") return null
  if (!a || at === null) {
    return (
      <section
        aria-label="Admission progress"
        aria-busy={status === "loading"}
        className="text-sm text-muted-foreground"
      >
        <span className="font-medium text-foreground">Admission</span>{" "}
        {status === "loading" ? "loading" : <span className="text-lost">unavailable ({error})</span>}
      </section>
    )
  }
  const age = a.ageMs === null ? null : a.ageMs + (now - at)
  const st =
    a.state === "running" && age !== null && age >= ACTIVE_MS ? (a.inRun ? "stale" : "idle") : a.state
  const label =
    st === "running"
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
        <span className={cn(st === "stale" && "text-stale", st === "running" && "text-foreground")}>
          {label}
        </span>
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
      {
        <span>
          {a.done} done, <span className={cn(a.failed > 0 && "text-stale")}>{a.failed} failed</span>
        </span>
      }
      {age !== null && <span>last event {ago(age)} ago</span>}
      {a.truncated && (
        <span title="The log is larger than the agent reads; counts cover its end only">partial log</span>
      )}
      {status === "stale" && (
        <span className="text-stale" title={error ?? undefined}>
          agent not answering, shown as of {ago(now - at)} ago
        </span>
      )}
    </section>
  )
}

const HIGHLIGHT_MS = 10_000
const KIND_LABEL: Record<Interaction["kind"], string> = {
  mail: "mailed",
  reply: "replied to",
  invite: "invited",
  request: "asked",
  request_done: "delivered a request for"
}

function useInteractionAlerts(list: Interaction[] | undefined, now: number): Set<string> {
  const [seen, setSeen] = useState<Set<string> | null>(null)
  const [lit, setLit] = useState<Record<string, number>>({})
  useEffect(() => {
    if (!list) return
    if (seen === null) return setSeen(new Set(list.map((i) => i.id)))
    const fresh = list.filter((i) => !seen.has(i.id))
    if (!fresh.length) return
    setSeen(new Set([...seen, ...fresh.map((i) => i.id)]))
    const until = Date.now() + HIGHLIGHT_MS
    setLit((l) => ({
      ...l,
      ...Object.fromEntries(
        fresh.flatMap((i) => [
          [vmOf(i.from), until],
          [vmOf(i.to), until]
        ])
      )
    }))
    for (const i of fresh) {
      toast(`${i.from} ${KIND_LABEL[i.kind]} ${i.to}`, {
        description: [i.label, i.logical && `at ${i.logical.replace("T", " ")}`].filter(Boolean).join(" · "),
        duration: HIGHLIGHT_MS
      })
    }
  }, [list, seen])
  return new Set(Object.keys(lit).filter((vm) => lit[vm] > now))
}

const vmOf = (user: string) => `ludius-${user}`

export default function LiveMonitor({ catalog }: { catalog: Catalog | null }) {
  const { vms, error, loaded } = useVms()
  const now = useNow()
  const calls = useCalls()
  const admission = useAdmission()
  const interactions = useInteractions()
  const lit = useInteractionAlerts(interactions.data ?? undefined, now)
  const [paused, setPaused] = useState(false)
  const [columns, setColumns] = useState(() => {
    try {
      return localStorage.getItem("monitor.columns") ?? "3"
    } catch {
      return "3"
    }
  })
  const [focused, setFocused] = useFocused()
  const runningVms = vms.filter((v) => v.state === "running")
  const running = runningVms.length
  const focusVm = focused ? runningVms.find((v) => v.name === focused) : undefined
  const roster = [...vms]
  for (const person of catalog?.people ?? []) {
    const name = vmOf(person.user)
    if (!roster.some((vm) => vm.name === name))
      roster.push({ name, state: "other", width: 1920, height: 1080 })
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && focused && setFocused(null)
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div>
          <h1 className="text-xl font-semibold">Overview</h1>
          <p className="mt-1 flex items-center gap-2 text-sm text-muted-foreground tabular-nums">
            <span
              aria-hidden
              className={cn("size-2 rounded-full bg-muted-foreground/50", loaded && running > 0 && "bg-live")}
            />
            {loaded ? `${running} of ${vms.length} workstations running` : "Connecting to workstations"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <CallCounters state={calls} now={now} />
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
                  try {
                    localStorage.setItem("monitor.columns", v)
                  } catch {
                  }
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
        <div className="flex flex-col gap-4">
          <div>
            <Button variant="ghost" size="sm" onClick={() => setFocused(null)}>
              <ArrowLeft />
              All VMs
            </Button>
          </div>
          {focusVm ? (
            <motion.div
              layoutId={`feed-${focusVm.name}`}
              className="mx-auto w-full max-w-[min(100%,calc((100svh-12rem)*16/9))]"
            >
              <Feed
                vm={focusVm}
                now={now}
                paused={paused}
                maxWidth={1280}
                large
              />
            </motion.div>
          ) : (
            loaded && (
              <p className="text-sm text-muted-foreground">VM {focused} is not running or is unavailable.</p>
            )
          )}
        </div>
      ) : (
        <div>
          {loaded && runningVms.length === 0 && !error && (
            <p className="mb-4 text-sm text-muted-foreground">No running virtual machines.</p>
          )}
          <ul className={cn("grid gap-x-5 gap-y-8", GRID[columns] ?? GRID["3"])}>
            {roster.map((vm) => {
              const person = catalog?.people?.find((p) => vmOf(p.user) === vm.name)
              const day = person && catalog?.latest?.find((d) => d.user === person.user)
              return (
                <motion.li key={vm.name} layout="position" className="flex min-w-0 flex-col gap-2.5">
                  <div className="flex min-w-0 items-center gap-2.5">
                    {person ? (
                      <Portrait person={person} className="size-8 text-xs" />
                    ) : (
                      <span
                        className="grid size-8 shrink-0 place-items-center rounded-md bg-muted text-xs font-medium"
                        aria-hidden
                      >
                        VM
                      </span>
                    )}
                    <div className="flex min-w-0 flex-1 flex-col leading-tight">
                      {person ? (
                        <a href={personLink(person.user)} className="truncate text-sm font-medium hover:underline">
                          {person.name}
                        </a>
                      ) : (
                        <strong className="truncate text-sm font-medium">{vm.name}</strong>
                      )}
                      <span className="truncate text-xs text-muted-foreground">
                        {person?.role ?? "Range workstation"}
                      </span>
                    </div>
                    {person && <span className="shrink-0 text-xs text-muted-foreground">{person.department}</span>}
                  </div>
                  <motion.a
                    layoutId={`feed-${vm.name}`}
                    href={`#/vm/${encodeURIComponent(vm.name)}`}
                    aria-label={`Open ${vm.name}`}
                    className={cn(
                      "group block rounded-lg outline-offset-4 focus-visible:outline-2 focus-visible:outline-ring",
                      lit.has(vm.name) && "outline-2 outline-live"
                    )}
                  >
                    <Feed
                      vm={vm}
                      now={now}
                      paused={paused}
                      maxWidth={960}
                    />
                  </motion.a>
                  {person && (
                    <div className="border-t pt-2 text-xs text-muted-foreground tabular-nums">
                      {day?.date ? (
                        <>
                          <p className="flex items-baseline justify-between gap-3">
                            <a href={dayLink(day.date, person.user)} className="link text-foreground">
                              <span>Latest recorded day</span> <strong>{day.date}</strong>
                            </a>
                            <span>
                              {day.done === null
                                ? "Outcomes unknown"
                                : `${day.done} done / ${day.abandoned} abandoned`}
                            </span>
                          </p>
                          {day.next && (
                            <p className="mt-1 truncate">
                              Without an outcome: {day.next.at} / {activityName(day.next.template)}
                            </p>
                          )}
                        </>
                      ) : (
                        <p>No day plan or recording available.</p>
                      )}
                    </div>
                  )}
                </motion.li>
              )
            })}
          </ul>
        </div>
      )}
      <Toaster position="bottom-right" />
    </div>
  )
}
