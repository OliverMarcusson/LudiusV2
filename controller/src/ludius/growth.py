"""Template growth (spec section 6): the LLM proposes new templates from the capability catalog.

A proposal is checked in code (schema, catalog processes, placeholders, policy, completion checks) and saved
to data/templates/proposed/. It reaches day plans only after `ludius validate` admits it.
"""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Any

import yaml

from ludius.llm import LlmClient, LlmError
from ludius.tasks import Done, Template

GROWTH_SYSTEM = (
    "You design task templates for an agent that operates a Windows 10 desktop like an office worker, one UI "
    "action at a time, choosing among the controls visible on screen. A template is a skeleton of subgoals. "
    "Each subgoal goal is one short, concrete instruction the agent can finish in a few clicks or keystrokes. "
    "Completion checks read UI Automation state and must be true only once the subgoal really is done. "
    "Variants are different human ways to do the same task (keyboard shortcut vs menu, Start search vs "
    "taskbar). Every variant ends by closing the app so the desktop is left clean. Never delete files, change "
    "system settings, install software, or touch anything outside the persona's own documents. The agent can "
    "click, double-click, type prepared text, press keys and scroll; it cannot drag or use the clipboard, and "
    "copy_text only types text visible in the same window as the target field. A done check must be false "
    "when its subgoal starts."
)

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
COOKIE_CONSENT = "(?i)accept.*|reject.*"  # the only destructive-looking control a proposal may allowlist


def propose(
    llm: LlmClient, capabilities: dict[str, Any], templates: dict[str, Template], idea: str = ""
) -> tuple[Template, dict[str, Any]]:
    """One proposal; problems found in code go back to the model once."""
    examples = "\n---\n".join(
        yaml.safe_dump(t.model_dump(exclude_defaults=True), sort_keys=False, width=110) for t in templates.values()
    )
    user = (
        f"Installed apps and what people do with them:\n{yaml.safe_dump(capabilities['apps'], sort_keys=False)}\n"
        f"Completion checks available:\n{yaml.safe_dump(capabilities['checks'], sort_keys=False)}\n"
        f"Existing templates (do not duplicate their task; copy their style and check patterns):\n{examples}\n"
        f"Propose one new template{': ' + idea if idea else ' for a common office task not covered yet'}. "
        "Return it as one JSON object with the same keys as the existing templates (task, app, processes, "
        "params, defaults, about, example, variants, subgoals, budget_steps, budget_minutes). 2-4 variants. "
        "params are what a planner fills per instance; defaults are fixed values such as the Start search "
        "query; example gives a value for every param. Placeholders like {file_name} may appear in goals and "
        "check regexes. Every subgoal that is not open-ended needs a done check."
    )
    raw: dict[str, Any] = {}
    for attempt in range(2):
        t = llm.json(Template, GROWTH_SYSTEM, user, temperature=0.7)
        problems = check(t, capabilities, templates)
        if not problems:
            raw = t.model_dump(exclude_defaults=True)
            return t, raw
        if attempt:
            raise LlmError(f"proposal {t.task!r} rejected: {'; '.join(problems)}")
        user += "\n\nYour proposal was rejected:\n" + "\n".join(f"- {p}" for p in problems) + "\nSend a corrected one."
    raise AssertionError("unreachable")


def check(t: Template, capabilities: dict[str, Any], templates: dict[str, Template]) -> list[str]:
    problems: list[str] = []
    if not _NAME.match(t.task):
        problems.append("task must be snake_case")
    if t.task in templates:
        problems.append(f"task {t.task!r} already exists")
    try:
        t.check()
    except ValueError as e:
        problems.append(str(e))
        return problems
    if not 2 <= len(t.variants) <= 4:
        problems.append("needs 2-4 variants")
    allowed = {a["process"].lower() for a in capabilities["apps"]}
    if bad := [p for p in t.processes if p.lower() not in allowed]:
        problems.append(f"processes outside the catalog: {bad}")
    keys = set(t.params) | set(t.defaults)
    keys |= {f"{k}_typo" for k in keys}  # mistake variants, derived in Task
    if missing := [p for p in t.params if not t.example.get(p)]:
        problems.append(f"example lacks {missing}")
    open_ended = 0
    for name, sg in t.subgoals.items():
        if sg.text and sg.text not in keys:
            problems.append(f"{name}: text {sg.text!r} is not a param or default")
        if unknown := _placeholders(sg.goal, sg.done) - keys:
            problems.append(f"{name}: unknown placeholders {sorted(unknown)}")
        if extra := set(sg.allow_destructive) - {COOKIE_CONSENT}:
            problems.append(f"{name}: allow_destructive not permitted for proposals: {sorted(extra)}")
        if sg.done is None:
            open_ended += 1
        for rx in _regexes(sg.done):
            try:
                re.compile(rx.format_map(dict.fromkeys(keys, "x")))
            except (re.error, KeyError, ValueError, IndexError) as e:
                problems.append(f"{name}: bad check regex {rx!r}: {e}")
    if open_ended > 1:
        problems.append("at most one open-ended subgoal (without done)")
    for v in t.variants:
        last = t.subgoals[v[-1]].done
        if last is None or last.window is None or not last.window.absent:
            problems.append(f"variant {v} must end with a check that the app window is closed (window absent)")
    return problems


def _regexes(done: Done | None) -> list[str]:
    if done is None:
        return []
    out = [w.title for w in (done.window, done.foreground) if w is not None and w.title]
    if done.element:
        out += [x for x in (done.element.name, done.element.value) if x]
    return out


def _placeholders(goal: str, done: Done | None) -> set[str]:
    names: set[str] = set()
    for s in [goal, *_regexes(done)]:
        try:
            names |= {f for _, f, _, _ in string.Formatter().parse(s) if f}
        except ValueError:
            names.add("<unbalanced braces>")
    return names


def save(raw: dict[str, Any], proposed: Path) -> Path:
    proposed.mkdir(parents=True, exist_ok=True)
    path = proposed / f"{raw['task']}.yaml"
    body = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=110)
    path.write_text("# Proposed by the planner LLM; admitted only via `ludius validate`.\n" + body, encoding="utf-8")
    return path
