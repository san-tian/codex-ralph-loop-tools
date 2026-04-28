# Ralph Loop Tools

Repo-local Codex plugin that brings Pi-style `ralph-wiggum` loops into Codex.

Canonical GitHub repository: `https://github.com/san-tian/codex-ralph-loop-tools`.

It provides:

- `ralph_start`
- `ralph_done`
- `ralph_status`
- `ralph_resume`
- `ralph_stop`
- `ralph_list`
- `ralph_cancel`
- `ralph_archive`

When Codex exposes those MCP tools through its host layer, the model-visible callable names may be host-qualified forms such as `mcp__ralph-loop-tools__ralph_start` and `mcp__ralph-loop-tools__ralph_done` rather than bare `ralph_start` / `ralph_done` names.

The plugin stores loop state under `.tmp/ralph-loop-tools/`, injects Ralph control context only for real Ralph-generated iteration prompts through Codex hooks, and can do Pi-like automatic compact-then-follow-up through tmux.

The hook-injected context is not just status text. For actual Ralph iteration turns, it tells the agent that updating `task.md` alone does not hand off an iteration and that Ralph operations must use the Ralph control surface such as `ralph_start`, `ralph_status`, and `ralph_done`.

To avoid polluting unrelated work, `SessionStart` stays silent and `UserPromptSubmit` injects Ralph context only when the submitted prompt exactly matches the next-iteration prompt previously produced by `ralph_start`, `ralph_done`, `ralph_resume`, or the tmux follow-up worker.

## Dependencies

This repository is intended for Codex-oriented use and is published under a GitHub repository name that includes `codex`.

Runtime and operator dependencies:

- `python3` — required for the MCP server, hook entrypoint, worker script, and local verification commands.
- `tmux` — required for automatic compact-then-follow-up. Without `tmux`, manual Ralph loop control still works, but auto-continue does not.
- Codex CLI with plugin loading plus workspace hook support — Ralph depends on Codex loading the plugin and the generated workspace `.codex/hooks.json` file.

Behavior notes tied to those dependencies:

- If `TMUX` is unset, the plugin must report that automatic follow-up is unavailable.
- The worker prefers Linux `/proc` rollout-log inspection for Codex busy/idle detection, first choosing the rollout file that contains the triggering turn id when multiple logs are open for the pane.
- If no rollout file can be matched by trigger turn, the worker falls back to the current Codex session id in the rollout filename, then to tmux pane text when no rollout log is available.

Command note:

- `rtk` is **not** a runtime dependency of this project. It is only a shell wrapper used in the original `ccss` workspace where this plugin was developed.
- This public README shows raw commands such as `python3 ...`. If you are operating inside the original `ccss` workspace, you may optionally prefix those commands with `rtk`.

## Read This First

- This README is written for both humans and coding agents.
- If an agent only gets this file, it should be able to configure the plugin, verify it, and teach the user how to use it.
- Automatic follow-up is tmux-only. If `TMUX` is unset, the plugin still works, but auto-continue is unavailable.
- The plugin resolves hook-scoped Ralph state from the hook event's `session_id` first, then falls back to `CODEX_THREAD_ID` / `CODEX_WEB_RESUME_SESSION_ID` for other paths such as direct tool calls and tmux follow-up workers.
- If a process has no session id at all, Ralph will not implicitly adopt another session's owned loop.

## Agent Runbook

If you are an agent helping a user configure this plugin in the `ccss` workspace, do these steps in order.

### 1. Install or refresh the Codex plugin cache

Run from the source plugin checkout:

```bash
cd /vePFS-Mindverse/user/intern/ccss/plugins/ralph-loop-tools
python3 scripts/install_codex_plugin.py
```

This keeps the always-discovered Codex plugin locations synchronized:

- `/root/.agents/plugins/marketplace.json` — home marketplace discovered regardless of the active cwd
- `/root/plugins/ralph-loop-tools` — home-local plugin source mirror used by that marketplace
- `/root/.codex/plugins/cache/workspace-local/ralph-loop-tools/<manifest-version>` — installed plugin cache loaded by Codex sessions, currently `0.3.0`
- `/root/.codex/config.toml` — contains `[plugins."ralph-loop-tools@workspace-local"]` with `enabled = true`

The installer rewrites the installed mirror/cache `.mcp.json` with absolute `cwd` and absolute `scripts/ralph_loop_mcp_server.py` paths pointing at the stable home mirror under `/root/plugins/ralph-loop-tools`. This avoids Codex sessions started from another project resolving the server path against that project's cwd, which otherwise can leave the Ralph MCP entry stuck in `booting` until the host times out.

