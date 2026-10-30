import { usePolled } from "./feeds"

export interface Person {
  user: string
  name: string
  role: string
  department: string
  manager?: string
  hours?: string[]
  style?: string
  risk_profile?: string[]
  tz?: string
  typing_mean_ms?: string
  think_mean_s?: string
}
export interface Company {
  company: string
  city: string
  summary: string
  projects: string[]
  people: Person[]
  events: { date: string; what: string }[]
  history: { date: string; what: string }[]
  revised?: string
}
export interface Catalog {
  company: Company | null
  people: Person[] | null
  index: { date: string; user: string; source: "plans" | "days" }[]
  sources: { narrative: string; people: string; plans: string; days: string }
  latest:
    | {
        user: string
        date: string | null
        done: number | null
        abandoned: number | null
        next: PlanItem | null
      }[]
    | null
}
export interface PlanItem {
  at: string
  template: string
  context: string
  minutes?: string
  interrupt?: string
  handoff?: string
}
export interface DayEvent {
  event: string
  host_ms?: number
  logical?: string
  at?: string
  template?: string
  task?: string
  status?: string
  reason?: string
  subgoal?: string
  steps?: number
  by?: string
  clock?: string
}
export interface Daybook {
  date: string
  user: string
  plan: PlanItem[] | null
  events: DayEvent[] | null
  sources: { plan: string; recording: string }
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Malformed console data")
  return value as Record<string, unknown>
}
const strings = (r: Record<string, unknown>, keys: string[]) => keys.every((k) => typeof r[k] === "string")
function parseCatalog(value: unknown): Catalog {
  const r = object(value)
  if (
    !Array.isArray(r.index) ||
    (r.people !== null && !Array.isArray(r.people))
  )
    throw new Error("Malformed console data")
  if (r.people?.some((p) => !strings(object(p), ["user", "name", "role", "department"])))
    throw new Error("Malformed people")
  if (r.company !== null) {
    const c = object(r.company)
    if (
      !strings(c, ["company", "city", "summary"]) ||
      ![c.projects, c.people, c.events, c.history].every(Array.isArray)
    )
      throw new Error("Malformed company story")
  }
  return r as unknown as Catalog
}
function parseDay(value: unknown): Daybook {
  const r = object(value)
  if (
    !strings(r, ["date", "user"]) ||
    (r.plan !== null && !Array.isArray(r.plan)) ||
    (r.events !== null && !Array.isArray(r.events))
  )
    throw new Error("Malformed daybook")
  return r as unknown as Daybook
}
export type RunPhase = "none" | "planning" | "preparing" | "running" | "finished" | "stopped" | "failed"
export interface RunStatus {
  phase: RunPhase
  day?: number
  date?: string
  ageMs?: number
  clock?: { ms: number; offset: number } | null
  unplanned?: string[]
  plans?: number
  personas?: { user: string; status: "waiting" | "running" | "done" | "failed"; items: number; done: number }[]
}
const PHASES = ["none", "planning", "preparing", "running", "finished", "stopped", "failed"]
function parseRun(value: unknown): RunStatus {
  const r = object(value)
  if (typeof r.phase !== "string" || !PHASES.includes(r.phase) || (r.personas !== undefined && !Array.isArray(r.personas)))
    throw new Error("Malformed run status")
  return r as unknown as RunStatus
}
export const useRun = () => usePolled("/api/run", parseRun, 3000)
export const useCatalog = () => usePolled("/api/console", parseCatalog, 15_000)
export const useDaybook = (date: string, user: string) =>
  usePolled(`/api/daybook/${encodeURIComponent(date)}/${encodeURIComponent(user)}`, parseDay, 5000)
export const dayLink = (date: string, user: string) =>
  `#/daybook/${encodeURIComponent(date)}/${encodeURIComponent(user)}`
export const personLink = (user: string) => `#/people/${encodeURIComponent(user)}`
export const humanize = (name: string) => name.replaceAll("_", " ")
const activities: Record<string, string> = {
  break: "Take a break",
  notepad_write_note: "Write a note in Notepad",
  wordpad_formatted_note: "Write a formatted note in WordPad",
  excel_new_workbook: "Create an Excel workbook",
  word_short_document: "Write a Word document",
  powerpoint_short_deck: "Create a PowerPoint presentation",
  outlook_read_mail: "Read an email",
  outlook_reply_mail: "Reply to an email",
  outlook_send_mail: "Send an email",
  outlook_send_invite: "Send a meeting invitation",
  edge_web_lookup: "Look up information on the web",
  explorer_new_folder: "Create a folder",
  explorer_rename_move: "Rename and move a file",
  paint_quick_sketch: "Draw a sketch in Paint",
  powershell_admin_check: "Check system health in PowerShell"
}
export const activityName = (name: string) => activities[name] ?? "Other activity"

export function latestDay(catalog: Catalog, user: string): string | undefined {
  return catalog.index
    .filter((i) => i.user === user)
    .map((i) => i.date)
    .sort()
    .at(-1)
}

export function itemRecord(item: PlanItem, events: DayEvent[] | null) {
  const matches = (e: DayEvent) => e.at === item.at && e.template === item.template
  const ends = events?.filter((e) => e.event === "item_end" && matches(e)) ?? []
  const start = events?.findLastIndex((e) => e.event === "item_start" && matches(e)) ?? -1
  const end = events?.findLastIndex((e) => e.event === "item_end" && matches(e)) ?? -1
  return {
    status:
      start > end ? "unfinished" : (ends.at(-1)?.status ?? (events === null ? "unknown" : "unrecorded")),
    attempts: ends.length,
    start,
    end: end >= start ? end : -1
  }
}

export function currentRun(events: DayEvent[] | null): DayEvent[] | null {
  const start = events?.findLastIndex((e) => e.event === "day_start") ?? -1
  return events?.slice(Math.max(0, start)) ?? null
}
