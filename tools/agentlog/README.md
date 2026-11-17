# agentlog

`agentlog` is a local, deterministic, standard-library-only toolkit (plus `tzdata` timezone data on Windows) for reading and analysing Codex and Claude Code session logs. Its commands produce compact, redacted reports for session discovery, usage and context analysis, repetition, failure patterns, automation changes, and indexed search.

## Install and run

Python 3.11 or newer is required. The only runtime dependency is the IANA timezone data package `tzdata`, installed automatically on Windows only, because Windows has no system timezone database; it is data read by the standard-library `zoneinfo` module. From the repository root, install the package in an isolated environment with:

```powershell
pipx install .\tools\agentlog
```

Alternatively, run the checked-in Windows launchers from PowerShell:

```powershell
.\tools\agentlog\agentlog.cmd --help
.\tools\agentlog\agentlog.ps1 compress --help
```

The launchers run the tool from its own folder without installing it, and keep the caller's working directory, so relative paths resolve where the command was run. They use `python` from `PATH` unless the `CODING_AGENT_TOOLS_PYTHON` environment variable holds the full path to a `python.exe`; Python 3.14 or newer reads `.jsonl.zst` archives natively without an external `zstd`. When running without installing on Windows, install `tzdata` for that interpreter (`python -m pip install --user tzdata`).

## Command guide

| Command | Purpose | Example |
| --- | --- | --- |
| `doctor` | Check Python, local capabilities, roots, and discovered-session counts. | `agentlog doctor` |
| `census` | Measure record, tool, and command byte/token shares. | `agentlog census <session> --all` |
| `sessions` | List session metadata and filter likely automation runs. | `agentlog sessions --automation <id>` |
| `skeleton` | Show the event timeline without full bodies. | `agentlog skeleton <session>` |
| `show` | Expand an event, block, variant, loop, or finding reference. | `agentlog show E412 --session <session>` |
| `profile` | Trace usage, context, tool costs, and limits over time. | `agentlog profile <session>` |
| `compactions` | Review compaction summaries, drift, and context reacquisition. | `agentlog compactions <session>` |
| `compress` | Deduplicate repeated text and annotate repeated cycles and non-adjacent sequences. | `agentlog compress <session>` |
| `expand` | Reconstruct redacted events from a lossless compressor report. | `agentlog expand .\agentlog-out\<session8>\compress.json --out .\events.jsonl` |
| `loops` | Detect repeated failures, wasted reads, stalls, and limit hits. | `agentlog loops <session> --fail-on high` |
| `automations list` | Show local automation metadata and matched run counts. | `agentlog automations list` |
| `automations runs` | List runs matched to an automation. | `agentlog automations runs <id>` |
| `automations show` | Inspect redacted configuration and memory statistics. | `agentlog automations show <id>` |
| `automations snapshot` | Save changed automation config and memory files to a chosen destination. | `agentlog automations snapshot --dest .\agentlog-snapshots` |
| `trend` | Compare automation runs and mark the first outlier. | `agentlog trend --automation <id>` |
| `diff` | Compare a healthy and degraded run across metadata, inputs, and behavior. | `agentlog diff <last-good> <first-bad>` |
| `index` | Build a local SQLite full-text index of redacted messages. | `agentlog index --root <session-root>` |
| `search` | Find matching messages in the local index. | `agentlog search --q "context window"` |

Most commands write a Markdown view and complete JSON data beneath `agentlog-out/`; use `--out DIR` to choose another report directory. Session references accept a full ID, an unambiguous prefix of at least eight characters, or a supported log path. `--root PATH` adds a discovery root. `--json` selects a compact JSON stdout summary.

### View budgets

| View | Default estimated-token budget | Behavior |
| --- | ---: | --- |
| `skeleton`, `show` | 8,000 | Strict target; truncates the displayed view while retaining complete redacted JSON. |
| `profile` | 3,000 | Strict target. |
| `compactions`, `diff` | 6,000 | Strict target. |
| `sessions`, `automations` | 4,000 | Strict target. |
| `census` | 1,500 | Strict target. |
| `compress` | 8,000 target | Mandatory transcript structure may exceed the target; optional detail is dropped first. |
| `loops` | 3,000 | Findings table can summarize additional findings retained in JSON. |
| `trend` | 3,000 | Keeps rows around the first outlier and newest runs when needed. |
| `search` | 6,000 | Strict query budget; absolute query cap is 8,000. |

