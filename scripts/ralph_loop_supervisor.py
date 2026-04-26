#!/usr/bin/env python3
"""External supervisor for Codex Ralph loops."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import ralph_loop_mcp_server as ralph


SUPERVISOR_HEADER = """You are running under the external Ralph supervisor.

Rules:
- Work on exactly this Ralph iteration.
- Update the managed task file with progress and verification evidence.
- If the task is fully complete, check every completed checklist item or add <promise>COMPLETE</promise> to the task file.
- If more work remains, stop after meaningful progress; the supervisor will advance the loop.
- Do not call ralph_done yourself; the supervisor owns iteration handoff.
"""


def build_iteration_prompt(state: ralph.LoopState) -> str:
    return f"{SUPERVISOR_HEADER}\n\n{ralph.render_iteration_prompt(state)}"


def supervisor_state_path(loop_name: str) -> Path:
    return ralph.loop_dir(loop_name) / "supervisor.json"


def read_supervisor_state(loop_name: str) -> dict[str, str]:
    path = supervisor_state_path(loop_name)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_supervisor_state(loop_name: str, payload: dict[str, str]) -> None:
    path = supervisor_state_path(loop_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def extract_thread_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def resolve_session_id(loop_name: str, args: argparse.Namespace) -> str | None:
    if args.session_id:
        return args.session_id
    if args.no_resume:
        return None
    state = read_supervisor_state(loop_name)
    session_id = state.get("session_id")
    return session_id if session_id else None


def run_codex_exec(prompt: str, loop_name: str, args: argparse.Namespace, output_path: Path) -> int:
    command = ["codex", "exec"]
    session_id = resolve_session_id(loop_name, args)
    if session_id:
        command.extend(["resume", session_id])
    elif args.resume_last:
        command.extend(["resume", "--last"])
    if args.model:
        command.extend(["--model", args.model])
    if args.profile:
        command.extend(["--profile", args.profile])
    if args.sandbox:
        command.extend(["--sandbox", args.sandbox])
    if args.full_auto:
        command.append("--full-auto")
    if args.dangerously_bypass_approvals_and_sandbox:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    if args.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    command.append("--json")
    command.extend(["--cd", str(ralph.REPO_ROOT)])
    command.extend(["--output-last-message", str(output_path)])
    command.append("-")

    if args.print_command:
        print("$ " + " ".join(command), flush=True)
    if args.dry_run:
        print(prompt)
        return 0

    completed = subprocess.run(
        command,
        input=prompt,
        text=True,
        cwd=ralph.REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    thread_id = extract_thread_id(completed.stdout)
    if thread_id:
        write_supervisor_state(loop_name, {"session_id": thread_id})
    return completed.returncode


def is_complete(state: ralph.LoopState) -> tuple[bool, str]:
    task_text = ralph.read_task_text(Path(state.task_file))
    checklist_total, checklist_done = ralph.checklist_counts(task_text)
    if ralph.COMPLETE_MARKER in task_text:
        return True, "Completion marker found in task file."
    if checklist_total > 0 and checklist_total == checklist_done:
        return True, "Checklist is fully complete."
    return False, ""


def load_active_state(name: str | None) -> ralph.LoopState:
    loop_name = ralph.resolve_loop_name(name)
    state = ralph.load_loop_state(loop_name)
    if state.status != "active":
        raise RuntimeError(f'Loop "{loop_name}" is not active (status={state.status}).')
    return state


def supervise(args: argparse.Namespace) -> int:
    ralph.ensure_state_dirs()
    output_path = Path(args.output_last_message).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for _round in range(1, args.rounds + 1):
        state = load_active_state(args.name)
        complete, reason = is_complete(state)
        if complete:
            ralph.complete_loop(state)
            print(ralph.render_complete_prompt(state, reason))
            return 0

        print(f"[ralph-supervisor] running {state.name} iteration {ralph.format_progress(state)}", flush=True)
        prompt = build_iteration_prompt(state)
        exit_code = run_codex_exec(prompt, state.name, args, output_path)
        if exit_code != 0:
            return exit_code
        if args.dry_run:
            return 0

        latest = ralph.load_loop_state(state.name)
        complete, reason = is_complete(latest)
        if complete:
            ralph.complete_loop(latest)
            print(ralph.render_complete_prompt(latest, reason))
            return 0

        next_prompt = ralph.advance_loop({"name": latest.name})
        print(next_prompt if args.verbose_prompts else f"[ralph-supervisor] advanced to next iteration", flush=True)

    print(f"[ralph-supervisor] stopped after supervisor rounds={args.rounds}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an active Ralph loop via repeated codex exec calls.")
    parser.add_argument("name", nargs="?", help="Loop name. Defaults to current active loop.")
    parser.add_argument("--rounds", type=int, default=1, help="Maximum supervisor rounds to run. Default: 1.")
    parser.add_argument("--session-id", help="Explicit Codex session/thread id to resume.")
    parser.add_argument("--no-resume", action="store_true", help="Do not resume a stored session id; start fresh exec threads.")
    parser.add_argument("--resume-last", action="store_true", help="Fallback to codex exec resume --last when no loop session id is stored. Risky; prefer --session-id.")
    parser.add_argument("--model", help="Model passed to codex exec.")
    parser.add_argument("--profile", help="Config profile passed to codex exec.")
    parser.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], help="Sandbox mode passed to codex exec.")
    parser.add_argument("--full-auto", action="store_true", help="Pass --full-auto to codex exec.")
    parser.add_argument("--dangerously-bypass-approvals-and-sandbox", action="store_true", help="Pass Codex's unsafe no-sandbox/no-approval flag.")
    parser.add_argument("--skip-git-repo-check", action="store_true", help="Pass --skip-git-repo-check to codex exec.")
    parser.add_argument("--output-last-message", default=str(ralph.STATE_ROOT / "supervisor-last-message.md"), help="Where codex exec writes its last message.")
    parser.add_argument("--verbose-prompts", action="store_true", help="Print each next-iteration prompt after advancing.")
    parser.add_argument("--print-command", action="store_true", help="Print the codex exec command before running it.")
    parser.add_argument("--dry-run", action="store_true", help="Print the first prompt without running codex exec.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be >= 1")
    return supervise(args)


if __name__ == "__main__":
    sys.exit(main())
