# LudiusV2 agent instructions

Read `Ludius – System Specification.md` first; it wins over this file. Coordination lives in the untracked `TASKS.md`; the tracker `docs/progress.html` is updated and published by the orchestrator only.

## Threat model (what safeguards defend against)

- The range is closed: persona VMs, the range server `ludius-srv01` (Samba AD `corp.ludius.test`, grommunio) and the host. The mail server refuses delivery outside `corp.ludius.test`.
- Plan content comes from our own planner LLM: fallible, not adversarial. Validate it at load (shape, recipients, names); don't defend against crafted attacks on our own pipeline.
- What must never happen: actions leaving the range; touching scenario-reserved paths, mailboxes or files (spec, Policy); destructive actions outside the noise folders; ground truth that claims an outcome that did not happen.
- A noise mistake that stays inside noise-only resources (a note in the wrong folder, a mail to the wrong noise colleague) is a failed task, not an incident: abandon, record it, move on.

## Where a safeguard goes

1. **Range boundary** (once): external delivery, network reachability, checkpoints.
2. **Shared layer** (once, reused): policy (reserved resources, destructive options), plan-load validation, observation honesty (unreadable is *unknown*, never empty; evidence must come from the window being acted on), shared send contract for anything that sends mail, budgets, outcome checks.
3. **Template**: goal, app, planner content, which shared check proves done, which irreversible effects it has. No per-template copies of shared rules.

Add a template-level guard only for an effect the shared layer does not cover yet; then move it into the shared layer when a second template needs it.

## "Done" contract for a new task type

- Declares what it may write and which effects can't be undone.
- Parameters validated at plan load.
- Bounded steps, and an abandon path.
- An outcome check outside the UI that tells *this* task's result from pre-existing state.
- One repeatable live run on the VM (compressed clock, escalation off, decision cap) with external evidence.

## Reviews

- State the acceptance contract (above + the change's goal) in the review brief.
- Blockers only for violations of the threat model, the spec or the done contract; batch them in one pass.
- Re-checks cover the fixes and risks they introduce; other ideas go to `TASKS.md` as backlog, not into the blocking list.
