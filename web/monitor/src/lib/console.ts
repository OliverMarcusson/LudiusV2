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
        reacting?: PlanItem | null
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
  error?: string
  personas?: { user: string; status: "waiting" | "running" | "done" | "failed"; items: number; done: number }[]
}
const PHASES = ["none", "planning", "preparing", "running", "finished", "stopped", "failed"]
function parseRun(value: unknown): RunStatus {
  const r = object(value)
  if (typeof r.phase !== "string" || !PHASES.includes(r.phase) || (r.personas !== undefined && !Array.isArray(r.personas)))
    throw new Error("Malformed run status")
  return r as unknown as RunStatus
}
export interface Template {
  task: string
  app: string
  capability?: string
  about: string
  params: string[]
  budget_minutes?: string
}
function parseTemplates(value: unknown): Template[] {
  const valid = (t: Record<string, unknown>) =>
    strings(t, ["task", "app", "about"]) &&
    Array.isArray(t.params) &&
    t.params.every((p) => typeof p === "string") &&
    ["capability", "budget_minutes"].every((k) => t[k] === undefined || typeof t[k] === "string")
  if (!Array.isArray(value) || !value.every((t) => valid(object(t)))) throw new Error("Malformed templates")
  return value as Template[]
}
export const useTemplates = () => usePolled("/api/templates", parseTemplates, 60_000)
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
  chat_direct_message: "Send a chat message to a colleague",
  chat_post_message: "Post in a chat channel",
  chat_read_channel: "Read a chat channel",
  chat_read_messages: "Read chat messages from a colleague",
  edge_download_file: "Download a file from the intranet",
  edge_web_lookup: "Look up information on the web",
  excel_add_rows: "Add rows to an Excel workbook",
  excel_import_csv: "Turn a downloaded CSV into a workbook",
  excel_new_workbook: "Create an Excel workbook",
  explorer_copy_to_share: "Copy a file to a team share",
  explorer_extract_zip: "Unpack a ZIP archive",
  explorer_file_download: "Tidy up Downloads",
  explorer_new_folder: "Create a folder",
  explorer_recycle_file: "Recycle an old file",
  explorer_rename_move: "Rename and move a file",
  explorer_zip_file: "Pack a file into a ZIP archive",
  notepad_write_note: "Write a note in Notepad",
  outlook_answer_invite: "Answer a meeting invitation",
  outlook_file_mail: "File an email into a folder",
  outlook_read_mail: "Read an email",
  outlook_reply_mail: "Reply to an email",
  outlook_save_attachment: "Save an email attachment",
  outlook_send_attachment: "Send a file by email",
  outlook_send_invite: "Send a meeting invitation",
  outlook_send_mail: "Send an email",
  paint_quick_sketch: "Draw a sketch in Paint",
  powerpoint_short_deck: "Create a PowerPoint presentation",
  powershell_admin_check: "Check system health in PowerShell",
  word_comment_document: "Comment on a Word document",
  word_edit_document: "Edit a Word document",
  word_export_pdf: "Export a Word document as PDF",
  word_revise_and_mail: "Revise a Word document and email it",
  word_save_as_copy: "Save a copy of a Word document",
  word_short_document: "Write a Word document",
  wordpad_formatted_note: "Write a formatted note in WordPad"
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
