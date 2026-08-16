# Copilot instructions for this repository

This repository must follow the General Procedure for Copilot at all times: https://iderawebdev.atlassian.net/wiki/spaces/CT/pages/3742892034/General+Procedure+for+Copilot

Follow the repository rules in AGENTS.md for all tasks in this project.

## Project-specific guidance
- This repo is a Python tool for suspending inactive Atlassian Cloud users.
- Keep the implementation safe, minimal, and operationally cautious.
- Use environment variables for secrets and never hardcode credentials.
- Prefer dry-run / help / syntax checks for validation before completion.
- Update documentation when CLI behavior changes.

## Required validation
Before claiming a task is complete, run the most relevant validation command and report the evidence.

Typical checks:
- `python -m py_compile AtlSuspendActiveUsers.py`
- `python AtlSuspendActiveUsers.py --help`

If the task impacts API behavior, validate with dry-run-safe paths when possible.
