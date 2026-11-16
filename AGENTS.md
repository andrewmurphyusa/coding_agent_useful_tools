# Agent Repository Rules

- Keep runtime code standard-library-only unless the tool's specification explicitly permits an optional external binary.
- Never access the network or call an LLM from these tools.
- Never modify session logs or anything under user configuration directories. Use synthetic, hand-written fixtures only; never commit real logs, secrets, or account identifiers.
- Add license headers from `file_header_template.txt` to source files, using comments appropriate to the language. Do not invent or alter attribution.
- Run the relevant standard-library `unittest` suite from each tool directory before reporting implementation complete.
- Keep changes within the files assigned to the task. Do not run `git add`, `git commit`, or `git stash`; the repository owner handles commits.
- If Avast quarantines a newly created file or denies access to a fresh folder, stop and report it. Do not retry in a loop or change ACLs.

## Sub-Agent guidance

When developing software:
- use sub-agents for individual implementation tasks; choose the appropriate model for the task size; 
- use independent sub-agents for verification of task implementation;
- reserve the orchestrator agent's context for orchestration; keep implementation details out of it.

## Andrej Karpathy guidance

When developing software, follow the guidelines in `./KARPATHY-AGENTS.md`

### Code Quality Expectations
- Apply SOLID principles and Clean Code practices
- DRY (Don't Repeat Yourself)
- Single responsibility and separation of concerns
- Meaningful names that clearly describe purpose
- Error handling for user inputs and external API calls
- Comments only for complex logic (code should be self-documenting)
- Prefer functional programming paradigms where applicable