`AGENTLOG_HARD_CAP` defaults to 20,000 estimated tokens. It is an absolute cap for `compress`; a view that cannot fit its required structure exits 2 before a partial report is written. Other view commands use their strict `--max-tokens` budget (default 8,000) and reject requests above the hard cap.

## Skeleton and usage profile

`skeleton` gives a compact event timeline. When a matching `compress.json` is present, loop annotations use the compressor's recorded occurrence indexes to identify actual loop members and their iteration. Bookkeeping events such as `token_usage` and `settings` are excluded from loop identity, so a bookkeeping event between two loop members is not annotated as part of that loop. The displayed event references preserve the actual membership.

Block annotations show the occurrence multiplicity of the referenced shared entry, including shared variants as well as blocks. When chunking is enabled, a block or variant reference identifies the exact source event chunk recorded by the compressor, not the entire containing event. These details depend on valid compressor occurrence metadata; malformed explicit analysis metadata is rejected with a clear error rather than treated as an empty or partial analysis. This T2 acceptance contract passed independent review.

### Aggregate census and finding evidence (T2 independent acceptance PASS)

With `census --all`, tool-call/result pairing is scoped to each source file. Reused call IDs or session IDs in different files do not connect records across files, and an unmatched result cannot inherit a call from another file. Finding evidence references are validated explicitly: valid `E`, `B`, and `L` references are accepted; malformed or non-string references produce exit code 2 before report files change or partial stdout is emitted. These census and evidence contracts passed independent review.

`profile` includes tool-call arguments in estimated event costs, context additions, carry cost, and largest-event rankings, along with tool results. For example, a synthetic 70-token tool call contributes 70 estimated tokens to those event-level views. When authoritative token usage is available, it remains authoritative for usage totals; the event estimates do not replace it.

Provider input fields keep their source meanings. For Claude, `input` excludes cache reads and cache creation; `cached_input` is cache reads, and `cache_creation_input` is reported separately. For example, `input=100`, `cached_input=40`, and `cache_creation_input=20` yield 120 non-cache-read input tokens. For Codex, `input` includes cache reads, so `input=100` and `cached_input=40` yield 60 non-cache-read tokens. Missing values remain unknown rather than being treated as zero. These non-cache-read figures are not exact quota or pricing calculations.

Run these views with a session reference:

```powershell
agentlog skeleton <session>
agentlog profile <session>
```

The examples below use `agentlog`. A session argument can be a full session ID, an unambiguous ID prefix of at least eight characters, or a path to a supported log. Use `--root PATH` to add a discovery root and `--source codex|claude|generic` when source detection needs to be specified.

### Search the indexed conversation (T7 independent acceptance PASS)

T7 adds a local SQLite full-text index for narrow searches across redacted, human-visible user and assistant messages. Independent acceptance passed: agentlog reports 221 passing checks and codeclaims reports 47. The archive-indexing test builds its synthetic archive with `compression.zstd` on Python 3.14+, falls back to the external `zstd` executable on older Pythons, and skips only when neither is available.

```powershell
agentlog index [--db PATH] [--root PATH] [--source codex|claude|generic]
    [--cwd-prefix PATH]... [--project-pattern PATTERN]... [--term REGEX]...
    [--min-term-hits N] [--since DATE] [--until DATE] [--include-archived]
    [--keep-tool-stubs] [--threads-csv PATH] [--json]
agentlog search --q QUERY [--db PATH] [--session ID] [--since DATE] [--until DATE]
    [--role user|assistant|any] [--top N] [--window N]
    [--max-chars-per-msg N] [--max-tokens N] [--out FILE] [--json]
agentlog index --stats [--db PATH]
agentlog sessions --indexed [--db PATH] [--limit N] [--json]
```

Run `index` to build or incrementally update the index; `search` runs the ranked query (the former `ddk transcripts query` operation), and `index --stats` reports index statistics. `sessions --indexed` lists indexed sessions through the existing `sessions` command. The database defaults to `./agentlog-out/index.sqlite`. Selection with no filters includes all sessions. The reader-based index keeps user and assistant messages, with optional tool-call stubs; readers redact content before it is indexed. Search defaults to a 6,000-token result budget and refuses requests above its separate 8,000-token limit, regardless of `AGENTLOG_HARD_CAP`.

