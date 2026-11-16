# Investigate a degraded automation

Use this sequence when recent scheduled runs grow, compact repeatedly, or stop completing. The analysis commands read local sessions and write reports under `agentlog-out`; they do not change logs or automation files. Run `automations snapshot` once to begin capturing future config and memory changes, then schedule it daily with a stable destination.

```powershell
agentlog automations list
agentlog automations snapshot --dest C:\agentlog-snapshots
agentlog trend --automation <automation-id>
agentlog diff <last-good-session> <first-outlier-session>
agentlog profile <first-outlier-session>
agentlog compactions <first-outlier-session>
agentlog loops <first-outlier-session>
agentlog compress <first-outlier-session>
agentlog skeleton <first-outlier-session>
```

Start with `trend.md` to identify the first outlier and note version, model, instruction, memory-read, context, compaction, and duration changes. Use `diff.md` to compare that run with the last healthy run. Profile and compaction reports show where context accumulated; loops identifies repeated work. Compression and the skeleton preserve the event structure for review while keeping the full transcript out of the initial inspection.

For a synthetic six-run regression, runs 1–3 are healthy. Runs 4–6 have increasingly large `memory.md` reads, repeated failures of the same command, two compactions, and a CLI version change at run 4. The integration test expects run 4 as the first outlier, the run 3→4 diff to expose memory growth and the version change, `loops` to report the retry, and run 6 compression to reach a ratio of 0.25 or lower. The fixture is generated from hand-written event values in `tests/test_e2e.py`; no real session content is used.

After deterministic review, provide the redacted `trend.md`, `diff.md`, `profile.md`, `compactions.md`, `loops.md`, `skeleton.md`, and `compress.md` reports to an analyst for root-cause review. Inspect reports before sharing them; redaction recognizes common patterns but cannot guarantee every sensitive detail is removed.

For a continuously diffable history, configure Windows Task Scheduler or cron using the commands in [Automation snapshots and timezone behavior](../README.md#t9-automation-snapshots-and-schedule-timezone-data). Automatic offline timezone database support remains deferred; until that task passes, the documented Eastern-time fallback may be used when a requested named zone is unavailable.
