# OS-engineering guardrails and when a check fails

Why each guardrail exists, and this repo's own guards. Safety-critical path: read before touching the gates.

## OS-Engineering Guardrails — what "the domain is real machines" actually requires

These are not generic good-practice items; each exists because a wrong guess here means a
wrong command runs against a real filesystem, package manager, or init system.

- **Command resolution must match how a real shell actually resolves the command**, not how a
  naive pattern-match reads the text on the page. `SafetyClassifier` unwraps `sudo`, `env`,
  `timeout`, `nohup`, `nice`, `xargs`; recurses into `sh -c` payloads; decodes base64 blobs
  piped to a shell; and splits on quotes correctly. Plain regex over raw text measured 5 of 12
  evasions missed in testing — treat that number as the bar a change to this area must clear
  (currently 20/20 caught, 0 false positives; re-run `tests/test_gates.py::TestSafety` and
  don't let it regress). If you're adding a new wrapper or shell construct to recognize, add
  the evasion case to that test corpus in the same change — an unlisted evasion is
  indistinguishable from "handled" until someone hits it live.
- **systemd-scope assumptions must be checked against the real unit, not assumed.**
  `AUDIT_FINDINGS.md` MED-2: service handling assumed `--user` scope everywhere; wrong for a
  system service. On this exact machine, `pop-os`'s `ollama.service` itself is a live
  cautionary example of scope mattering — it runs system-wide, and a `--user`-scoped
  assumption anywhere near it would silently check or act on the wrong unit. When code decides
  how to query or act on a systemd unit, verify scope explicitly; don't default to whichever
  scope was convenient to write first.
- **OS domain knowledge fed to the planning model grows from a measured gap, never a guess.**
  `docs/*.md` exists only for facts a real benchmark run (`scripts/dev/model_bench/`) caught a
  model getting wrong, vague, or hallucinated (see `docs/README.md`) — not from "this seems
  like something a Pop!_OS agent should know." A docs file written from guesswork is the same
  failure mode as an untested gate: it looks like coverage and isn't. Add a themed file only
  behind a benchmark result, and update `docs/README.md`'s table in the same change. Wiring
  any of `docs/` into `harness.agent.SYSTEM_PROMPT` is its own reviewed change with its own
  test — never bundle it into "just adding a docs file."
- **Model routing consults the live daemon before it guesses.** `ModelRouter` and
  `Accelerator.fits` use real `/api/show` data (params, `capabilities`) when available; the
  name-based guess is the fallback for when the daemon can't be reached, never the primary
  path. Found live: `qwen3.5` and `deepseek-r1-heavy` guessed as 0B from the name (no digit),
  and `nomic-embed-text` — an embedding-only model — was routed to as a chat target by the old
  "smallest guessed size wins" downgrade path. If you touch routing or the VRAM-starved
  fallback, keep the daemon-first order; a name-based shortcut that "usually works" is exactly
  how this broke the first time.
- **A destructive/irreversible action never runs without an explicit human tick, on every
  path that can decline or execute a plan** — TUI, CLI, and direct API use all go through the
  same `Harness.execute`/`Harness.reject` choke point. A new UI affordance or entry point that
  bypasses either is a second, untested code path for the exact thing the gates exist to
  cover — see HANDOFF.md §2.8/§2.10 for two real bugs of exactly this shape found in a
  full-file audit.

## When a check fails: fix the CAUSE, not the check

The rule is AGENT_DOCTRINE.md's. This repo's guards: the 788-test suite, `ruff`/`mypy`, the
defensive-coding scorecard, the evasion corpus in `test_gates.py::TestSafety`, the clarifier's
ambiguity corpus. The ❌ path has a named precedent here — tuning the clarifier against its own
test corpus instead of a fresh one (`HANDOFF.md` §4 calls this out explicitly as a mistake already
made once). For anything on the safety-critical path (any of the five gates,
`Step.command`/`Step.run`, `execute`/`reject`), the "is this guard actually wrong" call isn't
yours to make unilaterally: surface it to the user.