Index transactions are per file. If a later input has a duplicate session ID, indexing refuses it without replacing the data already indexed for that ID; earlier successful inputs from the same invocation may remain indexed.

Working-directory selection is based on a structured `cwd` field in any record, compared case-insensitively with normalized path separators. Codex top-level and payload cwd fields and Claude/generic top-level cwd fields are considered; message content is not searched for paths and path collections are not retained. Request-dump folders have no structured cwd and do not match this selector, though they can be selected by terms or by indexing without filters. Request-dump directories are re-read on every index invocation rather than trusted by directory metadata. If a rollout and request dump have the same session ID, indexing must refuse and ask for a narrower source selection instead of replacing either record.

For Codex logs, legacy `event_msg`/`user_message` user text is used as a fallback. A file-wide exact membership check against user text in response items suppresses a matching legacy mirror; other legacy user text remains searchable. Because the check is file-wide, identical text in separate prompts that use mixed formats cannot be distinguished. The check uses private, bounded fingerprint scratch data and does not store raw text in that scratch data.

For Claude logs, recognized command and system wrappers are stripped as complete spans before indexing, while ordinary conversational text in mixed wrapper/message bodies remains in normalized events and searchable. Wrapper-only events and events marked `isMeta` remain context rather than searchable conversation text. If a recognized leading wrapper is unclosed, handling is conservative; T7 passed independent acceptance. Its archive test exercises the native `compression.zstd` path on Python 3.14+ and skips only when neither native nor external zstd is available.

The SQLite index and search reports contain redacted conversation text and may still be sensitive; keep them protected and review reports before sharing. Tests use synthetic records only. T7's selection, collision, and privacy checks passed independent acceptance.

## Compress repeated content

```text
agentlog compress <session> [--root PATH] [--source codex|claude|generic]
    [--unit event|chunk] [--chunk-lines 40] [--near 0.85]
    [--min-block-tokens 30] [--body-cap 1200] [--max-period 12]
    [--min-reps 3] [--lossless] [--print] [--out DIR] [--json]
    [--max-tokens N]
```

For example, compress a synthetic JSONL session into a chosen report directory and keep it reconstructable:

```powershell
agentlog compress .\sample-rollout.jsonl --source generic --lossless --out .\reports\sample
```

The default destination is `./agentlog-out/<session8>/`. A successful run writes:

- `compress.md`: a readable transcript with a raw/view token header, a repetition cost summary, and the transcript structure.
- `compress.json`: the complete block and variant dictionaries, occurrence/event metadata, loop and sequence analysis, mask values, and statistics. With `--lossless`, it also contains the full normalized bodies needed to rebuild event text.
- `compress-stats.md`: token savings estimates, per-turn novelty, and the top 15 blocks by total tokens.

Without `--print`, stdout contains a short statistics summary (or JSON with `--json`). With `--print`, the complete `compress.md` view is written to stdout. Reports contain redacted log content and may still be sensitive; store them with the same care as the source logs.

### How repeats are represented

`compress` compares redacted event text after masking volatile values. Mask types include timestamps and dates, UUIDs, hexadecimal IDs, durations, large numbers, temporary paths, process IDs, and line/column locations. Their original values are stored per occurrence in `compress.json`; for example, otherwise identical tool output with different times can share a normalized body. Secret and account-identifier redaction happens before analysis and the removed values cannot be recovered by `expand`.

