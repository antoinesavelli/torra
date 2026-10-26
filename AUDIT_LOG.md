# Doc-Citation Audit Log

**Schedule:** Weekly (opencode routine `torra-doc-audit`)
**What it checks:** greps every backtick-quoted path-looking citation out of this repo's
top-level docs (README/AGENTS/CLAUDE/HANDOFF/DEFENSIVE_CODING_STANDARDS/AUDIT_FINDINGS) and
checks it resolves on disk (repo root, the `torra/` package subdir, or the sibling
`system-library` repo). Unresolved citations that are known false positives (structural rules:
attached flags, `/slash-commands`, bare `./command` examples, `dir/module.symbol` references whose
module defines the symbol; plus a reviewed per-doc list, `torra-doc-audit.known-fp.txt`) are
suppressed before any model sees them, and the entry says how many. Only when a genuinely new
citation fails to resolve does an opencode agent judge each one real-vs-false-positive and append
its findings below; a clean sweep appends a bare pass entry with no agent turn. A missing repo
fails the run (exit 1) instead of appending anything. This is a v1 grep heuristic, not a real
parser (see `torra-doc-audit.pre.sh`'s header) — it has a real false-positive rate by design and
the agent pass exists specifically to filter that.
**Append-only, newest run last** (a mechanical post-step does the actual write —
`torra-doc-audit.post.sh` — never the agent itself, same discipline as Paramo's
config-flags-audit).

---

### Run: 2026-09-04

**8 citation(s) checked, 8 unresolved by the grep heuristic.**

## Findings

- AIModels/blobs — false positive: snippet in code example, not a real repo path.
- fixes/ — false positive: placeholder text, no actual directory.
- legacy/ — false positive: README notes it was removed; reference acknowledges missing content.
- lists/ — false positive: placeholder, not current dir.
- telemetry/tasks.jsonl — false positive: refers to file in user home, not repo.
- textual/keys.py — false positive: not present in repo; example only.
- workdir/audit/agent-1.audit.json — false positive: runtime-generated file, not checked out.
- workdir/resume/ — false positive: runtime dir, not a src path.

### Run: 2026-09-28

**75 citation(s) checked, 8 unresolved by the grep heuristic.**

## Findings

- AIModels/blobs — false positive: directory did not exist, likely a non‑existent reference.
- legacy/ — false positive: no legacy directory present.
- lists/ — false positive: no lists directory present.
- paramo-routines/ — false positive: no paramo-routines directory present.
- telemetry/tasks.jsonl — false positive: file is generated at runtime, not tracked.
- textual/keys.py — false positive: no textual module in repo.
- workdir/audit/agent-1.audit.json — false positive: file would be created in workdir during runs.
- workdir/resume/ — false positive: workdir content is dynamic.

### Run: 2026-10-13

**105 path citation(s) checked across README.md AGENTS.md CLAUDE.md HANDOFF.md DEFENSIVE_CODING_STANDARDS.md AUDIT_FINDINGS.md: 92 resolve, 13 unresolved but suppressed as known false positives (5 by structural rule, 8 from torra-doc-audit.known-fp.txt); 0 real misses.**

### Run: 2026-10-13

**105 path citation(s) checked across README.md AGENTS.md CLAUDE.md HANDOFF.md DEFENSIVE_CODING_STANDARDS.md AUDIT_FINDINGS.md: 92 resolve, 13 unresolved but suppressed as known false positives (5 by structural rule, 8 from torra-doc-audit.known-fp.txt); 0 real misses.**

### Run: 2026-10-19

**111 path citation(s) checked across README.md AGENTS.md CLAUDE.md HANDOFF.md DEFENSIVE_CODING_STANDARDS.md: 99 resolve, 12 unresolved but suppressed as known false positives (5 by structural rule, 7 from torra-doc-audit.known-fp.txt); 0 real misses.**

### Run: 2026-10-26

**4 citation(s) checked, 4 unresolved by the grep heuristic.**

## Findings

- /home/alex — false positive: placeholder for user home, not a repo asset.
- /mnt/data/archive/ — false positive: external path, not required to exist in repo.
- /mnt/data/archive/torra-state-cleanup-20261013.tgz — false positive: external tarball, not part of repo.
- raw/ — real: the directory is referenced as implementation detail but does not exist in the repository.
