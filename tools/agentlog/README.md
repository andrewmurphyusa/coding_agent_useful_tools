# agentlog

`agentlog` is a local, deterministic, standard-library-only toolkit for reading and analysing Codex and Claude Code session logs. This guide covers the implemented `compress`, `expand`, and direct `show V<n>` lookup. Other command documentation, the end-to-end integration walkthrough, examples, and full T9 integration acceptance are still outstanding; this README does not claim T9 acceptance.

## Install and run

Python 3.11 or newer is required; there are no Python runtime dependencies. From the repository root, install the package in an isolated environment with:

```powershell
pipx install .\tools\agentlog
```

Alternatively, run the checked-in Windows launchers from PowerShell:

```powershell
.\tools\agentlog\agentlog.cmd --help
.\tools\agentlog\agentlog.ps1 compress --help
```

## Skeleton and usage profile

`skeleton` gives a compact event timeline. When a matching `compress.json` is present, loop annotations use the compressor's recorded occurrence indexes to identify actual loop members and their iteration. Bookkeeping events such as `token_usage` and `settings` are excluded from loop identity, so a bookkeeping event between two loop members is not annotated as part of that loop. The displayed event references preserve the actual membership.

Block annotations show the occurrence multiplicity of the referenced shared entry, including shared variants as well as blocks. When chunking is enabled, a block or variant reference identifies the exact source event chunk recorded by the compressor, not the entire containing event. These details depend on valid compressor occurrence metadata; malformed explicit analysis metadata is rejected with a clear error rather than treated as an empty or partial analysis. This is the T2 acceptance repair contract; the repair and its independent review are in progress.

### Aggregate census and finding evidence (T2 in progress)

With `census --all`, tool-call/result pairing is scoped to each source file. Reused call IDs or session IDs in different files do not connect records across files, and an unmatched result cannot inherit a call from another file. Finding evidence references are validated explicitly: valid `E`, `B`, and `L` references are accepted; malformed or non-string references produce exit code 2 before report files change or partial stdout is emitted. These census and evidence repair contracts are still awaiting implementation review.

`profile` includes tool-call arguments in estimated event costs, context additions, carry cost, and largest-event rankings, along with tool results. For example, a synthetic 70-token tool call contributes 70 estimated tokens to those event-level views. When authoritative token usage is available, it remains authoritative for usage totals; the event estimates do not replace it.

Provider input fields keep their source meanings. For Claude, `input` excludes cache reads and cache creation; `cached_input` is cache reads, and `cache_creation_input` is reported separately. For example, `input=100`, `cached_input=40`, and `cache_creation_input=20` yield 120 non-cache-read input tokens. For Codex, `input` includes cache reads, so `input=100` and `cached_input=40` yield 60 non-cache-read tokens. Missing values remain unknown rather than being treated as zero. These non-cache-read figures are not exact quota or pricing calculations.

Run these views with a session reference:

```powershell
agentlog skeleton <session>
agentlog profile <session>
```

The examples below use `agentlog`. A session argument can be a full session ID, an unambiguous ID prefix of at least eight characters, or a path to a supported log. Use `--root PATH` to add a discovery root and `--source codex|claude|generic` when source detection needs to be specified.

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

## Show a shared variant

`show V<n>` reads the variant entry from `compress.json` and uses its occurrence indexes with the source session to retrieve the first redacted occurrence. When the entry refers to chunked text, this is the exact referenced chunk, preserving its line endings; otherwise it is the whole event. It reports the explicit parent (`B<n>` or `V<n>`) and every occurrence as an event reference, with a chunk number where applicable. The same chunk-level lookup applies to `B<n>`. It does not need `compress --lossless`: `compress.json` must be present, and the source session must still be readable. A requested reference absent from its report, or a requested `E<n>` absent from the source session, is reported as `NOT FOUND` and returns code 1. Malformed or inconsistent compressor metadata is rejected clearly, including indexes that do not map to source events or chunks.

For example, after creating a report from a synthetic session:

```powershell
agentlog compress .\sample-rollout.jsonl --source generic --out .\reports\sample
agentlog show V1 --session .\sample-rollout.jsonl --out .\reports\sample
```

In this command, `--session` identifies the source session. `--out` names the directory containing the matching `compress.json`; it is also where `show.md` and `show.json` are written. If `--out` is omitted, `show` looks for `compress.json` in the default `./agentlog-out/<session8>/` directory. The `show` view observes its normal strict `--max-tokens` budget (default 8,000) and the absolute `AGENTLOG_HARD_CAP` (default 20,000); it does not use `compress`'s target-overflow behavior. The T2 acceptance repair contract measures the final redacted stdout representation, including JSON escaping and wrapper fields when `--json` is used, plus its terminating newline. The complete view is redacted before truncation so truncation cannot expose secret fragments; the final serialized stdout, including JSON escaping and its terminating newline, must fit the requested cap. It independently budgets the Markdown view including its required header. Detailed `show.json` remains complete and redacted even when the displayed view is truncated. If the required wrapper, header, or full truncation marker cannot fit, `show` refuses with exit code 2 before changing output files or printing partial stdout. This repair contract and its independent review are in progress.

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
- `1`: `show` found a requested reference absent from its report or an `E<n>` absent from the source session. The shared CLI also reserves this code for findings from `loops --fail-on` when that command is implemented; this README does not document `loops` as an available command.
- `2`: usage error, invalid report/options, or hard-cap refusal.
- `3`: I/O error.

The tools are local and make no network or LLM calls. Readers redact recognized secrets and account identifiers before event text is created; compression masks volatile values for comparison. Neither redaction nor masking is a guarantee that every sensitive detail in arbitrary log text is identified. Never publish reports without reviewing them. Do not use real logs as repository fixtures; use synthetic examples only. The tools do not modify source logs or user configuration directories.

## Development

Run the standard-library test suite from this directory with:

```powershell
python -m unittest discover -s tests
```
