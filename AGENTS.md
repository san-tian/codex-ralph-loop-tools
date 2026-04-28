# ralph-loop-tools

> CWD: `/vePFS-Mindverse/user/intern/ccss/plugins/ralph-loop-tools`

## Shared Memory Entry

- Project docs entry: `/vePFS-Mindverse/user/intern/ccss/docs/Projects/ralph-loop-tools/AGENTS.md`
- Shared docs root: `/vePFS-Mindverse/user/intern/ccss/docs/AGENTS.md`

## Usage

- Before changing this plugin, read `/vePFS-Mindverse/user/intern/ccss/docs/Projects/ralph-loop-tools/AGENTS.md`.
- Then read `/vePFS-Mindverse/user/intern/ccss/docs/Projects/ralph-loop-tools/Records/WORK_RECORDS.md` and any matching Feature from that project's Feature Index.
- Keep plugin behavior, README, skills, hooks, and project memory docs synchronized in the same turn.

## Directory Overview

- `.codex-plugin/plugin.json` — Codex plugin manifest.
- `.mcp.json` — local MCP server wiring.
- `hooks.json` — Codex lifecycle hook wiring.
- `scripts/` — MCP server, hooks, Codex plugin/cache installer, workspace-hook installer, tmux follow-up worker, tests, external supervisor script.
- `/vePFS-Mindverse/user/intern/ccss/.codex/hooks.json` — supported config-layer hook entry for Ralph across the `ccss/` tree.
- `skills/` — natural-language skill instructions for `ralph` and `ralph-wiggum`.

## Guardrails

- Pi-like automatic follow-up is tmux-only. If `TMUX` is unset, report that automatic follow-up is unavailable.
- Do not reintroduce non-tmux fallback behavior into user-facing skills or README unless explicitly requested.
- Avoid sending real tmux input during tests unless the user explicitly approves it.