Why this exists: the original `ccss/.agents/plugins/marketplace.json` is only discovered when Codex is started from the `ccss` tree or a repo whose root resolves there. This plugin is now its own standalone Git repo, so sessions started inside `plugins/ralph-loop-tools/` can otherwise refresh plugins and report that `ralph-loop-tools@workspace-local` no longer exists.

### 2. Install the supported hooks

Run:

```bash
cd /vePFS-Mindverse/user/intern/ccss
python3 plugins/ralph-loop-tools/scripts/install_workspace_hooks.py
```

This writes the supported workspace hook file:

- `/vePFS-Mindverse/user/intern/ccss/.codex/hooks.json`

That file is the recommended activation path. Do not rely on plugin-manifest hook loading alone.

### 3. Run the verification checks

Run:

```bash
cd /vePFS-Mindverse/user/intern/ccss/plugins/ralph-loop-tools
python3 -m py_compile scripts/ralph_loop_mcp_server.py scripts/ralph_hook.py scripts/ralph_tmux_followup.py scripts/ralph_loop_supervisor.py scripts/install_workspace_hooks.py scripts/test_ralph_tmux_followup.py
python3 -m py_compile scripts/install_codex_plugin.py scripts/test_install_codex_plugin.py
python3 -m unittest -q scripts.test_ralph_tmux_followup scripts.test_install_codex_plugin
python3 scripts/install_codex_plugin.py --check
python3 scripts/install_workspace_hooks.py --check
python3 -m json.tool hooks.json >/dev/null
python3 -m json.tool .codex-plugin/plugin.json >/dev/null
env -u TMUX python3 scripts/ralph_tmux_followup.py --dry-run
```

Expected results:

- compile succeeds
- unit tests pass
- plugin install check reports `up to date`
- hook install check reports `up to date`
- JSON checks succeed
- the final command exits non-zero and says `TMUX is not set`

### 4. Report the environment honestly

Tell the user:

- whether the plugin cache install is current
- whether hooks are installed and current
- whether tests passed
- whether the current session is inside tmux
- if not in tmux, that automatic follow-up is unavailable in this environment

### 5. Teach the user the natural-language controls

Show the user examples like:

```text
开一个 ralph loop，名字叫 ios-reconnect。
目标是修好 iOS attach/reconnect。
每轮处理 3 个 checklist 项，每 5 轮反思一次。

继续这个 ralph loop
查看 Ralph 状态
暂停这个 Ralph loop
恢复 ralph ios-reconnect
取消 ralph ios-reconnect
归档 ralph ios-reconnect
列出 archived ralph loops
```

### 6. Explain the one rule that users often miss

Updating the task file alone does not hand off the iteration.

An iteration advances only when:

- `ralph_done` succeeds, or
- tmux auto-follow succeeds and pastes the next prompt

Agents must not substitute direct calls to internal helper functions such as `advance_loop(...)` for `ralph_done`. Those helpers are implementation details. Bypassing `ralph_done` can advance the saved iteration counter without the supported handoff semantics, and `advance_loop(..., record_prompt_trigger=False)` in particular can leave the loop on a new iteration without refreshing the next-prompt fingerprint that Ralph hooks expect for the next turn.

The same rule applies if the host session appears to know about the Ralph plugin but does not actually expose callable Ralph MCP tools under either the bare plugin names (`ralph_*`) or the host-qualified Codex MCP names (`mcp__ralph-loop-tools__ralph_*`). In that case, the agent should report that the official Ralph control surface is unavailable in this Codex session. It should not treat direct reads or writes under `.tmp/ralph-loop-tools/` as an equivalent replacement for the missing MCP tool surface.

Ralph current-loop ownership is also session-scoped. If a loop is still `active` in another Codex session, naming that loop explicitly does not bypass isolation: mutating operations such as `ralph_done`, `ralph_stop`, forced restart of the same active loop, and `ralph_cancel` now fail instead of letting one session silently advance or destroy another session's active loop.

## Copyable Agent Prompt

If you want to hand this setup job to another agent, give it this block:

```text
Configure and verify the Ralph Loop Tools plugin in /vePFS-Mindverse/user/intern/ccss.

Do these steps exactly:
1. Run: cd /vePFS-Mindverse/user/intern/ccss/plugins/ralph-loop-tools && python3 scripts/install_codex_plugin.py
2. Verify /root/.agents/plugins/marketplace.json contains ralph-loop-tools -> ./plugins/ralph-loop-tools.
3. Verify /root/.codex/config.toml contains [plugins."ralph-loop-tools@workspace-local"] with enabled = true.
4. Run: cd /vePFS-Mindverse/user/intern/ccss && python3 plugins/ralph-loop-tools/scripts/install_workspace_hooks.py
5. Run verification commands from /vePFS-Mindverse/user/intern/ccss/plugins/ralph-loop-tools:
   - python3 -m py_compile scripts/ralph_loop_mcp_server.py scripts/ralph_hook.py scripts/ralph_tmux_followup.py scripts/ralph_loop_supervisor.py scripts/install_workspace_hooks.py scripts/test_ralph_tmux_followup.py
   - python3 -m py_compile scripts/install_codex_plugin.py scripts/test_install_codex_plugin.py
   - python3 -m unittest -q scripts.test_ralph_tmux_followup scripts.test_install_codex_plugin
   - python3 scripts/install_codex_plugin.py --check
   - python3 scripts/install_workspace_hooks.py --check
   - python3 -m json.tool hooks.json >/dev/null
   - python3 -m json.tool .codex-plugin/plugin.json >/dev/null
   - env -u TMUX python3 scripts/ralph_tmux_followup.py --dry-run
6. If TMUX is unset in the active Codex session, explicitly tell the user automatic follow-up is unavailable in this environment.
7. Teach the user these commands: start loop, continue loop, status, pause, resume, cancel, archive.
8. Remind the user that editing the task file alone does not advance the loop; ralph_done or successful tmux auto-follow is required.
```

## What Users Can Do

### Start a loop

Ask naturally in chat:

```text
开一个 ralph loop，名字叫 wezterm-ios-reconnect。
目标是修好 iOS attach/reconnect。
每轮处理 3 个 checklist 项，每 5 轮反思一次。
```

The agent should translate that into `ralph_start` with a full Markdown task body.

### Continue a loop

```text
继续这个 ralph loop
```

If there is no obvious current loop, the agent should inspect `ralph_status` instead of guessing.

### Pause, resume, inspect, archive, cancel

```text
暂停这个 Ralph loop
查看 Ralph 状态
恢复 ralph wezterm-ios-reconnect
归档 ralph wezterm-ios-reconnect
取消 ralph wezterm-ios-reconnect
列出 archived ralph loops
```

## Task Contract

The managed task file should normally look like this:

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

The loop completes when either:

- the checklist is fully checked, or
- the task file contains `<promise>COMPLETE</promise>`

## Tmux Auto-Follow

Pi-like automatic compact-then-follow-up requires the current Codex TUI to run inside tmux.

Behavior:

1. `Stop` hook notices the active Ralph loop is not complete.
2. If `TMUX` is set, it schedules `scripts/ralph_tmux_followup.py` in the background.
3. The worker waits for the triggering turn to reach `task_complete`, `turn_complete`, or `turn_aborted`.
4. If the pane has multiple writable `rollout-*.jsonl` files open, it chooses the one that contains the triggering turn before waiting for idle.
5. It waits for Codex idle state.
6. It sends `/compact`.
7. It waits for compact to finish.
8. It previews the next Ralph prompt.
9. It pastes the prompt into the tmux pane using tmux bracketed paste so a multi-line Ralph prompt stays one draft, then presses Enter.
10. Only after successful delivery does it commit the next iteration state.

If tmux delivery fails, the loop stays on the previous iteration and `ralph_status` reports the failure.

Manual examples:

```bash
cd /vePFS-Mindverse/user/intern/ccss
python3 plugins/ralph-loop-tools/scripts/ralph_tmux_followup.py wezterm-ios-reconnect --pane %12 --compact-first
python3 plugins/ralph-loop-tools/scripts/ralph_tmux_followup.py wezterm-ios-reconnect --dry-run
```

## Troubleshooting

### `TMUX is not set`

Meaning:

- the plugin is installed, but automatic follow-up cannot run in this Codex session

What to tell the user:

- manual Ralph loops still work
- auto-continue requires running Codex inside tmux

### `ralph loop isn't continuing automatically`

Check:

- `ralph_status`
- `.tmp/ralph-loop-tools/tmux-followup.log`

Common causes:

- current Codex session is not in tmux
- worker chose the wrong rollout log in an older build; current builds select by triggering turn id first
- pane never reached idle state
- `/compact` did not finish
- tmux paste delivery failed
- an agent bypassed `ralph_done` and directly called internal loop helpers such as `advance_loop(...)`, so the iteration counter changed without a real Ralph handoff

