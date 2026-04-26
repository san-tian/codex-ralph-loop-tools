#!/usr/bin/env python3
"""Codex lifecycle hooks for tmux-only Ralph follow-up."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import ralph_loop_mcp_server as ralph


FOLLOWUP_SCRIPT = Path(__file__).resolve().with_name("ralph_tmux_followup.py")


def read_input() -> dict[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, separators=(",", ":")))


def hook_session_id(event: dict[str, object] | None = None) -> str | None:
    if isinstance(event, dict):
        session_id = ralph.normalize_session_id(str(event.get("session_id") or ""))
        if session_id:
            return session_id
    return ralph.current_session_id()


def active_state(session_id: str | None = None) -> ralph.LoopState | None:
    try:
        loop_name = ralph.get_current_loop_name(session_id=session_id)
        if not loop_name:
            return None
        state = ralph.load_loop_state(loop_name)
        return state if state.status == "active" else None
    except Exception:
        return None


def completion_reason(state: ralph.LoopState) -> str | None:
    task_text = ralph.read_task_text(Path(state.task_file))
    checklist_total, checklist_done = ralph.checklist_counts(task_text)
    if ralph.COMPLETE_MARKER in task_text:
        return "Completion marker found in task file."
    if checklist_total > 0 and checklist_total == checklist_done:
        return "Checklist is fully complete."
    return None


def ralph_context(state: ralph.LoopState) -> str:
    lines = [
        "[RALPH LOOP]",
        f"Active loop: {state.name}",
        f"Iteration: {ralph.format_progress(state)}",
        f"Task file: {ralph.relative_to_repo(Path(state.task_file))}",
    ]
    if state.pending_continuation:
        lines.append("Pending continuation: yes")
    if state.last_continuation_error:
        lines.append(f"Last follow-up error: {state.last_continuation_error}")
    lines.extend(
        [
            "Ralph control rule: updating the task file alone does not hand off the iteration.",
            "If this turn is continuing the Ralph loop, you must explicitly use Ralph control: inspect with ralph_status when needed, and call ralph_done only after real progress is recorded.",
            "If pending continuation is already yes, do not call ralph_done blindly; inspect ralph_status or .tmp/ralph-loop-tools/tmux-followup.log first.",
            "Automatic follow-up requires tmux. If TMUX is unset, tell the user it is unavailable.",
        ]
    )
    return "\n".join(lines)


def ralph_available_context() -> str:
    return "\n".join(
        [
            "[RALPH AVAILABLE]",
            "Ralph control rule: updating the task file alone does not hand off the iteration.",
            "When the user asks to start, continue, pause, resume, inspect, cancel, or archive a Ralph loop, explicitly use Ralph control tools such as ralph_start, ralph_status, ralph_done, ralph_stop, ralph_resume, ralph_cancel, or ralph_archive instead of treating Ralph as background context only.",
            "Automatic follow-up requires tmux. If TMUX is unset, tell the user it is unavailable.",
        ]
    )


def handle_session_start() -> None:
    emit({"continue": True})


def handle_user_prompt_submit(event: dict[str, object]) -> None:
    session_id = hook_session_id(event)
    prompt = str(event.get("prompt") or "")
    loop_name = ralph.consume_expected_prompt(prompt, session_id=session_id)
    if not loop_name:
        emit({"continue": True})
        return

    try:
        state = ralph.load_loop_state(loop_name)
    except ValueError:
        emit({"continue": True})
        return
    if state.status != "active":
        emit({"continue": True})
        return

    emit(
        {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": ralph_context(state),
            },
        }
    )


def current_pane_id() -> str:
    completed = subprocess.run(
        ["tmux", "display-message", "-p", "#{pane_id}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "failed to read current tmux pane id")
    pane_id = completed.stdout.strip()
    if not pane_id.startswith("%"):
        raise RuntimeError(f"unexpected tmux pane id: {pane_id!r}")
    return pane_id


def schedule_followup(
    state: ralph.LoopState,
    trigger_turn_id: str | None = None,
    *,
    session_id: str | None = None,
) -> str:
    pane_id = current_pane_id()
    log_path = ralph.STATE_ROOT / "tmux-followup.log"
    ralph.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(FOLLOWUP_SCRIPT), state.name, "--compact-first", "--pane", pane_id]
    if session_id:
        argv.extend(["--session-id", session_id])
    if trigger_turn_id:
        argv.extend(["--trigger-turn-id", trigger_turn_id])
    ralph.mark_pending_continuation(state)
    try:
        with log_path.open("ab") as log_file:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                cwd=ralph.REPO_ROOT,
                start_new_session=True,
                close_fds=True,
            )
        if trigger_turn_id:
            return (
                f"Ralph compact/follow-up scheduled for tmux pane {pane_id} "
                f"after turn {trigger_turn_id} completes."
            )
        return f"Ralph compact/follow-up scheduled for tmux pane {pane_id}."
    except Exception as exc:
        ralph.clear_pending_continuation(state, error=f"failed to schedule follow-up: {exc}")
        return f"Ralph follow-up failed to schedule: {exc}"


def handle_stop(event: dict[str, object]) -> None:
    session_id = hook_session_id(event)
    state = active_state(session_id=session_id)
    if not state:
        emit({"continue": True})
        return

    last_message = str(event.get("last_assistant_message") or "")
    if ralph.COMPLETE_MARKER in last_message:
        ralph.complete_loop(state)
        emit({"continue": True, "systemMessage": f'Ralph loop "{state.name}" completed by assistant marker.'})
        return

    reason = completion_reason(state)
    if reason:
        ralph.complete_loop(state)
        emit({"continue": True, "systemMessage": f'Ralph loop "{state.name}" completed: {reason}'})
        return

    if state.pending_continuation:
        emit(
            {
                "continue": True,
                "systemMessage": (
                    f'Ralph loop "{state.name}" is already preparing the next iteration. '
                    "Check `ralph_status` or `.tmp/ralph-loop-tools/tmux-followup.log` if it does not continue."
                ),
            }
        )
        return

    if not os.environ.get("TMUX"):
        emit({"continue": True, "systemMessage": "Ralph automatic follow-up unavailable: TMUX is not set."})
        return

    trigger_turn_id = str(event.get("turn_id") or "").strip() or None
    message = schedule_followup(state, trigger_turn_id, session_id=session_id)
    emit({"continue": True, "systemMessage": message})


def main() -> int:
    try:
        event = read_input()
        hook_event_name = str(event.get("hook_event_name") or "")
        if hook_event_name == "SessionStart":
            handle_session_start()
        elif hook_event_name == "UserPromptSubmit":
            handle_user_prompt_submit(event)
        elif hook_event_name == "Stop":
            handle_stop(event)
        else:
            emit({"continue": True})
        return 0
    except Exception as exc:
        emit({"continue": True, "systemMessage": f"Ralph hook failed: {exc}"})
        return 0


if __name__ == "__main__":
    sys.exit(main())
