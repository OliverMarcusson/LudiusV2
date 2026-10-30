import {
  FileText,
  FolderOpen,
  Globe,
  Mail,
  MessagesSquare,
  NotebookPen,
  Palette,
  Presentation,
  Sheet,
  SquareTerminal,
  type LucideIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { type Template, activityName, humanize, useTemplates } from "@/lib/console";
import { PageHeading, SourceNote } from "./console";

const GROUPS: Record<string, { label: string; icon: LucideIcon }> = {
  outlook: { label: "Mail and calendar", icon: Mail },
  chat: { label: "Team chat", icon: MessagesSquare },
  word: { label: "Word", icon: FileText },
  excel: { label: "Excel", icon: Sheet },
  powerpoint: { label: "PowerPoint", icon: Presentation },
  notepad: { label: "Notepad", icon: NotebookPen },
  wordpad: { label: "WordPad", icon: FileText },
  paint: { label: "Paint", icon: Palette },
  explorer: { label: "Files", icon: FolderOpen },
  edge: { label: "Web", icon: Globe },
  powershell: { label: "PowerShell", icon: SquareTerminal },
};

// The task name's prefix groups chat (run in Edge) and word_revise_and_mail (ends in Outlook) by what a person would call them.
const groupOf = (t: Template) => t.task.split("_")[0];

// `about` is written for the planner: a plain sentence, then "param: ..." notes. Keep the sentence.
function summary(t: Template) {
  const cut = Math.min(
    ...t.params.map((p) => t.about.indexOf(`. ${p}:`) + 1).filter((i) => i > 0),
    t.about.length,
  );
  return t.about.slice(0, cut).trim();
}

export function Tasks() {
  const state = useTemplates();
  const templates = state.data ?? [];
  const groups = Object.keys(GROUPS)
    .concat([...new Set(templates.map(groupOf))].filter((g) => !GROUPS[g]))
    .map((g) => [g, templates.filter((t) => groupOf(t) === g)] as const)
    .filter(([, list]) => list.length);
  return (
    <div>
      <PageHeading title="Tasks">
        {templates.length
          ? `${templates.length} activities a persona can be planned to do on its workstation.`
          : "Activities a persona can be planned to do on its workstation."}
      </PageHeading>
      <SourceNote state={state} />
      <div className="flex flex-col gap-10">
        {groups.map(([g, list]) => {
          const { label, icon: Icon } = GROUPS[g] ?? { label: humanize(g), icon: FileText };
          return (
            <section key={g} aria-labelledby={`tasks-${g}`}>
              <h2
                id={`tasks-${g}`}
                className="mb-4 flex items-center gap-2 border-b pb-2 text-base font-semibold"
              >
                <Icon className="size-4 text-muted-foreground" aria-hidden />
                {label}
                <span className="font-normal text-muted-foreground tabular-nums">{list.length}</span>
              </h2>
              <ul className="grid gap-x-8 gap-y-5 md:grid-cols-2">
                {list.map((t) => (
                  <li key={t.task} className="flex flex-col gap-1">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                      <h3 className="font-medium">{activityName(t.task)}</h3>
                      {t.capability && <Badge variant="outline">{t.capability}</Badge>}
                    </div>
                    <p className="text-sm text-muted-foreground">{summary(t)}</p>
                    <p className="text-xs text-muted-foreground tabular-nums">
                      <code>{t.task}</code>
                      {t.budget_minutes && ` · up to ${t.budget_minutes} min`}
                      {t.params.length > 0 && ` · ${t.params.map(humanize).join(", ")}`}
                    </p>
                  </li>
                ))}
              </ul>
            </section>
          );
        })}
      </div>
    </div>
  );
}