### `editing the task file did nothing`

That is expected. The loop only hands off on `ralph_done` or successful tmux auto-follow.

### `the session shows the Ralph plugin/skill, but there are no usable Ralph tools`

Meaning:

- plugin discovery or skill injection happened, but the host did not expose the Ralph MCP server's callable tools to this agent session under either bare `ralph_*` names or host-qualified names like `mcp__ralph-loop-tools__ralph_start`

What to tell the user:

- absence of bare `ralph_*` names alone is not enough to diagnose failure, because some Codex hosts qualify MCP tools as `mcp__<server>__<tool>`
- this is a Codex host MCP exposure problem only when neither the bare Ralph names nor the host-qualified Ralph names are available
- the official Ralph control surface is unavailable in this session
- the agent should not inspect or mutate `.tmp/ralph-loop-tools/` as a substitute for the missing official Ralph tools

Safe operator check:

- run `python3 scripts/install_codex_plugin.py --check`; if it reports out-of-date paths, run `python3 scripts/install_codex_plugin.py` and restart or reload the Codex session so tool discovery can rebuild from the refreshed cache
- directly probe `scripts/ralph_loop_mcp_server.py` over stdio with `initialize` and `tools/list`; if that returns the raw `ralph_*` tools, the plugin server is healthy and the remaining gap is between the host session and MCP tool injection/namespacing

### `Ralph MCP stays booting for a long time`

First check whether the installed cache `.mcp.json` contains absolute paths. The source `.mcp.json` is intentionally relative for portability, but the installed mirror/cache should be generated by `scripts/install_codex_plugin.py` with an absolute `cwd` and absolute `scripts/ralph_loop_mcp_server.py` argument pointing at `/root/plugins/ralph-loop-tools`.

If the installed MCP config still contains `"cwd": "."` plus a relative `scripts/ralph_loop_mcp_server.py` path, Codex sessions started from unrelated projects can launch Python against the wrong cwd. Refresh the install/cache with `python3 scripts/install_codex_plugin.py` and restart or reload Codex.

If the paths are already absolute but Codex still reports startup timeouts, verify that the installed server echoes the MCP `initialize.params.protocolVersion` value in its initialize response. Newer Codex MCP clients can request a newer protocol version than the server's default constant; returning the requested version avoids a slow host-side startup failure even though direct `tools/list` probes may look healthy.

Also verify the stdio framing used by the client. Ralph accepts standard `Content-Length` MCP frames and newline-delimited JSON-RPC; it answers in the same framing style as the first received request. A framing mismatch can make the server exit immediately while Codex surfaces the failure only after its startup timeout.

### `this session is reading another session's loop`

The current implementation isolates implicit current-loop resolution by the hook event `session_id` first and by `CODEX_THREAD_ID` / `CODEX_WEB_RESUME_SESSION_ID` elsewhere. A process with no session id should no longer auto-adopt a foreign owned loop. If this still happens, inspect the hook payload/session environment and the files under:

- `.tmp/ralph-loop-tools/current-by-session/`

## Repository Layout

- `.codex-plugin/plugin.json` — plugin manifest
- `.mcp.json` — local MCP server wiring
- `hooks.json` — plugin-local hook definition
- `scripts/ralph_loop_mcp_server.py` — MCP server and Ralph state machine
- `scripts/ralph_hook.py` — Stop hook dispatcher plus exact Ralph-prompt guidance injection
- `scripts/ralph_tmux_followup.py` — tmux compact-and-paste worker
- `scripts/install_codex_plugin.py` — writes the home marketplace, home plugin mirror, Codex plugin cache, and config enablement
- `scripts/install_workspace_hooks.py` — writes the supported workspace hook file
- `scripts/test_ralph_tmux_followup.py` — plugin regression tests
- `scripts/test_install_codex_plugin.py` — install/cache regression tests
- `skills/ralph/SKILL.md` — main natural-language skill
- `skills/ralph-wiggum/SKILL.md` — alias skill

## Design Notes

This plugin intentionally mirrors Pi Ralph behavior where Codex allows it:

- Pi-style loop state and iteration prompts
- natural-language start/continue/status controls
- reflection cadence
- tmux-based compact-then-follow-up
- pending continuation and failure visibility in `ralph_status`

Current limits:

- automatic follow-up is tmux-only
- no non-tmux auto-follow fallback should be offered unless explicitly requested
- compact uses Codex TUI `/compact`, not a host API
- the plugin depends on workspace hook installation for the supported no-source path
