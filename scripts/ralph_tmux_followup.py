#!/usr/bin/env python3
"""Send the next Ralph prompt into the current Codex tmux pane."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import ralph_loop_mcp_server as ralph


BUSY_PANE_PATTERNS = (
    re.compile(r"esc\s+to\s+interrupt", re.IGNORECASE),
    re.compile(r"compacting\s+(?:context|conversation)", re.IGNORECASE),
)
CALL_ITEM_TYPES = {
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "web_search_call",
    "local_shell_call",
}
TERMINAL_TURN_EVENT_TYPES = {"task_complete", "turn_complete", "turn_aborted"}
LOCK_PATH = ralph.STATE_ROOT / "tmux-followup.lock"


@contextmanager
def session_env_override(session_id: str | None):
    resolved = ralph.normalize_session_id(session_id)
    if not resolved:
        yield
        return

    previous_thread = os.environ.get("CODEX_THREAD_ID")
    previous_resume = os.environ.get("CODEX_WEB_RESUME_SESSION_ID")
    os.environ["CODEX_THREAD_ID"] = resolved
    os.environ.pop("CODEX_WEB_RESUME_SESSION_ID", None)
    try:
        yield
    finally:
        if previous_thread is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = previous_thread
        if previous_resume is None:
            os.environ.pop("CODEX_WEB_RESUME_SESSION_ID", None)
        else:
            os.environ["CODEX_WEB_RESUME_SESSION_ID"] = previous_resume


def ensure_tmux() -> None:
    if not os.environ.get("TMUX"):
        raise RuntimeError("Ralph tmux follow-up requires running inside tmux; TMUX is not set.")


def run_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def current_pane_id() -> str:
    completed = run_tmux(["display-message", "-p", "#{pane_id}"])
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "failed to read current tmux pane id")
    pane_id = completed.stdout.strip()
    if not pane_id.startswith("%"):
        raise RuntimeError(f"unexpected tmux pane id: {pane_id!r}")
    return pane_id


def pane_pid(pane_id: str) -> int:
    completed = run_tmux(["display-message", "-p", "-t", pane_id, "#{pane_pid}"])
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"failed to read tmux pane pid for {pane_id}")
    raw = completed.stdout.strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"unexpected tmux pane pid: {raw!r}") from exc


def _proc_pid_uid(proc_root: Path, pid: int) -> int | None:
    try:
        return int((proc_root / str(pid)).stat().st_uid)
    except Exception:
        return None


def _proc_children(proc_root: Path, pid: int) -> list[int]:
    children_path = proc_root / str(pid) / "task" / str(pid) / "children"
    try:
        raw = children_path.read_text(encoding="utf-8").strip()
    except Exception:
        return []
    out: list[int] = []
    for value in raw.split():
        try:
            out.append(int(value))
        except ValueError:
            continue
    return out


def _proc_descendants(proc_root: Path, root_pid: int) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(_proc_children(proc_root, pid))
    return out


def _proc_fd_flags(proc_root: Path, pid: int, fd_name: str) -> int | None:
    try:
        raw = (proc_root / str(pid) / "fdinfo" / fd_name).read_text(encoding="utf-8")
    except Exception:
        return None
    for line in raw.splitlines():
        if not line.startswith("flags:"):
            continue
        try:
            return int(line.split(":", 1)[1].strip().split()[0], 8)
        except ValueError:
            return None
    return None


def _fd_has_write_intent(flags: int) -> bool:
    return (int(flags) & int(os.O_ACCMODE)) in (int(os.O_WRONLY), int(os.O_RDWR))


def _is_codex_rollout_log_path(path: Path) -> bool:
    return path.name.startswith("rollout-") and path.suffix == ".jsonl" and "/sessions/" in str(path)


def open_writable_rollout_logs_for_pane(pane_id: str) -> list[Path]:
    if sys.platform == "darwin":
        return []
    proc_root = Path("/proc")
    root_pid = pane_pid(pane_id)
    uid = os.getuid()
    logs: set[Path] = set()
    for pid in _proc_descendants(proc_root, root_pid):
        puid = _proc_pid_uid(proc_root, pid)
        if puid is not None and puid != uid:
            continue
        fd_dir = proc_root / str(pid) / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except Exception:
            continue
        for ent in entries:
            flags = _proc_fd_flags(proc_root, pid, ent.name)
            if flags is None or not _fd_has_write_intent(flags):
                continue
            try:
                target = os.readlink(ent)
            except OSError:
                continue
            if target.endswith(" (deleted)") or not target.endswith(".jsonl") or not target.startswith("/"):
                continue
            path = Path(target)
            if _is_codex_rollout_log_path(path):
                logs.add(path)
    return sorted(logs, key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)


def _read_jsonl_tail(path: Path, max_scan_bytes: int) -> list[dict[str, Any]]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - max_scan_bytes))
        raw = fh.read().decode("utf-8", errors="replace")
    if size > max_scan_bytes:
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
    objs: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _has_assistant_output_text(obj: dict[str, Any]) -> bool:
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "assistant":
        return False
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict)
        and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
        and bool(part.get("text"))
        for part in content
    )


def rollout_has_terminal_turn(
    path: Path, turn_id: str | None, max_scan_bytes: int = 8 * 1024 * 1024
) -> bool | None:
    if not turn_id:
        return None
    scan = min(256 * 1024, max_scan_bytes)
    if scan <= 0:
        return None

    while True:
        objs = _read_jsonl_tail(path, scan)
        if not objs:
            return None
        for obj in objs:
            if obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("turn_id") != turn_id:
                continue
            if payload.get("type") in TERMINAL_TURN_EVENT_TYPES:
                return True
        if scan >= max_scan_bytes:
            return False
        scan *= 2


def rollout_contains_turn(
    path: Path, turn_id: str | None, max_scan_bytes: int = 8 * 1024 * 1024
) -> bool | None:
    if not turn_id:
        return None
    scan = min(256 * 1024, max_scan_bytes)
    if scan <= 0:
        return None

    while True:
        objs = _read_jsonl_tail(path, scan)
        if not objs:
            return None
        for obj in objs:
            payload = obj.get("payload")
            if isinstance(payload, dict) and payload.get("turn_id") == turn_id:
                return True
        if scan >= max_scan_bytes:
            return False
        scan *= 2


def choose_best_rollout_log(
    logs: list[Path], *, trigger_turn_id: str | None = None, session_id: str | None = None
) -> Path | None:
    if not logs:
        return None

    if trigger_turn_id:
        turn_matches = [path for path in logs if rollout_contains_turn(path, trigger_turn_id) is True]
        if turn_matches:
            return turn_matches[0]

    if session_id:
        session_matches = [path for path in logs if session_id in path.name]
        if session_matches:
            return session_matches[0]

    return logs[0]


def _assistant_message_ends_turn(payload: dict[str, Any]) -> bool:
    return payload.get("phase") == "final_answer" or payload.get("end_turn") is True


def compute_idle_from_rollout(path: Path, max_scan_bytes: int = 8 * 1024 * 1024) -> bool | None:
    scan = min(256 * 1024, max_scan_bytes)
    if scan <= 0:
        return None
    saw_terminal_signal = False
    idle = True

    while True:
        objs = _read_jsonl_tail(path, scan)
        saw_terminal_signal = False
        idle = True
        for obj in objs:
            typ = obj.get("type")
            if typ == "event_msg":
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                event_type = payload.get("type")
                if event_type == "user_message" and isinstance(payload.get("message"), str):
                    saw_terminal_signal = True
                    idle = False
                    continue
                if event_type == "agent_message" and isinstance(payload.get("message"), str) and payload.get("message"):
                    saw_terminal_signal = True
                    idle = False
                    continue
                if event_type == "agent_reasoning":
                    saw_terminal_signal = True
                    idle = False
                    continue
                if event_type in ("turn_aborted", "thread_rolled_back", "task_complete", "turn_complete"):
                    saw_terminal_signal = True
                    idle = True
                    continue
            if typ == "response_item":
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                item_type = payload.get("type")
                if _has_assistant_output_text(obj):
                    saw_terminal_signal = True
                    idle = _assistant_message_ends_turn(payload)
                    continue
                if item_type == "reasoning" or item_type in CALL_ITEM_TYPES:
                    saw_terminal_signal = True
                    idle = False
                    continue
        if saw_terminal_signal or scan >= max_scan_bytes:
            break
        scan *= 2

    if not objs:
        return None
    if not saw_terminal_signal:
        return True if path.stat().st_size <= 128 * 1024 else False
    return idle


def pane_has_busy_hint(pane_id: str) -> bool:
    completed = run_tmux(["capture-pane", "-p", "-t", pane_id, "-S", "-120"])
    if completed.returncode != 0:
        return False
    text = completed.stdout
    return any(pattern.search(text) for pattern in BUSY_PANE_PATTERNS)


def wait_for_turn_terminal(
    pane_id: str, trigger_turn_id: str | None, *, timeout: float, dry_run: bool
) -> None:
    if not trigger_turn_id or timeout <= 0:
        return
    if dry_run:
        print(f"# wait for trigger turn {trigger_turn_id} to complete, timeout {timeout:.1f}s")
        return

    deadline = time.monotonic() + timeout
    last_reason = f"turn {trigger_turn_id} has not completed yet."
    session_id = ralph.current_session_id()
    while True:
        terminal = None
        logs = open_writable_rollout_logs_for_pane(pane_id)
        log_path = choose_best_rollout_log(
            logs,
            trigger_turn_id=trigger_turn_id,
            session_id=session_id,
        )
        if log_path:
            terminal = rollout_has_terminal_turn(log_path, trigger_turn_id)
            last_reason = (
                f"turn {trigger_turn_id} has not reached a terminal event in {log_path}"
            )
        if terminal is True:
            return
        if terminal is None and not pane_has_busy_hint(pane_id):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"timed out waiting for triggering turn {trigger_turn_id} to complete; {last_reason}"
            )
        time.sleep(0.25)


def wait_for_idle(
    pane_id: str,
    *,
    timeout: float,
    quiet_seconds: float,
    dry_run: bool,
    preferred_log: Path | None = None,
) -> None:
    if timeout <= 0:
        return
    if dry_run:
        print(f"# wait for Codex idle state, timeout {timeout:.1f}s, quiet {quiet_seconds:.1f}s")
        return
    deadline = time.monotonic() + timeout
    idle_since: float | None = None
    last_reason = "Codex pane is not idle yet."
    session_id = ralph.current_session_id()
    while True:
        idle = None
        log_path = preferred_log if preferred_log and preferred_log.exists() else None
        if log_path is None:
            logs = open_writable_rollout_logs_for_pane(pane_id)
            log_path = choose_best_rollout_log(logs, session_id=session_id)
        if log_path:
            idle = compute_idle_from_rollout(log_path)
            last_reason = f"Codex rollout log still reports busy: {log_path}"
        if idle is None:
            busy_hint = pane_has_busy_hint(pane_id)
            idle = not busy_hint
            last_reason = "Codex pane still shows a busy/compact hint."
        now = time.monotonic()
        if idle:
            if idle_since is None:
                idle_since = now
            if now - idle_since >= quiet_seconds:
                return
        else:
            idle_since = None
        if now >= deadline:
            raise RuntimeError(f"timed out waiting for Codex idle state; {last_reason}")
        time.sleep(0.25)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _lock_is_stale(lock_path: Path, *, stale_after: float) -> bool:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return True
    pid = payload.get("pid")
    created_at = payload.get("created_at")
    if isinstance(pid, int) and _pid_is_running(pid):
        return False
    if isinstance(created_at, (int, float)) and time.time() - float(created_at) <= stale_after:
        return False
    return True


def acquire_lock(loop_name: str | None, *, stale_after: float) -> bool:
    ralph.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "name": loop_name,
        "created_at": time.time(),
    }
    while True:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if _lock_is_stale(LOCK_PATH, stale_after=stale_after):
                LOCK_PATH.unlink(missing_ok=True)
                continue
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
            fh.write("\n")
        return True


def release_lock() -> None:
    try:
        payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if payload.get("pid") == os.getpid():
        LOCK_PATH.unlink(missing_ok=True)


def is_complete(state: ralph.LoopState) -> tuple[bool, str]:
    task_text = ralph.read_task_text(Path(state.task_file))
    checklist_total, checklist_done = ralph.checklist_counts(task_text)
    if ralph.COMPLETE_MARKER in task_text:
        return True, "Completion marker found in task file."
    if checklist_total > 0 and checklist_total == checklist_done:
        return True, "Checklist is fully complete."
    return False, ""


def preview_next_prompt(loop_name: str | None, *, allow_pending: bool = False) -> str:
    name = ralph.resolve_loop_name(loop_name)
    state = ralph.load_loop_state(name)
    if state.status != "active":
        raise RuntimeError(f'Loop "{name}" is not active (status={state.status}).')
    if state.pending_continuation and not allow_pending:
        raise RuntimeError(f'Loop "{name}" is already preparing the next iteration.')

    complete, reason = is_complete(state)
    if complete:
        return ralph.render_complete_prompt(state, reason)

    preview_state = replace(state)
    preview_state.iteration += 1
    if preview_state.max_iterations > 0 and preview_state.iteration > preview_state.max_iterations:
        return ralph.render_complete_prompt(
            preview_state,
            f"Max iterations ({preview_state.max_iterations}) reached before completion.",
            stopped=True,
        )
    if ralph.should_reflect(preview_state):
        preview_state.last_reflection_at = preview_state.iteration
    return ralph.render_iteration_prompt(preview_state)


def clear_followup_pending(loop_name: str | None, *, error: str | None = None) -> None:
    name = ralph.resolve_loop_name(loop_name)
    state = ralph.load_loop_state(name)
    if state.status != "active":
        return
    ralph.clear_pending_continuation(state, error=error)


def next_prompt(loop_name: str | None, *, dry_run: bool) -> str:
    if dry_run:
        return preview_next_prompt(loop_name)
    name = ralph.resolve_loop_name(loop_name)
    return ralph.advance_loop({"name": name}, allow_pending=True, record_prompt_trigger=False)


def deliver_next_prompt(loop_name: str | None, pane_id: str, *, dry_run: bool) -> str:
    if dry_run:
        return preview_next_prompt(loop_name)
    prompt = preview_next_prompt(loop_name, allow_pending=True)
    name = ralph.resolve_loop_name(loop_name)
    ralph.set_expected_prompt(name, prompt)
    paste_prompt_into_pane(pane_id, prompt)
    next_prompt(loop_name, dry_run=False)
    return prompt


def send_line(pane_id: str, line: str) -> None:
    completed = run_tmux(["send-keys", "-t", pane_id, "--", line, "Enter"])
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"tmux send-keys failed for {line!r}")


def wait_until_ready_for_followup(
    pane_id: str,
    *,
    trigger_turn_id: str | None,
    dry_run: bool,
    compact_first: bool,
    compact_wait: float,
    wait_idle: bool,
    idle_timeout: float,
    quiet_seconds: float,
) -> None:
    if dry_run:
        wait_for_turn_terminal(
            pane_id,
            trigger_turn_id,
            timeout=idle_timeout,
            dry_run=True,
        )
        if compact_first:
            if wait_idle:
                wait_for_idle(pane_id, timeout=idle_timeout, quiet_seconds=quiet_seconds, dry_run=True)
            print("/compact")
            if compact_wait > 0:
                print(f"# minimum wait {compact_wait:.1f}s after /compact")
            if wait_idle:
                wait_for_idle(pane_id, timeout=idle_timeout, quiet_seconds=quiet_seconds, dry_run=True)
            print()
        return

    relevant_log = choose_best_rollout_log(
        open_writable_rollout_logs_for_pane(pane_id),
        trigger_turn_id=trigger_turn_id,
        session_id=ralph.current_session_id(),
    )

    wait_for_turn_terminal(
        pane_id,
        trigger_turn_id,
        timeout=idle_timeout,
        dry_run=False,
    )

    if relevant_log is None:
        relevant_log = choose_best_rollout_log(
            open_writable_rollout_logs_for_pane(pane_id),
            trigger_turn_id=trigger_turn_id,
            session_id=ralph.current_session_id(),
        )

    if wait_idle:
        wait_for_idle(
            pane_id,
            timeout=idle_timeout,
            quiet_seconds=quiet_seconds,
            dry_run=False,
            preferred_log=relevant_log,
        )

    if compact_first:
        send_line(pane_id, "/compact")
        if compact_wait > 0:
            time.sleep(compact_wait)
        if wait_idle:
            wait_for_idle(
                pane_id,
                timeout=idle_timeout,
                quiet_seconds=quiet_seconds,
                dry_run=False,
                preferred_log=relevant_log,
            )


def paste_prompt_into_pane(pane_id: str, prompt: str) -> None:

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
        tmp.write(prompt)
        tmp_path = tmp.name
    try:
        buffer_name = "ralph-next"
        load = run_tmux(["load-buffer", "-b", buffer_name, tmp_path])
        if load.returncode != 0:
            raise RuntimeError(load.stderr.strip() or "tmux load-buffer failed")
        # Use tmux bracketed paste so multi-line Ralph prompts land as one draft
        # in the Codex TUI instead of being split into multiple submitted messages.
        paste = run_tmux(["paste-buffer", "-p", "-t", pane_id, "-b", buffer_name])
        if paste.returncode != 0:
            raise RuntimeError(paste.stderr.strip() or "tmux paste-buffer failed")
        enter = run_tmux(["send-keys", "-t", pane_id, "Enter"])
        if enter.returncode != 0:
            raise RuntimeError(enter.stderr.strip() or "tmux send-keys Enter failed")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paste the next Ralph loop prompt into a tmux pane.")
    parser.add_argument("name", nargs="?", help="Loop name. Defaults to current active loop.")
    parser.add_argument("--pane", help="Target tmux pane id, e.g. %%12. Defaults to current pane.")
    parser.add_argument(
        "--session-id",
        help="Codex session/thread id to use for session-scoped Ralph state resolution.",
    )
    parser.add_argument(
        "--trigger-turn-id",
        help="Codex turn id that scheduled this follow-up. The worker waits for that turn to complete before compacting.",
    )
    parser.add_argument("--compact-first", action="store_true", help="Send /compact before the next Ralph prompt.")
    parser.add_argument("--compact-wait", type=float, default=1.0, help="Minimum seconds to wait after /compact before checking idle state. Default: 1.0.")
    parser.add_argument("--idle-timeout", type=float, default=30.0, help="Seconds to wait for the triggering turn to finish or for Codex idle state before sending input. Default: 30.0.")
    parser.add_argument("--quiet-seconds", type=float, default=1.0, help="Seconds the Codex pane/log must stay idle. Default: 1.0.")
    parser.add_argument("--lock-stale-after", type=float, default=300.0, help="Seconds after which a dead follow-up lock may be removed. Default: 300.0.")
    parser.add_argument("--no-wait-idle", action="store_true", help="Disable Codex rollout/pane idle checks.")
    parser.add_argument("--dry-run", action="store_true", help="Print the next prompt without pasting it.")
    parser.add_argument("--print-pane", action="store_true", help="Print the resolved tmux pane id.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        with session_env_override(args.session_id):
            ensure_tmux()
            pane_id = args.pane or current_pane_id()
            if args.print_pane:
                print(f"target pane: {pane_id}")
            if not args.dry_run and not acquire_lock(args.name, stale_after=args.lock_stale_after):
                raise RuntimeError("another Ralph tmux follow-up is already pending")
            try:
                wait_until_ready_for_followup(
                    pane_id,
                    trigger_turn_id=args.trigger_turn_id,
                    dry_run=args.dry_run,
                    compact_first=args.compact_first,
                    compact_wait=args.compact_wait,
                    wait_idle=not args.no_wait_idle,
                    idle_timeout=args.idle_timeout,
                    quiet_seconds=args.quiet_seconds,
                )
                prompt = deliver_next_prompt(args.name, pane_id, dry_run=args.dry_run)
                if args.dry_run:
                    print(prompt)
            finally:
                if not args.dry_run:
                    release_lock()
        return 0
    except Exception as exc:
        if not args.dry_run:
            try:
                clear_followup_pending(args.name, error=str(exc))
            except Exception:
                pass
        print(f"ralph tmux follow-up unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
