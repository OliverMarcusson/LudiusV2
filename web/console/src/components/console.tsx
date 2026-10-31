import { useEffect, useState, type ReactNode } from "react";
import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import LiveMonitor from "@/App";
import { CompanyLogo, LogoButton, Portrait, PortraitButton, PortraitStatus } from "./portraits";
import { ThemeButton } from "./theme";
import { Tasks } from "./tasks";
import {
  type Catalog,
  type Daybook,
  type Person,
  activityName,
  currentRun,
  dayLink,
  humanize,
  itemRecord,
  latestDay,
  personLink,
  useCatalog,
  useDaybook,
  useRun,
} from "@/lib/console";
import { type Polled, useInteractions, useNow } from "@/lib/feeds";

function useRoute() {
  const read = () => {
    try {
      return window.location.hash.slice(2).split("/").map(decodeURIComponent);
    } catch {
      return ["invalid"];
    }
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function SourceNote({ state }: { state: Polled<unknown> }) {
  if (state.status === "ok") return null;
  return (
    <p
      role="status"
      className={cn(
        "mb-4 rounded-md border px-3 py-2 text-sm",
        state.status === "loading"
          ? "border-border text-muted-foreground"
          : "border-stale/40 bg-stale/10 text-foreground",
      )}
    >
      {state.status === "loading"
        ? "Reading range records…"
        : `${state.status === "stale" ? `Showing records from ${new Date(state.at!).toLocaleTimeString()}. ` : "Records unavailable. "}${state.error}. Retrying automatically.`}
    </p>
  );
}

export function PageHeading({
  title,
  children,
}: {
  title: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="mb-8">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      {children && (
        <p className="mt-1 text-sm text-muted-foreground">{children}</p>
      )}
    </div>
  );
}

function Section({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="mb-4 flex items-baseline justify-between gap-4 border-b pb-2">
        <h2 className="text-base font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function rise(i: number) {
  return {
    initial: { opacity: 0, y: 6 },
    animate: { opacity: 1, y: 0 },
    transition: {
      delay: Math.min(i, 16) * 0.025,
      duration: 0.22,
      ease: "easeOut" as const,
    },
  };
}

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "status", label: "Status" },
  { key: "company", label: "Company" },
  { key: "people", label: "People" },
  { key: "daybook", label: "Daybook" },
  { key: "tasks", label: "Tasks" },
];

export default function Console() {
  const route = useRoute();
  const state = useCatalog();
  const catalog = state.data;
  const view = route[0] || "overview";
  const active = view === "vm" ? "overview" : view;
  const pageKey = active === "people" ? `people/${route[1] ?? ""}` : active;
  return (
    <MotionConfig reducedMotion="user">
      <div className="flex min-h-svh flex-col">
        <a
          href="#content"
          className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
          onClick={(e) => {
            e.preventDefault();
            document.getElementById("content")?.focus();
          }}
        >
          Skip to content
        </a>
        <header
          className="sticky top-0 z-40 flex h-12 items-stretch gap-6 border-b bg-background px-4 sm:px-6"
          aria-label="Console"
        >
          <a
            className="flex items-center gap-2 font-semibold"
            href="#/overview"
          >
            <svg
              className="size-5"
              viewBox="0 0 40 40"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M5 7h20v15H5zM15 17h20v16H15z"
                stroke="currentColor"
                strokeWidth="2.5"
              />
              <path
                d="M5 12h20M15 22h20"
                stroke="currentColor"
                strokeWidth="2"
              />
            </svg>
            <span>Ludius</span>
          </a>
          <nav
            aria-label="Main navigation"
            className="flex items-stretch gap-1 overflow-x-auto overflow-y-hidden [scrollbar-width:none]"
          >
            {TABS.map(({ key, label }) => (
              <a
                key={key}
                href={`#/${key}`}
                aria-current={active === key ? "page" : undefined}
                className={cn(
                  "relative flex items-center px-2 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  active === key
                    ? "text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {label}
                {active === key && (
                  <motion.span
                    layoutId="tab-underline"
                    className="absolute inset-x-0 bottom-0 h-0.5 bg-foreground"
                    transition={{ type: "spring", stiffness: 500, damping: 40 }}
                  />
                )}
              </a>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <span className="hidden text-xs text-muted-foreground sm:inline">
              Read only
            </span>
            <ThemeButton />
          </div>
        </header>
        <main
          id="content"
          tabIndex={-1}
          className="flex-1 px-4 py-6 outline-none sm:px-6 sm:py-8"
        >
          <div
            className={cn(
              "mx-auto w-full",
              active !== "overview" && "max-w-7xl",
            )}
          >
            <SourceNote state={state} />
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={pageKey}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.18, ease: "easeOut" }}
              >
                {view === "overview" || view === "vm" ? (
                  <LiveMonitor catalog={catalog} />
                ) : view === "status" ? (
                  <Status catalog={catalog} />
                ) : view === "company" ? (
                  <Company catalog={catalog} />
                ) : view === "people" ? (
                  <People catalog={catalog} user={route[1]} />
                ) : view === "tasks" ? (
                  <Tasks />
                ) : view === "daybook" ? (
                  <DaybookPicker
                    catalog={catalog}
                    date={route[1]}
                    user={route[2]}
                  />
                ) : (
                  <p>
                    Page not found.{" "}
                    <a className="link" href="#/overview">
                      Return to Overview
                    </a>
                    .
                  </p>
                )}
              </motion.div>
            </AnimatePresence>
          </div>
        </main>
        <footer className="border-t px-4 py-3 text-xs text-muted-foreground sm:px-6">
          <div
            className={cn(
              "mx-auto w-full",
              active !== "overview" && "max-w-7xl",
            )}
          >
            Ludius / {catalog?.company?.company ?? "Training range"}
          </div>
        </footer>
      </div>
    </MotionConfig>
  );
}

const STEPS = [
  { key: "planning", label: "Planning", note: "Writing the day's plans" },
  { key: "preparing", label: "Preparing", note: "Clock jumped, desktops starting" },
  { key: "running", label: "Running", note: "Personas working through the day" },
] as const;
const ENDED: Record<string, string> = {
  finished: "The warp finished and the range is back on real time.",
  stopped: "The warp was stopped and the range is back on real time.",
  failed: "The warp failed.",
};

function Status({ catalog }: { catalog: Catalog | null }) {
  const state = useRun();
  const now = useNow(1000);
  const run = state.data;
  const names = new Map(catalog?.people?.map((p) => [p.user, p]) ?? []);
  const step = STEPS.findIndex((s) => s.key === run?.phase);
  const logical =
    run?.clock && state.at !== null
      ? new Date(run.clock.ms + (now - state.at) + run.clock.offset * 60_000)
      : null;
  if (!run) return <PageHeading title="Status" />;
  if (run.phase === "none")
    return (
      <PageHeading title="Status">
        No warp recorded yet. Start one with ludius range warp.
      </PageHeading>
    );
  return (
    <div className="space-y-12">
      <header className="flex flex-wrap items-end justify-between gap-8">
        <div>
          <p className="text-sm text-muted-foreground tabular-nums">
            Simulated day {run.day}
            {run.date && ` / ${run.date}`}
          </p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight">
            {step >= 0 ? STEPS[step].label : humanize(run.phase)}
          </h1>
        </div>
        {logical && (
          <div className="text-right">
            <p className="text-sm text-muted-foreground">Range clock</p>
            <p className="font-mono text-4xl font-semibold tabular-nums">
              {logical.toISOString().slice(11, 16)}
              <motion.span
                key={logical.getUTCSeconds()}
                initial={{ opacity: 0.3 }}
                animate={{ opacity: 1 }}
                className="text-muted-foreground"
              >
                :{logical.toISOString().slice(17, 19)}
              </motion.span>
            </p>
            <p className="text-xs text-muted-foreground tabular-nums">
              {logical.toISOString().slice(0, 10)}
            </p>
          </div>
        )}
      </header>
      {step >= 0 ? (
        <ol className="grid gap-3 sm:grid-cols-3">
          {STEPS.map((s, i) => (
            <li
              key={s.key}
              aria-current={i === step ? "step" : undefined}
              className={cn(
                "relative overflow-hidden rounded-lg border p-4",
                i > step && "text-muted-foreground",
              )}
            >
              {i === step && (
                <motion.div
                  layoutId="run-step"
                  className="absolute inset-0 bg-accent"
                  transition={{ type: "spring", stiffness: 300, damping: 35 }}
                />
              )}
              <div className="relative flex items-center gap-2 font-medium">
                <span className="relative flex size-2.5">
                  {i === step && (
                    <motion.span
                      className="absolute inset-0 rounded-full bg-foreground"
                      animate={{ scale: [1, 2.2], opacity: [0.6, 0] }}
                      transition={{ duration: 1.4, repeat: Infinity }}
                    />
                  )}
                  <span
                    className={cn(
                      "relative size-2.5 rounded-full",
                      i <= step
                        ? "bg-foreground"
                        : "border border-muted-foreground",
                    )}
                  />
                </span>
                {s.label}
              </div>
              <p className="relative mt-1 text-sm text-muted-foreground">
                {s.key === "planning" && run.phase === "planning"
                  ? `${run.plans ?? 0} of ${names.size || "?"} plans written`
                  : s.note}
              </p>
            </li>
          ))}
        </ol>
      ) : (
        <p
          role="status"
          className={cn(
            "rounded-md border px-3 py-2 text-sm",
            run.phase === "failed" && "border-lost/40 bg-lost/10",
          )}
        >
          {ENDED[run.phase]}
          {run.phase === "failed" && run.error && (
            <span className="mt-1 block font-mono text-xs break-words">
              {run.error}
            </span>
          )}
        </p>
      )}
      {!!run.unplanned?.length && (
        <p className="text-sm text-stale">
          No plan for{" "}
          {run.unplanned.map((u) => names.get(u)?.name ?? u).join(", ")}.
        </p>
      )}
      {!!run.personas?.length && (
        <Section title="Personas">
          <ul className="grid gap-x-12 gap-y-4 md:grid-cols-2">
            {run.personas.map((p, i) => (
              <motion.li
                key={p.user}
                {...rise(i)}
                className="flex items-center gap-3"
              >
                <Portrait
                  person={names.get(p.user) ?? { user: p.user, name: p.user }}
                  className="size-9"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex justify-between gap-3 text-sm">
                    <span className="truncate font-medium">
                      {names.get(p.user)?.name ?? p.user}
                    </span>
                    <span className="text-muted-foreground tabular-nums">
                      {p.status === "waiting"
                        ? "Waiting"
                        : p.status === "failed"
                          ? "Failed"
                          : `${p.done} / ${p.items}`}
                    </span>
                  </div>
                  <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted">
                    <motion.div
                      className={cn(
                        "h-full rounded-full",
                        p.status === "failed" ? "bg-lost" : "bg-foreground",
                      )}
                      initial={false}
                      animate={{
                        width: `${p.items ? Math.min(100, (p.done / p.items) * 100) : 0}%`,
                      }}
                      transition={{ duration: 0.6, ease: "easeOut" }}
                    />
                  </div>
                </div>
              </motion.li>
            ))}
          </ul>
        </Section>
      )}
      <p className="text-xs text-muted-foreground">
        Last journal entry {Math.round((run.ageMs ?? 0) / 60_000)} min ago.
      </p>
    </div>
  );
}

function Timeline({ entries }: { entries: { date: string; what: string }[] }) {
  return (
    <ol className="gap-12 text-sm md:columns-2">
      {entries.map((e, i) => (
        <motion.li
          key={i}
          {...rise(i)}
          className="grid break-inside-avoid grid-cols-[6.5rem_1fr] gap-3 border-b py-2.5"
        >
          <time className="text-muted-foreground tabular-nums">{e.date}</time>
          <p>{e.what}</p>
        </motion.li>
      ))}
    </ol>
  );
}

function Company({ catalog }: { catalog: Catalog | null }) {
  const company = catalog?.company;
  if (!company)
    return (
      <PageHeading title="Company">
        The company narrative is not available. Configure the console to use the
        controller’s runs directory.
      </PageHeading>
    );
  return (
    <div className="space-y-12">
      <header className="grid gap-x-16 gap-y-8 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:items-start">
        <div>
          <p className="text-sm text-muted-foreground">
            {company.city}
            {company.revised && ` / Story revised ${company.revised}`}
          </p>
          <div className="mt-1 flex items-center gap-4">
            <CompanyLogo className="size-16 shrink-0 object-contain" />
            <h1 className="text-3xl font-semibold tracking-tight">
              {company.company}
            </h1>
          </div>
          <p className="mt-5 max-w-prose text-base leading-relaxed text-foreground/90">
            {company.summary}
          </p>
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <LogoButton />
            <PortraitStatus />
          </div>
        </div>
        <section className="rounded-lg border bg-card p-5">
          <h2 className="mb-3 text-sm font-semibold">Projects</h2>
          <ul className="divide-y text-sm">
            {company.projects.map((project, i) => (
              <motion.li
                key={project}
                {...rise(i)}
                className="py-2 first:pt-0 last:pb-0"
              >
                {project}
              </motion.li>
            ))}
          </ul>
        </section>
      </header>
      <Section title="Company calendar">
        <Timeline entries={company.events} />
      </Section>
      <Section
        title="Organisation"
        action={
          <a className="link text-sm" href="#/people">
            Explore persona traits
          </a>
        }
      >
        <Organization people={company.people} />
      </Section>
      {company.history.length > 0 && (
        <Section title="How the story has changed">
          <Timeline entries={company.history} />
        </Section>
      )}
    </div>
  );
}

function Organization({ people }: { people: Person[] }) {
  function branch(person: Person, ancestors: string[] = []) {
    const reports = people.filter(
      (p) => p.manager === person.user && !ancestors.includes(p.user),
    );
    return (
      <li key={person.user}>
        <div className="flex items-center gap-3 py-1.5">
          <Portrait person={person} />
          <div className="min-w-0 text-sm">
            <a className="link" href={personLink(person.user)}>
              {person.name}
            </a>
            <p className="text-muted-foreground">
              {person.role} / {person.department}
              <span className="text-xs">
                {" / "}
                {reports.length
                  ? `Manager (${reports.length} direct ${reports.length === 1 ? "report" : "reports"})`
                  : "No direct reports"}
              </span>
            </p>
          </div>
        </div>
        {reports.length > 0 && (
          <ul
            className={
              ancestors.length === 0
                ? "mt-3 grid items-start gap-x-10 gap-y-4 md:grid-cols-2 xl:grid-cols-3"
                : "ml-4 border-l pl-5"
            }
          >
            {reports.map((p) => branch(p, [...ancestors, person.user]))}
          </ul>
        )}
      </li>
    );
  }
  const roots = people.filter(
    (p) => !p.manager || !people.some((manager) => manager.user === p.manager),
  );
  return <ul>{roots.map((p) => branch(p))}</ul>;
}

function ProfileHeader({
  person,
}: {
  person: Pick<Person, "user" | "name" | "role" | "department">;
}) {
  return (
    <header className="mb-6 flex flex-wrap items-center gap-4">
      <Portrait person={person} className="size-16 text-lg" />
      <div className="min-w-0">
        <h1 className="text-xl font-semibold">{person.name}</h1>
        <p className="text-sm text-muted-foreground">
          {person.role} / {person.department}
        </p>
      </div>
      <div className="flex gap-2 sm:ml-auto">
        <Button asChild variant="outline" size="sm">
          <a href="#/people">All people</a>
        </Button>
        <PortraitButton person={person} />
      </div>
      <div className="basis-full empty:hidden">
        <PortraitStatus />
      </div>
    </header>
  );
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[8rem_1fr] gap-3 py-2 first:pt-0 last:pb-0">
      <dt className="text-muted-foreground">{term}</dt>
      <dd className="tabular-nums">{children}</dd>
    </div>
  );
}

function People({ catalog, user }: { catalog: Catalog | null; user?: string }) {
  const [search, setSearch] = useState("");
  const people = catalog?.people;
  const selected = people?.find((p) => p.user === user);
  const colleague = catalog?.company?.people.find((p) => p.user === user);
  const manager = catalog?.company?.people.find(
    (p) => p.user === (colleague?.manager ?? selected?.manager),
  );
  const typingMs = Number(selected?.typing_mean_ms);
  const matches = (p: Person) =>
    `${p.name} ${p.role} ${p.department}`
      .toLowerCase()
      .includes(search.toLowerCase());
  if (user && !selected)
    return (
      <section>
        {colleague ? (
          <ProfileHeader person={colleague} />
        ) : (
          <PageHeading title="Profile unavailable" />
        )}
        <p className="text-sm">
          {colleague
            ? "This colleague appears in the narrative and has no active persona profile."
            : "No persona profile was found for this account."}
        </p>
        {!colleague && (
          <a className="link mt-2 inline-block text-sm" href="#/people">
            All people
          </a>
        )}
      </section>
    );
  if (selected) {
    const latest = catalog && latestDay(catalog, selected.user);
    return (
      <section>
        <ProfileHeader person={selected} />
        <div className="grid items-start gap-4 lg:grid-cols-[3fr_2fr]">
          <Card>
            <CardHeader>
              <CardTitle>
                <h2>Style & habits</h2>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              <p className="max-w-prose leading-relaxed">
                {selected.style || "No style recorded."}
              </p>
              <div>
                <h3 className="mb-1 text-sm font-semibold">
                  Role-appropriate signal
                </h3>
                <p className="text-sm">
                  {selected.risk_profile?.map(humanize).join(", ") ||
                    "No signal-like activities configured."}
                </p>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>
                <h2>At their desk</h2>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="divide-y text-sm">
                <Fact term="Account">{selected.user}</Fact>
                <Fact term="Usual hours">
                  {selected.hours?.join("–") ?? "Not recorded"}
                </Fact>
                <Fact term="Time zone">{selected.tz ?? "Not recorded"}</Fact>
                <Fact term="Typing pace">
                  {Number.isFinite(typingMs) && typingMs > 0
                    ? `${Math.round(60000 / (typingMs * 5))} WPM`
                    : "Not recorded"}
                </Fact>
                <Fact term="Thinking pause">
                  {selected.think_mean_s
                    ? `${selected.think_mean_s} s on average`
                    : "Not recorded"}
                </Fact>
                <Fact term="Manager">
                  {manager ? (
                    <a className="link" href={personLink(manager.user)}>
                      {manager.name}
                    </a>
                  ) : (
                    "Not recorded"
                  )}
                </Fact>
              </dl>
              {latest && (
                <Button asChild size="sm" className="mt-5">
                  <a href={dayLink(latest, selected.user)}>
                    Read their latest day
                  </a>
                </Button>
              )}
            </CardContent>
          </Card>
        </div>
      </section>
    );
  }
  return (
    <section>
      <PageHeading title="People" />
      <label className="mb-5 flex max-w-sm flex-col gap-1.5 text-sm font-medium">
        Find a person
        <Input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Name, role or department"
        />
      </label>
      {!people ? (
        <p className="text-sm">Persona profiles are unavailable.</p>
      ) : (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4">
          <AnimatePresence mode="popLayout" initial={false}>
            {people.filter(matches).map((person, i) => (
              <motion.a
                key={person.user}
                layout
                {...rise(i)}
                exit={{
                  opacity: 0,
                  scale: 0.97,
                  transition: { duration: 0.12 },
                }}
                href={personLink(person.user)}
                className="group rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Card
                  size="sm"
                  className="h-full transition-colors group-hover:ring-foreground/40"
                >
                  <CardContent className="text-sm">
                    <Portrait
                      person={person}
                      className="aspect-square size-auto w-full text-3xl"
                    />
                    <h2 className="mt-3 truncate font-semibold">
                      {person.name}
                    </h2>
                    <p className="truncate">{person.role}</p>
                    <p className="truncate text-muted-foreground tabular-nums">
                      {person.department} / {person.hours?.join("–")}
                    </p>
                  </CardContent>
                </Card>
              </motion.a>
            ))}
          </AnimatePresence>
        </div>
      )}
      {people && !people.some(matches) && (
        <p className="text-sm">No people match this search.</p>
      )}
    </section>
  );
}

function DaybookPicker({
  catalog,
  date,
  user,
}: {
  catalog: Catalog | null;
  date?: string;
  user?: string;
}) {
  const dates = [...new Set(catalog?.index.map((i) => i.date) ?? [])]
    .sort()
    .reverse();
  const selectedDate = date ?? dates[0];
  const users = [
    ...new Set(
      catalog?.index
        .filter((i) => i.date === selectedDate)
        .map((i) => i.user) ?? [],
    ),
  ].sort();
  const selectedUser = user ?? users[0];
  return (
    <div>
      <PageHeading title="Daybook" />
      {dates.length > 0 && (
        <div className="mb-5 flex flex-wrap items-end gap-4 text-sm">
          <div className="flex flex-col gap-1.5">
            <span id="daybook-date" className="font-medium">
              Date
            </span>
            <Select
              value={selectedDate}
              onValueChange={(d) => {
                const u = catalog?.index.find((i) => i.date === d)?.user;
                if (u) window.location.hash = dayLink(d, u).slice(1);
              }}
            >
              <SelectTrigger
                aria-labelledby="daybook-date"
                className="min-w-36 tabular-nums"
              >
                <SelectValue placeholder="Choose a date" />
              </SelectTrigger>
              <SelectContent>
                {dates.map((d) => (
                  <SelectItem key={d} value={d} className="tabular-nums">
                    {d}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <span id="daybook-person" className="font-medium">
              Person
            </span>
            <Select
              value={selectedUser}
              onValueChange={(u) => {
                if (selectedDate)
                  window.location.hash = dayLink(selectedDate, u).slice(1);
              }}
            >
              <SelectTrigger
                aria-labelledby="daybook-person"
                className="min-w-48"
              >
                <SelectValue placeholder="Choose a person" />
              </SelectTrigger>
              <SelectContent>
                {users.map((u) => (
                  <SelectItem value={u} key={u}>
                    {catalog?.people?.find((p) => p.user === u)?.name ?? u}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {selectedUser && (
            <a className="link pb-1.5" href={personLink(selectedUser)}>
              Persona profile
            </a>
          )}
        </div>
      )}
      {selectedDate && selectedUser ? (
        <DaybookView
          key={`${selectedDate}/${selectedUser}`}
          date={selectedDate}
          user={selectedUser}
          catalog={catalog}
        />
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>
              <h2>No days available</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm">
            {!catalog ||
            catalog.sources.plans !== "ok" ||
            catalog.sources.days !== "ok"
              ? "Plan or recording directories are unavailable. Check the console’s runs directory configuration."
              : "The runs directory has no day plans or recordings yet. They appear here when the controller writes a day."}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function outcomeVariant(status: string | undefined) {
  return status === "done"
    ? "live"
    : status === "abandoned" || status === "failed"
      ? "lost"
      : status === "unfinished"
        ? "stale"
        : "outline";
}

function DaybookView({
  date,
  user,
  catalog,
}: {
  date: string;
  user: string;
  catalog: Catalog | null;
}) {
  const state = useDaybook(date, user);
  const interactions = useInteractions();
  const day = state.data;
  const [onlyProblems, setOnlyProblems] = useState(false);
  if (!day) return <SourceNote state={state} />;
  const events = currentRun(day.events);
  const ended = events?.some((e) => e.event === "day_end");
  const aborted = events?.some((e) => e.event === "day_aborted");
  const results = events?.filter((e) => e.event === "item_end");
  const done = results?.filter((e) => e.status === "done").length;
  const abandoned = results?.filter((e) => e.status === "abandoned").length;
  const connections = interactions.data?.filter(
    (i) => i.logical?.startsWith(date) && (i.from === user || i.to === user),
  );
  return (
    <>
      <SourceNote state={state} />
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border bg-card px-4 py-2.5 text-sm tabular-nums">
        <strong className="font-semibold">
          {date} / {user}
        </strong>
        <Badge
          variant={
            events === null
              ? "stale"
              : aborted
                ? "lost"
                : ended
                  ? "secondary"
                  : "outline"
          }
        >
          {events === null
            ? "Recording unavailable"
            : aborted
              ? "Day aborted"
              : ended
                ? "Day ended"
                : "No day-end recorded"}
        </Badge>
        <span className={cn(!results && "text-stale")}>
          {results
            ? `${done} done, ${abandoned} abandoned, ${results.filter((e) => e.status === "failed").length} failed`
            : "Outcomes unknown"}
        </span>
        <span className="text-muted-foreground">
          {events?.find((e) => e.event === "day_start")?.clock ??
            "Clock mode unknown"}
        </span>
      </div>
      <div className="mt-6 mb-3 flex items-center justify-between gap-4">
        <h2 className="text-base font-semibold">Intent & outcome</h2>
        <Button
          variant="outline"
          size="sm"
          aria-pressed={onlyProblems}
          onClick={() => setOnlyProblems((v) => !v)}
        >
          {onlyProblems ? "Show every item" : "Show unresolved items"}
        </Button>
      </div>
      <div
        className="hidden grid-cols-2 gap-6 px-4 pb-2 text-xs font-medium text-muted-foreground md:grid"
        aria-hidden="true"
      >
        <span>Planned activity</span>
        <span>Recorded result</span>
      </div>
      {day.plan === null ? (
        <p className="rounded-md border border-stale/40 bg-stale/10 px-3 py-2 text-sm">
          Plan unavailable. Any available recording is listed below.
        </p>
      ) : (
        <div className="divide-y rounded-lg border bg-card">
          {day.plan
            .filter(
              (item) =>
                !onlyProblems || itemRecord(item, events).status !== "done",
            )
            .map((item, i) => {
              const record = itemRecord(item, events);
              const { start, end } = record;
              const evidence =
                start < 0
                  ? []
                  : events!.slice(start, end < 0 ? undefined : end + 1);
              const reason = evidence.findLast(
                (e) => e.event === "task_end" && e.task === item.template,
              )?.reason;
              return (
                <motion.article
                  {...rise(i)}
                  className="grid gap-3 px-4 py-3 text-sm md:grid-cols-2 md:gap-6"
                  key={`${item.at}/${item.template}/${i}`}
                >
                  <div className="grid grid-cols-[3.5rem_1fr] gap-3">
                    <time className="text-muted-foreground tabular-nums">
                      {item.at}
                    </time>
                    <div className="min-w-0">
                      <h3 className="font-semibold">
                        {activityName(item.template)}
                      </h3>
                      <p className="text-muted-foreground">
                        {item.context || "No context recorded."}
                      </p>
                      {item.interrupt === "true" && (
                        <Badge variant="secondary" className="mt-1.5">
                          Interruption
                        </Badge>
                      )}
                      {item.handoff && (
                        <p className="mt-1.5 text-xs">
                          Planned handoff to{" "}
                          <a className="link" href={personLink(item.handoff)}>
                            {catalog?.company?.people.find(
                              (p) => p.user === item.handoff,
                            )?.name ?? item.handoff}
                          </a>
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="min-w-0 space-y-1.5 max-md:pl-[4.25rem]">
                    <Badge variant={outcomeVariant(record.status)}>
                      {record.status === "done"
                        ? "Recorded done"
                        : record.status === "unrecorded"
                          ? "No matching item recorded"
                          : record.status === "unfinished"
                            ? "Started; no outcome recorded"
                            : record.status === "unknown"
                              ? "Outcome unknown"
                              : record.status === "abandoned"
                                ? "Task stopped"
                                : humanize(record.status ?? "unknown")}
                    </Badge>
                    {reason && <p>{describeReason(reason)}</p>}
                    {record.attempts > 1 && (
                      <p className="text-muted-foreground tabular-nums">
                        {record.attempts} recorded attempts
                      </p>
                    )}
                    {evidence.length > 0 && (
                      <details className="group">
                        <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
                          Read {evidence.length} records for this attempt
                        </summary>
                        <div className="mt-2">
                          <EventList day={{ ...day, events: evidence }} />
                        </div>
                      </details>
                    )}
                  </div>
                </motion.article>
              );
            })}
        </div>
      )}
      {day.plan?.length === 0 && (
        <p className="py-3 text-sm">No items in this plan.</p>
      )}
      {onlyProblems &&
        day.plan?.length !== 0 &&
        day.plan?.every(
          (item) => itemRecord(item, events).status === "done",
        ) && (
          <p className="py-5 text-sm text-muted-foreground">
            Every planned item has a recorded outcome of done.
          </p>
        )}
      <Card className="mt-6">
        <CardHeader>
          <CardTitle>
            <h2>Verified connections</h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm">
          <SourceNote state={interactions} />
          {connections?.length ? (
            <ul className="divide-y">
              {connections.map((i) => (
                <li key={i.id} className="py-2 first:pt-0 last:pb-0">
                  <a className="link" href={personLink(i.from)}>
                    {i.from}
                  </a>{" "}
                  / {humanize(i.kind)} /{" "}
                  <a className="link" href={dayLink(date, i.to)}>
                    {i.to}
                  </a>
                  <span className="block text-muted-foreground">{i.label}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p>No matching connections in the available recent feed.</p>
          )}
        </CardContent>
      </Card>
      <details className="mt-4 rounded-lg border bg-card text-sm">
        <summary className="cursor-pointer px-4 py-3 font-semibold">
          Controller record / {day.events?.length ?? "unknown"} events
        </summary>
        <div className="border-t px-4 py-3">
          <p className="mb-3 text-muted-foreground">
            Host timestamps are separate from the logical timeline. Records omit
            screen text and message bodies. Repeated attempts and items missing
            from the plan remain visible here.
          </p>
          <EventList day={day} />
        </div>
      </details>
    </>
  );
}

function describeReason(reason: string): string {
  const [stage, ...parts] = reason.split(":");
  const cause = parts.join(":").trim();
  const stages: Record<string, string> = {
    search: "searching for the requested item",
    send_button: "finding the Send button",
    open_message: "opening the message",
    confirm_save: "confirming the file save",
    save_as: "saving the file",
    reply: "preparing the reply",
  };
  const activity =
    stages[stage.trim()] ?? `working on “${humanize(stage.trim())}”`;
  if (cause === "step budget" || reason === "step budget")
    return reason === "step budget"
      ? "Stopped after reaching the action limit for this task."
      : `Stopped after reaching the action limit while ${activity}.`;
  if (cause === "stuck" || cause === "stagnation")
    return `Stopped because no progress was detected while ${activity}.`;
  if (cause.startsWith("abandon"))
    return `Stopped while ${activity}: ${humanize(cause)}.`;
  return humanize(reason);
}

function EventList({ day }: { day: Daybook }) {
  if (day.events === null) return <p>Recording unavailable.</p>;
  if (!day.events.length) return <p>No complete events recorded.</p>;
  return (
    <ol className="divide-y text-xs">
      {day.events.map((e, i) => (
        <li
          key={i}
          className="grid gap-x-3 py-1.5 sm:grid-cols-[11rem_9rem_1fr]"
        >
          <time
            title="Host time"
            className="text-muted-foreground tabular-nums"
          >
            {e.host_ms
              ? new Date(e.host_ms).toLocaleString()
              : "Host time unknown"}
          </time>
          <strong className="font-medium">{humanize(e.event)}</strong>
          <span className="break-words text-muted-foreground tabular-nums">
            {[
              e.logical,
              e.template ?? e.task,
              e.subgoal,
              e.status,
              e.reason,
              e.by && `checked by ${e.by}`,
            ]
              .filter(Boolean)
              .join(" / ")}
          </span>
        </li>
      ))}
    </ol>
  );
}
