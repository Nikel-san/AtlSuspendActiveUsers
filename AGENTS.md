# AGENTS.md

## Mandatory requirement
Never deviate from the General Procedure for Copilot: https://iderawebdev.atlassian.net/wiki/spaces/CT/pages/3742892034/General+Procedure+for+Copilot

## Repository purpose
This repository contains a Python utility that identifies Atlassian Cloud users whose last activity is before a configured cutoff date and optionally suspends them. The script is intentionally safety-focused and must not be used for destructive actions without explicit user approval.

## Required workflow for all tasks
- Keep changes narrow, targeted, and aligned to the existing script and README structure.
- Prefer small, reviewable edits over broad refactors.
- Preserve existing command-line behavior unless a change explicitly requires a new flag, output field, or documented behavior.
- Update the README when behavior, arguments, or usage examples change.
- Do not commit secrets, tokens, or customer data to the repo.
- Use environment variables such as `ATLASSIAN_TOKEN` and `ATLASSIAN_ORG` instead of hardcoded credentials.
- Do not perform live Atlassian suspension actions while validating or debugging code; prefer dry-run output, help output, or syntax validation.

## Validation before completion
- Before claiming success, run the smallest relevant verification command.
- For Python changes, at minimum run:
  - `python -m py_compile AtlSuspendActiveUsers.py`
  - and, when relevant, `python AtlSuspendActiveUsers.py --help`
- If testing behavior that touches API logic, prefer dry-run or mock-safe validation and report the exact command used and its result.
- State verification evidence explicitly in the final response.

## Quality bar
- Keep code readable and maintainable.
- Follow the existing naming and CLI conventions already present in the project.
- If a task requires new functions or flags, document them in the README and keep examples consistent.
- If a bug is found, fix the root cause rather than layering workarounds.

## Safety and compliance
- Treat all customer data as sensitive.
- Never expose private tokens, org IDs, or output from live API calls in logs or commit content.
- Avoid unnecessary network calls during local validation.

## Final response requirements
- Summarize what changed.
- Include the validation command(s) run and the outcome.
- Call out any limitations or follow-up actions that remain.
