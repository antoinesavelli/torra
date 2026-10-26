# Migrating off Claude Code as the main operator

Status: active

Owner decisions pending: none

Last verified: 61b6886 on 2026-10-21

Written 2026-09-03. The operator's goal: stop leaning on Claude Code (or any
paid hosted model) as the day-to-day driver across these machines, using
torra instead. The gap breaks into four buckets. One is built (this
change); the other three are scoped here for whoever picks them up next --
this is a roadmap, not an implementation.

## Why a phased approach, not a big-bang expansion

torra's own AGENTS.md already has a rule for this ("before adding any
feature," and "fix the cause, not the check"'s sibling: don't relax scope to
get somewhere faster). Bolting on "everything Claude does" at once would
mean skipping the four-question gate (does an existing gate cover this, does
it add a second code path, can you measure it before building it, does it
grow an open-ended detection list) for a dozen features at once. Each
bucket below gets that gate applied on its own, when it's actually built.

## Bucket 1 -- general coding/refactor loop (BUILT this change)

**Gap:** torra's pipeline is single-shot plan-then-execute. Claude Code's
edit loop is iterative: edit, run tests, read the failure, edit again. That
loop didn't exist here before this change.

**What shipped:** `torra/iterate.py`'s `iterate()`, wired into
`torra ask --verify "<check>" [--max-iterations N]` (default 3). See
README.md's "`ask --verify`" section for usage.

**Design constraint that shaped it:** torra's AGENTS.md gates
`Torra.execute`, the five gates, and `Step.command`/`Step.run`
construction as ask-first, safety-critical path. A naive implementation
(one plan with an edit step followed by a test step) would also fight
ROLLBACK_ON_FAIL: a failing test step in the *same* plan triggers
`_sweep_rollback`, which would undo the edit you actually want to keep and
iterate on. The actual design avoids both problems by never touching that
path: `iterate()` composes ordinary, already-public `Torra.plan()` /
`Torra.execute()` calls into a bounded loop of separate rounds --
edit round, verify round, (fix round, verify round)* -- exactly the
sequence of turns a human retyping "that failed, fix it" after reading the
real output would produce by hand. Nothing here is a second way to run a
command; nothing here required touching a single line the ask-first list
names. It only lives in `iterate.py`/`cli.py`, both peripheral-tier.

**Known limitation, not fixed here:** a NEEDS_ANSWER (clarifying question)
outcome on any round is treated as a stop condition, not something the
loop resolves. `cmd_ask`'s existing single-round path already loops on
NEEDS_ANSWER via `_collect_answers`; teaching `iterate()` the same trick
would mean either duplicating that loop or extracting it into something
both paths share. Left alone deliberately -- this is exactly the kind of
scope creep the four-question gate exists to catch, and it wasn't measured
as a real problem yet (a vague verify/fix request is more likely to trip
the Clarifier than a well-anchored one). Revisit only if it's actually hit
in practice, with a real example in hand, not speculatively.

## Bucket 2 -- OS/ops tasks (no build needed, habit change only)

**Gap:** none, in principle. torra already owns this: shell commands,
file edits (with real snapshot/undo), service/package management, deploys,
even a second-machine SSH link (`torra/core/machine.py`'s
`observe_executor`, `torra/core/remote_edit.py`'s pull-edit-push). This is
explicitly its stated scope -- "an OS-engineering agent first, a chat UI
second" (`AGENTS.md`).

**What's actually needed:** switching habit, not code. When the operator
finds themselves asking Claude Code to run a deploy, restart a service,
edit a systemd unit, or touch anything package/filesystem-level on this
box, that's a `torra ask "..."` call instead. Nothing here needs a
`CLAUDE_MIGRATION_ROADMAP.md` action item beyond naming it -- if a *specific*
task type keeps getting routed to Claude Code instead of torra because
torra genuinely can't do it, that's a concrete, measurable gap worth its
own entry the next time this doc is revisited (see the four-question gate:
measure before building).

## Bucket 3 -- memory/research continuity (not built, scoped here)

**Gap:** Claude Code carries cross-session project memory (this repo's own
`~/.claude/projects/<path>/memory/`: `user`/`feedback`/`project`/`reference`
typed files plus an `MEMORY.md` index) that persists decisions, dead ends,
and project context across sessions. torra has an analogous but narrower
mechanism -- the "site-knowledge library" (`docs/*.md`, a topic index fed to
the planning model every turn, see README.md's "Site-knowledge library"
section) -- but that's OS/machine domain knowledge, gated on a measured
benchmark gap (`scripts/dev/model_bench/`), not a place for "we tried X,
it failed, don't reopen it" or "the operator prefers Y."

**What a build here would look like**, if picked up:
1. A new file kind under `docs/` (or a sibling directory, if `docs/`'s own
   measured-gap discipline shouldn't apply to it) holding session-scale
   decisions/dead-ends, analogous to the vault's `project`/`feedback` types.
2. A way for the *model* to write to it, not just read it -- `read_memory.py`
   already handles "a completed read-only step's real output entering the
   model's context, tagged by path"; a memory write is a different shape
   (durable across sessions, not scoped to one conversation's context).
3. Apply the four-question gate before wiring it into `SYSTEM_PROMPT` --
   `docs/README.md`'s own playbook already says not to do that step in the
   same change that adds the source file (see docs/agents/PLAYBOOKS.md's "Adding OS domain
   knowledge to `docs/`" playbook, item 4).

**Not started.** No file exists for this yet; this section is the design
starting point, not a stub implementation.

## Bucket 4 -- ecosystem conveniences (not built, lowest priority)

**Gap:** subagents (parallel delegated work), Skills (packaged procedures),
Artifacts (published interactive pages), MCP connectors (Gmail/Drive/
Calendar-style external tool access), and scheduling (cron-driven recurring
agent runs) are all Claude-Code-specific abstractions with no torra
equivalent.

**Why lowest priority:** none of these are core to "operator" function --
they're conveniences layered on top of a coding/ops agent, not things an
OS-engineering agent inherently needs. torra's TUI already has an
analogous primitive for one of them (`ctrl+t`'s independent tabs are closer
to parallel sessions than Claude Code's subagents, though without a
supervising/aggregating layer). If a *specific* convenience turns out to be
load-bearing for the operator's actual workflow (not hypothetically useful),
scope it here the same way -- name the gap, measure it, then apply the
four-question gate before building.

**Not started.**