The first distinct body is a `B<n>` block. A later identical normalized body references that same B or V entry as an exact repeat. A sufficiently similar new body can become a `V<n>` variant. Each variant stores one normalized body and an explicit `parent` reference to an earlier B or V entry, plus its similarity and a capped display diff. The parent records the compressor's deterministic similarity choice; it does not establish editing history. Among eligible MinHash candidates that meet the similarity and diff-size thresholds, the parent has the highest Jaccard similarity, with creation order breaking ties. Similarity uses line shingles for tool output and word shingles for other text. Use `agentlog show V<n> --session <session>` to inspect a variant's first source occurrence, its parent reference, and all occurrence event/chunk references. This lookup needs the source session and the corresponding `compress.json`; see [Show a shared variant](#show-a-shared-variant). JSON and `expand` follow variant references and ancestry.

By default, each event with text is compared as a unit, except that `token_usage`, `settings`, and `other` events are represented as markers. Long `tool_result` events are split into line chunks when they exceed `--chunk-lines` (default 40). `--unit chunk` also chunks other long text events. `--near` sets the near-duplicate Jaccard threshold (default 0.85); `--min-block-tokens` is the minimum size eligible for near matching (default 30 estimated tokens). `--body-cap` limits text previews in the transcript (default 1,200 characters); full body text is available in JSON only with `--lossless`.

Readers stream input records, but compression retains one body for each distinct normalized block or variant, plus occurrence-specific data (including mask values and event metadata) and the links and indexes needed to reconstruct and analyze occurrences. Its memory use can therefore grow with the number and size of distinct bodies and occurrence records. Repeated full bodies are shared rather than copied for every occurrence.

Repeated event cycles can be shown as `L<n>` loops; bookkeeping `token_usage` and `settings` events are excluded from loop identity. The default maximum period is 12 events and at least 3 repetitions are required. Non-adjacent repeated sequences are annotated as `S<n>` and are not collapsed. The JSON report retains exact occurrence positions. Loop event lists preserve gaps caused by excluded bookkeeping events.

### Detect loops and stalls (T5 independent acceptance PASS)

`loops` applies deterministic rules to a session and produces findings that identify likely repeated work, recurring failures, or resource pressure. It uses the matching `compress.json` when available and runs `compress` internally when that report is missing. Findings are numbered `F<n>` and ranked by estimated token cost, highest first.

```text
agentlog loops <session> [--rules R1,R2,...] [--set R1.min=4]... [--fail-on high|medium] [--json]
```

The command writes `loops.md` (at most 3,000 estimated tokens) and complete `loops.json`. The Markdown report summarizes findings by severity and token cost as a share of the session total, lists findings in a table, then gives each finding its evidence references and a fixed rule-specific explanation. Evidence includes up to eight `E`, `B`, or `L` references and a `show` hint, plus first and last event references, occurrence count, severity, title, and estimated token cost. With 100 findings, the table is truncated with a count pointing to `loops.json`; the JSON report retains the complete results. `--rules` selects rules by their `R1`–`R12` IDs. Thresholds can be overridden with `--set R1.min=4`; available settings and their defaults are reported by `--help`. `--fail-on high` returns exit code 1 when a high-severity finding exists, and `--fail-on medium` does so for a medium- or high-severity finding. Without a matching finding, the command returns 0.

The rules cover repeated tool calls without intervening writes (R1), recurring errors (R2), compressor-detected loops with no outcome change (R3), reverted edits (R4), repeated reads without writes and their token cost (R5), growing command output (R6), stretches of turns without progress (R7), reacquiring pre-compaction reads (R8), context saturation (R9), excessive agent/task fan-out (R10), growing instruction-file reads (R11), and usage, rate, or context limit errors (R12). Titles are capped at 100 characters; each finding includes severity and occurrence count. R1 compares identical masked calls within a 50-event window; R4 compares patches to the same file within 200 events; R5 sums result tokens after the first read; R6 requires strictly increasing output across at least three runs, with the last at least twice the first; R7 requires five turns with no file write and novelty below 0.10; R8 compares re-read tokens within ten turns after compaction; R9 requires at least 30% of model calls at 85% or more of the context window; R10 flags over 20 matching tool calls in a turn or nesting depth above two; R11 requires reads in at least three turns with at least 25% growth; R12 lists matching limit errors. R1, R2, R3, and R8–R12 are high severity under their specified thresholds, R4 is medium, and R5, R6, and R7 can be high under their rule-specific thresholds. Findings estimate token cost to help prioritize review; these estimates are not billing or quota measurements.

T5 passed independent acceptance with positive and near-miss synthetic cases, hand-checked costs, deterministic finding numbers, `--fail-on` behavior, and a bounded report with 100 findings. T10 request-dump-to-loops integration was also checked.

## Show a shared variant

`show V<n>` reads the variant entry from `compress.json` and uses its occurrence indexes with the source session to retrieve the first redacted occurrence. When the entry refers to chunked text, this is the exact referenced chunk, preserving its line endings; otherwise it is the whole event. It reports the explicit parent (`B<n>` or `V<n>`) and every occurrence as an event reference, with a chunk number where applicable. The same chunk-level lookup applies to `B<n>`. It does not need `compress --lossless`: `compress.json` must be present, and the source session must still be readable. A requested reference absent from its report, or a requested `E<n>` absent from the source session, is reported as `NOT FOUND` and returns code 1. Malformed or inconsistent compressor metadata is rejected clearly, including indexes that do not map to source events or chunks.

For example, after creating a report from a synthetic session:

```powershell
agentlog compress .\sample-rollout.jsonl --source generic --out .\reports\sample
agentlog show V1 --session .\sample-rollout.jsonl --out .\reports\sample
```

In this command, `--session` identifies the source session. `--out` names the directory containing the matching `compress.json`; it is also where `show.md` and `show.json` are written. If `--out` is omitted, `show` looks for `compress.json` in the default `./agentlog-out/<session8>/` directory. The `show` view observes its normal strict `--max-tokens` budget (default 8,000) and the absolute `AGENTLOG_HARD_CAP` (default 20,000); it does not use `compress`'s target-overflow behavior. The T2 acceptance repair contract measures the final redacted stdout representation, including JSON escaping and wrapper fields when `--json` is used, plus its terminating newline. The complete view is redacted before truncation so truncation cannot expose secret fragments; the final serialized stdout, including JSON escaping and its terminating newline, must fit the requested cap. It independently budgets the Markdown view including its required header. Detailed `show.json` remains complete and redacted even when the displayed view is truncated. If the required wrapper, header, or full truncation marker cannot fit, `show` refuses with exit code 2 before changing output files or printing partial stdout. T2 passed independent acceptance review.

### Output token target and hard cap

`--max-tokens N` (default 8,000) is a target for the complete `compress.md` view, including its header. The compressor preserves mandatory event positions, turn boundaries, errors, and compaction markers (including before/after context where known). If mandatory structure alone exceeds the target, the view may exceed it and includes a notice with the target and actual estimated size. Optional previews, variation values, and detailed variation rows are dropped first; complete variation data remains in `compress.json`. The CLI summary also reports target overflow, or with `--print` the overflow summary goes to stderr.

`AGENTLOG_HARD_CAP` (default 20,000 estimated tokens) is an absolute cap on the complete view, including its header and overflow notice. If required structure cannot fit, the command exits with code 2 before writing reports or emitting a partial view. The diagnostic gives the estimated minimum and cap; raise `AGENTLOG_HARD_CAP` if appropriate. `--max-tokens` must be positive and cannot exceed the hard cap.

## Expand a lossless report

Create a lossless report with `compress --lossless`, then reconstruct its redacted events with their volatile values restored as JSON Lines:

```powershell
agentlog compress .\sample-rollout.jsonl --source generic --lossless --out .\reports\sample
agentlog expand .\reports\sample\compress.json --out .\reports\sample\events.jsonl
```

`expand` requires a schema-version-1 lossless `compress.json`; it rejects ordinary non-lossless reports. It resolves shared B/V bodies and explicit variant ancestry, reapplies each occurrence's volatile values, and writes the event fields and reconstructed text in original order. Empty bookkeeping events are preserved. The output is redacted JSONL, not a copy of the original raw log: secrets/account identifiers removed during reading stay removed, while stored mask values restore the redacted event text before volatile masking. Expansion validates the full report before opening the destination and rejects a destination that is a symlink or hard link or conflicts with protected inputs. Use a separate output path. `--out` is required; `--json` selects JSON for the short stdout summary.

## Exit codes and privacy

- `0`: command completed.
- `1`: `show` found a requested reference absent from its report or an `E<n>` absent from the source session; `loops --fail-on` found a finding at or above the requested severity.
- `2`: usage error, invalid report/options, or hard-cap refusal.
- `3`: I/O error.

The tools are local and make no network or LLM calls. Readers redact recognized secrets and account identifiers before event text is created; compression masks volatile values for comparison. Neither redaction nor masking is a guarantee that every sensitive detail in arbitrary log text is identified. Never publish reports without reviewing them. Do not use real logs as repository fixtures; use synthetic examples only. The tools do not modify source logs or user configuration directories.

## Investigate a degraded automation

The [step-by-step walkthrough](examples/degraded-automation.md) starts with automation metadata and snapshots, finds the first outlier, compares it with the prior healthy run, then inspects profile, compaction, loop, compression, and skeleton reports. T9's synthetic integration uses six runs: the first three are healthy; runs four through six have growing memory reads, a repeated failing command, two compactions, and a CLI version bump at run four. Trend must mark run four first, diff 3→4 must expose the memory-read increase and version change, loops must report the retry, and compression of run six must be at most 0.25.

### T5 `loops` integration: R10 ancestry clarification (T5 and scoped T10 PASS)

An ordinary normalized session exposes only a `sidechain` boolean, which cannot establish thread nesting depth. Report depth as unavailable when reliable ancestry is absent; never infer depth from that boolean. For request-dump sessions, optional `--threads-csv` rows in `meta.extra["threads"]` can provide ancestry. Derive only proven root-relative chains rooted at `meta.session_id`; coalesce identical edges and treat redaction-marker IDs, conflicting parent links, cycles, and missing parents as unresolved. Retain child CSV rows whose parent is empty or missing as unresolved; do not discard them or infer a parent. Exclude the main/root row and rows belonging to another session from child ancestry and depth counts.

Distinct resolved child threads above the R10 depth threshold can produce a metadata-only finding with `evidence=[]`, `first=null`, `last=null`, `tokens=null`, and `evidence_kind="thread_metadata"`. Include up to eight known root-to-thread chains in `metadata_evidence`, and use `show_hint="show F<n>"`. Since these findings have no event references or numeric costs, exempt them from event-reference and numeric-cost requirements. Sort unknown costs after known costs, sum only numeric costs, report `unknown_cost_findings`, and render unknown costs as `?` or `unavailable`, never zero. Preserve all event-based findings. Keep fan-out as a separate R10 signal; ancestry depth is not inferred from fan-out. T5 and the scoped T10 ancestry repair passed independent verification. The request-dump-to-loops integration was checked.

### Automation matching decision (T6 independent PASS)

Automation matching considers only sessions marked `thread_source == "automation"`. Candidates are ranked first by prompt containment, then by whether the session cwd is listed for the automation, then by schedule evidence: a confirmed schedule fit ranks above unknown schedule fit, which ranks above a confirmed miss. Schedule misses remain eligible candidates; an unsupported or unparseable schedule is unknown, not a miss. Prompt containment must be at least 0.8 or the result is `no-match`; tied best candidates remain `ambiguous`.

For `FREQ=HOURLY`, `INTERVAL` aligns eligible hourly periods to the hour in `DTSTART`; `BYMINUTE` selects the occurrence minute within each eligible hour. No occurrence before `DTSTART` counts as a fit. Schedule proximity is measured in UTC, with a window of plus or minus 90 minutes around the session start. T6 passed independent verification: agentlog reports 201 passing checks and codeclaims reports 47. The T5 and T10 loop integration walkthroughs passed.

### T9 automation snapshots and schedule timezone data

`automations snapshot` copies each selected automation's `automation.toml` and `memory*.md` into a timestamped subdirectory, but only when file hashes differ from the latest snapshot. The default destination is `agentlog-snapshots` under the current working directory. Set `--dest` to a stable, explicit path when scheduling the command; it is safe to run repeatedly. These examples run daily at 02:00 and use the installed `agentlog` executable. Replace the executable and destination paths with paths on your machine.

Windows Task Scheduler (`schtasks.exe`, entered as one PowerShell command):

```powershell
schtasks.exe /Create /SC DAILY /ST 02:00 /TN "agentlog automation snapshot" /TR '"C:\Users\YOURNAME\AppData\Local\Programs\Python\Python313\Scripts\agentlog.exe" automations snapshot --dest "C:\Users\YOURNAME\agentlog-snapshots"' /F
```

Cron (one crontab line; replace the paths):

```cron
0 2 * * * /home/YOURNAME/.local/bin/agentlog automations snapshot --dest /home/YOURNAME/agentlog-snapshots
```

Schedule matching supports `FREQ=DAILY`, `WEEKLY`, and `HOURLY` rules with the documented interval and date/time selectors. Occurrence seconds are inherited from `DTSTART`; without a `DTSTART` anchor, occurrences use minute resolution with seconds set to zero. `BYSECOND` is unsupported and makes schedule fit unknown. Without an anchor, `DAILY` requires `BYHOUR` and `BYMINUTE`, `WEEKLY` requires `BYDAY`, `BYHOUR`, and `BYMINUTE`, and `HOURLY` requires `BYMINUTE` (`BYHOUR` is optional). Any interval greater than 1 requires an anchor. If a needed field is missing, schedule fit is unknown. Unsupported or unparseable rules are unknown schedule evidence. A named `TZID` is resolved with the standard-library `zoneinfo` module, which uses the operating system's IANA database on Linux and macOS and the `tzdata` package on Windows (installed automatically by `pip install` or `pipx install`). The data is local; no network access happens at runtime. If a zone cannot be loaded (an unknown name, or no timezone data, for example when running the source-tree launchers on Windows without `tzdata`), schedule fit is unknown and a warning on stderr names the zone; when no timezone data is present at all, the warning says to install `tzdata`. Unknown schedule evidence never counts as a miss. Timezone rule changes arrive with new `tzdata` releases or operating-system updates; upgrade with `pip install -U tzdata` (or reinstall agentlog). See [docs/timezone-database-task.md](docs/timezone-database-task.md) for the decision record. T9 passed independent acceptance: agentlog reported 240 passing checks at that time (238 after the 2026-11-15 timezone change replaced the fallback tests) and codeclaims reports 47. The archive test now covers the Python 3.14+ native-zstd path without the external executable. T5 and T10 `loops` end-to-end integration remains passed, and the T9 synthetic six-run walkthrough is covered by `tests/test_e2e.py`.

For supported schedules, local times that do not occur during a daylight-saving transition have no occurrence and are excluded at every frequency. For `FREQ=DAILY` and `FREQ=WEEKLY`, an ambiguous local time during a daylight-saving transition resolves to its first occurrence (`fold=0`). For `FREQ=HOURLY`, `INTERVAL` advances by elapsed UTC hours, anchored to the hour of `DTSTART` in UTC; `BYMINUTE` selects the minute within each eligible hour, and matching considers both actual UTC occurrences when they fall in a repeated local time. This documents the T9 behavior; the synthetic six-run integration is covered by `tests/test_e2e.py`.

### Request snapshot folders (T10 independent PASS)

`requestdump` reads a folder of reconstructed full-context request snapshots produced by `codex-request-usage.py --dump-requests`. The supported files are named `request-NNN.txt` (the numeric portion may have any length); the reader orders them numerically. Pass the folder path as the session reference, or make it discoverable under an explicit `--root`:

```powershell
agentlog profile .\request-dump-folder
agentlog profile request-dump-folder --threads-csv .\threads.csv
```

The reader ignores derived files such as `request-NNN.bin`, `request-NNN-new.txt`, `request-NNN-new - truncated.txt`, and `request-growth-report.txt`. Within each text snapshot, items are separated only by a line of exactly 60 hyphens followed by an `ITEM NNNN` line; hyphen lines elsewhere can be part of an item body. Request snapshots contain the full context sent for each request, so the reader reconstructs incremental main-thread events by comparing adjacent snapshots. Identical re-sends produce a context marker, while resets produce a compaction event. This is a partial, main-thread-only reconstruction, not a complete record of all subagent activity.

The optional global `--threads-csv PATH` supplements the session metadata with thread rows from a `codex-token-usage.py` CSV. The rows are available as `SessionMeta.extra["threads"]`; they do not add events to the dump, are not merged into request or session token totals, and are not evidence that subthread content is present. Profile may display these rows under “Sub-threads (from CSV; not in dumps)”.

Snapshot metadata includes additive `SessionMeta` fields for partial status and its reason. Because these fields are part of the dataclass, `dataclasses.asdict`-based JSON output includes them; consumers that compare serialized metadata should allow the added fields. Snapshot bodies pass through the normal redaction before analysis. Reports may still contain sensitive information, so review and protect them as described under [Exit codes and privacy](#exit-codes-and-privacy).

`doctor <folder>` can report the request count, total bytes, and parse error count for a request snapshot folder. `doctor` without a folder retains its existing behavior. `dump-sessions` identifies request-dump sessions through the normal discovery flow. `trend` bypasses its cache for request-dump sessions because their contents are reconstructed from the current files. T10 passed independent verification: agentlog reports 206 passing checks and codeclaims reports 47. The T5 and T10 `loops` end-to-end integration passed; the T9 six-run walkthrough is covered by `tests/test_e2e.py`.

## Development

Run the standard-library test suite from this directory with:

```powershell
python -m unittest discover -s tests
```
