---
name: ralph
description: Long-running iterative development loops with pacing control and verifiable progress. Use when tasks require multiple iterations, many discrete steps, or periodic reflection with clear checkpoints; avoid for simple one-shot tasks or quick fixes.
---

# Ralph Wiggum For Codex

This skill is intentionally modeled after Pi's `ralph-wiggum` loop, but adapted to Codex's plugin constraints.

Available tools:

- `ralph_start`
- `ralph_done`
- `ralph_status`
- `ralph_resume`
- `ralph_stop`
- `ralph_list`
- `ralph_cancel`
- `ralph_archive`

## Loop Behavior

1. Start the loop with a complete task file body by calling `ralph_start`.
2. Work on the task and update the managed task file each iteration.
3. Record verification evidence in the task file as progress is made.
4. Call `ralph_done` to proceed to the next iteration.
5. Treat the loop as complete when the checklist is fully checked or the task file contains `<promise>COMPLETE</promise>`.
6. Stop when complete or when max iterations is reached.

Updating the task file alone does **not** hand off the iteration. A Ralph iteration is only handed off when `ralph_done` runs successfully or when tmux follow-up has clearly entered its pending continuation path and then successfully pastes the next prompt.

Do **not** bypass the Ralph control surface by calling internal helper functions such as `advance_loop(...)` directly from shell/Python snippets. That can advance the saved iteration counter without going through the supported `ralph_done` handoff path, and variants such as `record_prompt_trigger=False` can leave the session on a new iteration without refreshing the next-prompt fingerprint that hooks use for the next turn.

## Tmux Requirement

Pi-like automatic follow-up requires the current Codex TUI to run inside tmux.

Before starting or promising automatic multi-round continuation, check whether `TMUX` is set. If Codex is not running inside tmux, tell the user directly that Ralph automatic follow-up is unavailable in this environment and do not offer a non-tmux fallback.

When `TMUX` is set, use `scripts/ralph_tmux_followup.py --compact-first` for Pi-like follow-up into the current Codex pane. The Stop hook schedules this worker in the background so slow compacting does not block the hook. The worker first waits for the triggering turn's rollout to reach a terminal event, then waits for the Codex pane to be idle, sends `/compact`, waits for compact to finish, previews the next Ralph prompt, pastes it into tmux, presses Enter, and only then commits the next iteration state. If tmux delivery fails, the loop stays on the previous iteration and `ralph_status` surfaces the follow-up error.

Ralph resolves hook-driven session scope from the hook event's `session_id` first, and otherwise uses `CODEX_THREAD_ID` / `CODEX_WEB_RESUME_SESSION_ID` for implicit current-loop resolution. Another session should not inherit your current Ralph loop unless the user explicitly names that loop.

Automatic follow-up also depends on Ralph hooks being installed through a supported config-layer `hooks.json`. In this workspace, `scripts/install_workspace_hooks.py` writes `/vePFS-Mindverse/user/intern/ccss/.codex/hooks.json`, and that file calls the source-tree `scripts/ralph_hook.py` by absolute path so the active cwd can be another project under `ccss/`.

Ralph hook guidance is intentionally narrow: `SessionStart` stays silent, and `UserPromptSubmit` only injects Ralph loop context when the submitted prompt exactly matches a session-scoped next-iteration prompt previously emitted by Ralph itself. Mentioning Ralph in ordinary prose does not trigger hook injection.

## Natural-Language Triggers

Use this skill when the user says things like:

- "开一个 ralph loop"
- "帮我起一个 ralph"
- "继续这个 ralph loop"
- "暂停这个 ralph loop"
- "恢复 ralph xxx"
- "查看 ralph 状态"
- "列出 archived ralph loops"
- "取消这个 ralph"
- "归档这个 ralph"

## Start

Call `ralph_start` with:

- `name`
- `taskContent`
- optionally `maxIterations`
- optionally `itemsPerIteration`
- optionally `reflectEvery`
- optionally `reflectInstructions`

If the user gives only a loose request, synthesize a reasonable task file before calling `ralph_start`.

Preferred task shape:

```markdown
# Task Title

Brief description.

## Goals
- Goal 1
- Goal 2

## Checklist
- [ ] Item 1
- [ ] Item 2
- [x] Completed item

## Verification
- Evidence, commands run, or file paths

## Notes
- Constraints, blockers, decisions, reminders
```

## Continue

When the user asks to continue the current loop or move to the next iteration, call `ralph_done`.

- If the user names a loop, pass `name`.
- Otherwise call `ralph_done` without `name`.
- Do not call `ralph_done` to begin work. It is the iteration handoff after real progress.
- Do not substitute direct calls to internal loop helpers such as `advance_loop(...)` for `ralph_done`. Those helpers are implementation details, not the agent-facing handoff API.
- For automatic continuation, require tmux and use the tmux follow-up script with `--compact-first`. If `TMUX` is not set, tell the user it cannot auto-continue.
- If auto-follow does not continue, inspect `ralph_status` or `.tmp/ralph-loop-tools/tmux-followup.log` and tell the user the loop did **not** successfully hand off yet.

## Pause / Resume / Inspect

- Pause the active loop with `ralph_stop`.
- Resume a paused loop with `ralph_resume`.
- Inspect loop state with `ralph_status`.
- List all loops, or archived loops, with `ralph_list`.
- Delete a loop with `ralph_cancel`.
- Archive a paused or completed loop with `ralph_archive`.

If the user says only “继续这个 Ralph loop” but no active loop is obvious, call `ralph_status` first rather than guessing.

## Pi Parity And Limits

This skill intentionally mirrors Pi Ralph's structure and wording, but Codex cannot do everything Pi can:

- It keeps Pi-like loop state and prompt structure.
- It supports start, continue, status, resume, pause, cancel, archive, and list.
- It uses tmux paste automation for Pi-like compact-then-follow-up when Codex is running inside tmux.
- The tmux automation uses Codoxear-style Codex busy-state checks from the pane's `rollout-*.jsonl` log, with a pane-text fallback for busy and compacting hints.
- The tmux worker waits for the originating turn to emit `task_complete`/`turn_complete`/`turn_aborted` before starting idle checks, so Stop-hook timing races do not cancel follow-up early.
- The supported no-source deployment installs lifecycle hooks through the workspace config layer rather than relying on plugin-manifest hook loading.
- The Stop hook schedules the tmux follow-up worker in the background and the worker owns the duplicate-prevention lock.
- The loop state now surfaces pending continuation plus the most recent follow-up failure, so `ralph_status` can explain why an auto-follow did not continue.
- Current-loop resolution is session-isolated from hook `session_id` or, outside hooks, from `CODEX_THREAD_ID` / `CODEX_WEB_RESUME_SESSION_ID`, so hooks and implicit `ralph_done` / `ralph_status` lookups do not bleed across Codex sessions.
- If a Ralph process has no session id at all, it should not implicitly adopt another session's owned loop.
- UserPromptSubmit guidance is keyed to Ralph-generated next-prompt fingerprints, not to heuristic intent matching on the user's prose.
- It does not support automatic follow-up outside tmux.
- It does not auto-compact the thread through a Codex host API.
- It cannot observe the assistant's emitted `<promise>COMPLETE</promise>` directly, so that marker must be written into the task file if you want to use it.
