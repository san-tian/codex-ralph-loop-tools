#!/usr/bin/env python3
"""Pi-inspired Ralph loop MCP server for Codex."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ralph-loop-tools"
SERVER_VERSION = "0.3.0"

COMPLETE_MARKER = "<promise>COMPLETE</promise>"
DEFAULT_REFLECT_INSTRUCTIONS = """REFLECTION CHECKPOINT

Pause and reflect on your progress:
1. What has been accomplished so far?
2. What's working well?
3. What's not working or blocking progress?
4. Should the approach be adjusted?
5. What are the next priorities?

Update the task file with your reflection, then continue working."""
SESSION_OWNER = "codex-plugin"
STATUS_ICONS = {"active": "▶", "paused": "⏸", "completed": "✓"}
CHECKLIST_ITEM_RE = re.compile(r"^\s*[-*]\s+\[(x|X| )\]\s+(.*\S)\s*$")
DISPLAY_PROTOCOL_VERSION = 1
DISPLAY_SOURCE = "ralph-loop"
DISPLAY_TITLE = "Ralph loop"
DISPLAY_ITEM_LIMIT = 8

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]
STATE_ROOT = REPO_ROOT / ".tmp" / "ralph-loop-tools"
LOOPS_ROOT = STATE_ROOT / "loops"
ARCHIVE_ROOT = STATE_ROOT / "archive"
CURRENT_LOOP_PATH = STATE_ROOT / "current.json"
CURRENT_SESSION_ROOT = STATE_ROOT / "current-by-session"
PROMPT_TRIGGER_PATH = STATE_ROOT / "expected-prompt.json"
PROMPT_TRIGGER_SESSION_ROOT = STATE_ROOT / "expected-prompt-by-session"
STDIO_FRAMING = "headers"


@dataclass
class LoopState:
    name: str
    task_file: str
    state_file: str
    iteration: int
    max_iterations: int
    items_per_iteration: int
    reflect_every: int
    reflect_instructions: str
    status: str
    owner_session: str | None
    started_at: str
    completed_at: str | None
    last_reflection_at: int
    pending_continuation: bool
    last_continuation_error: str | None
    last_continuation_error_at: str | None
    updated_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_session_id(session_id: str | None) -> str | None:
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    return None


def normalize_loop_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized


def ensure_state_dirs() -> None:
    LOOPS_ROOT.mkdir(parents=True, exist_ok=True)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    CURRENT_SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    PROMPT_TRIGGER_SESSION_ROOT.mkdir(parents=True, exist_ok=True)


def loop_dir(loop_name: str, archived: bool = False) -> Path:
    root = ARCHIVE_ROOT if archived else LOOPS_ROOT
    return root / loop_name


def loop_task_path(loop_name: str, archived: bool = False) -> Path:
    return loop_dir(loop_name, archived) / "task.md"


def loop_state_path(loop_name: str, archived: bool = False) -> Path:
    return loop_dir(loop_name, archived) / "state.json"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def relative_to_repo(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def parse_int(value: Any, *, default: int, minimum: int = 0) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected integer value, got {value!r}.") from exc
    if parsed < minimum:
        raise ValueError(f"Expected integer >= {minimum}, got {parsed}.")
    return parsed


def migrate_loop_state(loop_name: str, payload: dict[str, Any], archived: bool = False) -> LoopState:
    task_file = payload.get("task_file") or str(loop_task_path(loop_name, archived))
    state_file = payload.get("state_file") or str(loop_state_path(loop_name, archived))
    started_at = payload.get("started_at") or payload.get("created_at") or now_iso()
    status = str(payload.get("status") or "active")
    if status not in STATUS_ICONS:
        status = "active"
    reflect_every = parse_int(payload.get("reflect_every"), default=0, minimum=0)
    owner_session = payload.get("owner_session") or None
    # Legacy shared-owner states used a synthetic owner value that effectively
    # made one active loop visible to unrelated sessions. Treat that old marker
    # as unowned state instead of as a session match.
    if owner_session == SESSION_OWNER:
        owner_session = None
    return LoopState(
        name=str(payload.get("name") or loop_name),
        task_file=str(task_file),
        state_file=str(state_file),
        iteration=parse_int(payload.get("iteration"), default=1, minimum=1),
        max_iterations=parse_int(payload.get("max_iterations"), default=50, minimum=1),
        items_per_iteration=parse_int(payload.get("items_per_iteration"), default=0, minimum=0),
        reflect_every=reflect_every,
        reflect_instructions=str(
            payload.get("reflect_instructions") or DEFAULT_REFLECT_INSTRUCTIONS
        ),
        status=status,
        owner_session=owner_session,
        started_at=str(started_at),
        completed_at=payload.get("completed_at") or None,
        last_reflection_at=parse_int(payload.get("last_reflection_at"), default=0, minimum=0),
        pending_continuation=bool(payload.get("pending_continuation", False)),
        last_continuation_error=payload.get("last_continuation_error") or None,
        last_continuation_error_at=payload.get("last_continuation_error_at") or None,
        updated_at=str(payload.get("updated_at") or now_iso()),
    )


def save_loop_state(state: LoopState) -> None:
    write_json(Path(state.state_file), asdict(state))


def load_loop_state(loop_name: str, archived: bool = False) -> LoopState:
    path = loop_state_path(loop_name, archived)
    if not path.is_file():
        raise ValueError(f'Loop "{loop_name}" does not exist.')
    return migrate_loop_state(loop_name, read_json(path), archived)


def list_loops(archived: bool = False) -> list[LoopState]:
    ensure_state_dirs()
    root = ARCHIVE_ROOT if archived else LOOPS_ROOT
    states: list[LoopState] = []
    for state_path in sorted(root.glob("*/state.json")):
        loop_name = state_path.parent.name
        try:
            states.append(migrate_loop_state(loop_name, read_json(state_path), archived))
        except Exception:
            continue
    return states


def rebind_state_paths(state: LoopState, archived: bool) -> None:
    state.task_file = str(loop_task_path(state.name, archived))
    state.state_file = str(loop_state_path(state.name, archived))


def current_session_id() -> str | None:
    for key in ("CODEX_THREAD_ID", "CODEX_WEB_RESUME_SESSION_ID"):
        value = normalize_session_id(os.environ.get(key))
        if value:
            return value
    return None


def resolve_session_id(session_id: str | None = None) -> str | None:
    explicit = normalize_session_id(session_id)
    if explicit:
        return explicit
    return current_session_id()


def loop_owner_session() -> str | None:
    return current_session_id()


def session_current_loop_path(session_id: str) -> Path:
    safe_session = re.sub(r"[^A-Za-z0-9._-]+", "_", session_id.strip())
    return CURRENT_SESSION_ROOT / f"{safe_session}.json"


def session_prompt_trigger_path(session_id: str) -> Path:
    safe_session = re.sub(r"[^A-Za-z0-9._-]+", "_", session_id.strip())
    return PROMPT_TRIGGER_SESSION_ROOT / f"{safe_session}.json"


def normalize_prompt_text(prompt: str) -> str:
    return prompt.replace("\r\n", "\n").strip()


def prompt_fingerprint(prompt: str) -> str:
    normalized = normalize_prompt_text(prompt)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def read_current_loop_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        payload = read_json(path)
    except Exception:
        path.unlink(missing_ok=True)
        return None
    current = payload.get("name")
    if isinstance(current, str) and current:
        return current
    return None


def clear_current_loop_file(path: Path, loop_name: str | None = None) -> None:
    if not path.is_file():
        return
    if loop_name is None:
        path.unlink(missing_ok=True)
        return
    current = read_current_loop_file(path)
    if current == loop_name:
        path.unlink(missing_ok=True)


def read_prompt_trigger_file(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    try:
        payload = read_json(path)
    except Exception:
        path.unlink(missing_ok=True)
        return None
    loop_name = payload.get("name")
    fingerprint = payload.get("fingerprint")
    if isinstance(loop_name, str) and loop_name and isinstance(fingerprint, str) and fingerprint:
        return {"name": loop_name, "fingerprint": fingerprint}
    path.unlink(missing_ok=True)
    return None


def clear_prompt_trigger_file(path: Path, loop_name: str | None = None) -> None:
    if not path.is_file():
        return
    if loop_name is None:
        path.unlink(missing_ok=True)
        return
    payload = read_prompt_trigger_file(path)
    if payload and payload.get("name") == loop_name:
        path.unlink(missing_ok=True)


def set_expected_prompt(loop_name: str, prompt: str, session_id: str | None = None) -> None:
    ensure_state_dirs()
    owner_session = resolve_session_id(session_id)
    payload = {
        "name": loop_name,
        "fingerprint": prompt_fingerprint(prompt),
        "updated_at": now_iso(),
    }
    if owner_session:
        clear_prompt_trigger_file(PROMPT_TRIGGER_PATH, loop_name)
        write_json(session_prompt_trigger_path(owner_session), payload)
        return
    write_json(PROMPT_TRIGGER_PATH, payload)


def clear_expected_prompt(loop_name: str | None = None, session_id: str | None = None) -> None:
    owner_session = resolve_session_id(session_id)
    if owner_session:
        clear_prompt_trigger_file(session_prompt_trigger_path(owner_session), loop_name)
    elif loop_name is None:
        clear_prompt_trigger_file(PROMPT_TRIGGER_PATH)

    if loop_name is not None and PROMPT_TRIGGER_SESSION_ROOT.exists():
        for prompt_path in PROMPT_TRIGGER_SESSION_ROOT.glob("*.json"):
            clear_prompt_trigger_file(prompt_path, loop_name)
        clear_prompt_trigger_file(PROMPT_TRIGGER_PATH, loop_name)


def consume_expected_prompt(prompt: str, session_id: str | None = None) -> str | None:
    normalized = normalize_prompt_text(prompt)
    if not normalized:
        return None

    owner_session = resolve_session_id(session_id)
    candidate_paths: list[tuple[Path, str | None]] = []
    if owner_session:
        candidate_paths.append((session_prompt_trigger_path(owner_session), owner_session))
    candidate_paths.append((PROMPT_TRIGGER_PATH, None))

    fingerprint = prompt_fingerprint(normalized)
    for path, expected_session in candidate_paths:
        payload = read_prompt_trigger_file(path)
        if not payload:
            continue
        if payload.get("fingerprint") != fingerprint:
            continue
        validated = validate_current_loop_name(payload["name"], expected_session or owner_session)
        path.unlink(missing_ok=True)
        if validated:
            return validated
    return None


def set_current_loop(loop_name: str, session_id: str | None = None) -> None:
    owner_session = resolve_session_id(session_id)
    if owner_session:
        clear_current_loop_file(CURRENT_LOOP_PATH, loop_name)
        write_json(session_current_loop_path(owner_session), {"name": loop_name})
        return
    write_json(CURRENT_LOOP_PATH, {"name": loop_name})


def clear_current_loop(loop_name: str | None = None, session_id: str | None = None) -> None:
    owner_session = resolve_session_id(session_id)
    if owner_session:
        clear_current_loop_file(session_current_loop_path(owner_session), loop_name)
    elif loop_name is None:
        clear_current_loop_file(CURRENT_LOOP_PATH)

    if loop_name is not None and CURRENT_SESSION_ROOT.exists():
        for current_path in CURRENT_SESSION_ROOT.glob("*.json"):
            clear_current_loop_file(current_path, loop_name)
        clear_current_loop_file(CURRENT_LOOP_PATH, loop_name)


def state_owned_by_session(state: LoopState, session_id: str | None) -> bool:
    if not session_id:
        return state.owner_session is None
    return state.owner_session == session_id


def ensure_mutation_access(state: LoopState) -> None:
    owner_session = normalize_session_id(state.owner_session)
    if not owner_session or state.status != "active":
        return
    current = current_session_id()
    if current == owner_session:
        return
    raise ValueError(
        f'Loop "{state.name}" is active in another Codex session ({owner_session}). '
        "Switch back to that session before changing it here."
    )


def state_session_scope(state: LoopState, session_id: str | None = None) -> str | None:
    explicit = normalize_session_id(session_id)
    if explicit:
        return explicit
    if state.owner_session:
        return state.owner_session
    return current_session_id()


def validate_current_loop_name(loop_name: str, session_id: str | None) -> str | None:
    try:
        state = load_loop_state(loop_name)
    except ValueError:
        clear_current_loop(loop_name, session_id=session_id)
        return None
    if state.status != "active":
        clear_current_loop(loop_name, session_id=session_id)
        return None
    if not state_owned_by_session(state, session_id):
        clear_current_loop(loop_name, session_id=session_id)
        return None
    return state.name


def get_current_loop_name(session_id: str | None = None) -> str | None:
    owner_session = resolve_session_id(session_id)
    if owner_session:
        current = read_current_loop_file(session_current_loop_path(owner_session))
        if current:
            validated = validate_current_loop_name(current, owner_session)
            if validated:
                return validated
    else:
        current = read_current_loop_file(CURRENT_LOOP_PATH)
        if current:
            validated = validate_current_loop_name(current, None)
            if validated:
                return validated

    active = [
        state.name
        for state in list_loops()
        if state.status == "active" and state_owned_by_session(state, owner_session)
    ]
    if len(active) == 1:
        return active[0]
    return None


def resolve_loop_name(name: str | None) -> str:
    if name:
        loop_name = normalize_loop_name(name)
        if not loop_name:
            raise ValueError("Loop name must include at least one letter or digit.")
        return loop_name
    current = get_current_loop_name()
    if current:
        return current
    raise ValueError(
        "No active Ralph loop. Start one first, resume one, or pass an explicit name."
    )


def resolve_resumable_loop_name(name: str | None) -> str:
    if name:
        state, archived = resolve_existing_loop(name)
        if archived:
            raise ValueError(f'Loop "{state.name}" is archived. It must be restored before resuming.')
        return state.name
    paused = [state.name for state in list_loops() if state.status == "paused"]
    if len(paused) == 1:
        return paused[0]
    raise ValueError("Pass a loop name to resume.")


def resolve_existing_loop(name: str) -> tuple[LoopState, bool]:
    loop_name = normalize_loop_name(name)
    if not loop_name:
        raise ValueError("Loop name must include at least one letter or digit.")
    state_path = loop_state_path(loop_name)
    if state_path.is_file():
        return load_loop_state(loop_name), False
    archived_state_path = loop_state_path(loop_name, archived=True)
    if archived_state_path.is_file():
        return load_loop_state(loop_name, archived=True), True
    raise ValueError(f'Loop "{loop_name}" does not exist.')


def read_task_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"Task file is missing: {relative_to_repo(path)}")
    return path.read_text(encoding="utf-8").strip()


def checklist_counts(task_text: str) -> tuple[int, int]:
    total = 0
    done = 0
    for line in task_text.splitlines():
        match = re.match(r"^\s*[-*]\s+\[(x|X| )\]\s+", line)
        if match:
            total += 1
            if match.group(1).lower() == "x":
                done += 1
    return total, done


def checklist_items(task_text: str, *, highlight_next: bool) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    highlighted_pending = False
    for line in task_text.splitlines():
        match = CHECKLIST_ITEM_RE.match(line)
        if not match:
            continue
        status = "completed" if match.group(1).lower() == "x" else "pending"
        if highlight_next and status == "pending" and not highlighted_pending:
            status = "in_progress"
            highlighted_pending = True
        items.append({"label": match.group(2).strip(), "status": status})
    if len(items) <= DISPLAY_ITEM_LIMIT:
        return items
    remaining = len(items) - DISPLAY_ITEM_LIMIT
    return items[:DISPLAY_ITEM_LIMIT] + [{"label": f"... and {remaining} more", "status": "pending"}]


def display_status(state: LoopState) -> str:
    if state.status == "active":
        return "running"
    if state.status == "completed":
        return "completed"
    return "pending"


def display_summary(state: LoopState, checklist_done: int, checklist_total: int) -> str:
    summary = f"Iteration {state.iteration}/{state.max_iterations}"
    if checklist_total > 0:
        return f"{summary} - {checklist_done}/{checklist_total} checklist complete"
    return summary


def codoxear_display_payload(state: LoopState) -> dict[str, Any]:
    task_path = Path(state.task_file)
    checklist_total = 0
    checklist_done = 0
    items: list[dict[str, str]] = []
    if task_path.is_file():
        task_text = read_task_text(task_path)
        checklist_total, checklist_done = checklist_counts(task_text)
        items = checklist_items(task_text, highlight_next=state.status == "active")

    payload: dict[str, Any] = {
        "version": DISPLAY_PROTOCOL_VERSION,
        "kind": "progress",
        "source": DISPLAY_SOURCE,
        "title": DISPLAY_TITLE,
        "status": display_status(state),
        "summary": display_summary(state, checklist_done, checklist_total),
        "progress": {
            "current": state.iteration,
            "total": state.max_iterations,
            "label": "iterations",
        },
        "text": f"Loop: {state.name}\nTask: {relative_to_repo(task_path)}",
    }
    if items:
        payload["items"] = items
    return payload


def should_reflect(state: LoopState) -> bool:
    return (
        state.reflect_every > 0
        and state.iteration > 1
        and (state.iteration - 1) % state.reflect_every == 0
    )


def next_reflection_in(state: LoopState) -> int | None:
    if state.reflect_every <= 0:
        return None
    return state.reflect_every - ((state.iteration - 1) % state.reflect_every)


def pause_loop(state: LoopState) -> None:
    state.status = "paused"
    state.owner_session = None
    state.pending_continuation = False
    state.last_continuation_error = None
    state.last_continuation_error_at = None
    state.updated_at = now_iso()
    state.completed_at = None
    save_loop_state(state)
    clear_current_loop(state.name)
    clear_expected_prompt(state.name)


def complete_loop(state: LoopState) -> None:
    state.status = "completed"
    state.owner_session = None
    state.pending_continuation = False
    state.last_continuation_error = None
    state.last_continuation_error_at = None
    state.completed_at = now_iso()
    state.updated_at = state.completed_at
    save_loop_state(state)
    clear_current_loop(state.name)
    clear_expected_prompt(state.name)


def mark_pending_continuation(state: LoopState) -> None:
    state.pending_continuation = True
    state.last_continuation_error = None
    state.last_continuation_error_at = None
    state.updated_at = now_iso()
    save_loop_state(state)


def clear_pending_continuation(state: LoopState, *, error: str | None = None) -> None:
    state.pending_continuation = False
    if error:
        state.last_continuation_error = str(error)
        state.last_continuation_error_at = now_iso()
        state.updated_at = state.last_continuation_error_at
    else:
        state.last_continuation_error = None
        state.last_continuation_error_at = None
        state.updated_at = now_iso()
    save_loop_state(state)


def pause_active_loop_for_switch(next_loop_name: str) -> str | None:
    current = get_current_loop_name()
    if not current or current == next_loop_name:
        return None
    try:
        state = load_loop_state(current)
    except ValueError:
        clear_current_loop(current)
        return None
    if state.status == "active":
        pause_loop(state)
        return state.name
    clear_current_loop(current)
    return None


def format_progress(state: LoopState) -> str:
    return f"{state.iteration}/{state.max_iterations}"


def render_iteration_prompt(state: LoopState) -> str:
    task_path = Path(state.task_file)
    task_text = read_task_text(task_path)
    reflection_block = []
    if should_reflect(state):
        reflection_block = [state.reflect_instructions.strip(), "\n---"]

    header = (
        "───────────────────────────────────────────────────────────────────────\n"
        f"🔄 RALPH LOOP: {state.name} | Iteration {format_progress(state)}"
        f"{' | 🪞 REFLECTION' if should_reflect(state) else ''}\n"
        "───────────────────────────────────────────────────────────────────────"
    )

    instructions = [
        "## Instructions",
        "",
        "Natural-language controls:",
        "- 继续当前 loop: “继续这个 Ralph loop”",
        "- 暂停当前 loop: “暂停这个 Ralph loop”",
        "- 查看状态: “查看 Ralph 状态”",
        "",
        f"You are in a Ralph loop (iteration {format_progress(state)}).",
    ]
    if state.items_per_iteration > 0:
        instructions.append(
            f"THIS ITERATION: process about {state.items_per_iteration} checklist items,"
            " then call ralph_done."
        )
        instructions.append(
            f"1. Work on the next ~{state.items_per_iteration} checklist items."
        )
    else:
        instructions.append("1. Continue working on the task.")
    instructions.extend(
        [
            f"2. Update the task file ({relative_to_repo(task_path)}) with progress and verification evidence.",
            f"3. When FULLY COMPLETE, either finish the checklist or add {COMPLETE_MARKER} to the task file.",
            "4. Otherwise, call ralph_done to proceed to the next iteration.",
            "",
            "When running inside tmux, Ralph hooks can send /compact and paste the"
            " next prompt back into the Codex pane for Pi-like continuation.",
        ]
    )

    sections = [
        header,
        "",
        *reflection_block,
        f"## Current Task (from {relative_to_repo(task_path)})",
        "",
        task_text,
        "",
        "---",
        "",
        *instructions,
    ]
    return "\n".join(section for section in sections if section is not None).strip()


def render_complete_prompt(state: LoopState, reason: str, *, stopped: bool = False) -> str:
    banner = "⚠️ RALPH LOOP STOPPED" if stopped else "✅ RALPH LOOP COMPLETE"
    task_path = Path(state.task_file)
    return "\n".join(
        [
            "───────────────────────────────────────────────────────────────────────",
            f"{banner}: {state.name} | {state.iteration} iterations",
            "───────────────────────────────────────────────────────────────────────",
            "",
            f"Reason: {reason}",
            f"Task file: {relative_to_repo(task_path)}",
        ]
    ).strip()


def render_status(state: LoopState) -> str:
    next_reflection = next_reflection_in(state)
    lines = [
        f"Loop: {state.name}",
        f"Status: {STATUS_ICONS[state.status]} {state.status}",
        f"Iteration: {format_progress(state)}",
        f"Task: {relative_to_repo(Path(state.task_file))}",
        f"Started: {state.started_at}",
        f"Updated: {state.updated_at}",
    ]
    if state.owner_session:
        lines.append(f"Owner session: {state.owner_session}")
    if state.completed_at:
        lines.append(f"Completed: {state.completed_at}")
    if state.items_per_iteration > 0:
        lines.append(f"Items per iteration: {state.items_per_iteration}")
    if state.reflect_every > 0:
        lines.append(f"Reflect every: {state.reflect_every}")
        if next_reflection is not None:
            lines.append(f"Next reflection in: {next_reflection} iteration(s)")
    if state.last_reflection_at > 0:
        lines.append(f"Last reflection at iteration: {state.last_reflection_at}")
    if state.pending_continuation:
        lines.append("Pending continuation: yes")
    if state.last_continuation_error:
        lines.append(f"Last follow-up error: {state.last_continuation_error}")
    if state.last_continuation_error_at:
        lines.append(f"Last follow-up error at: {state.last_continuation_error_at}")
    return "\n".join(lines)


def render_status_list(states: list[LoopState]) -> str:
    return render_status_list_label(states, label="Ralph loops:")


def render_status_list_label(states: list[LoopState], label: str) -> str:
    if not states:
        return f"{label}\n- none"
    current = get_current_loop_name()
    ordered = sorted(
        states,
        key=lambda state: (
            0 if state.status == "active" else 1 if state.status == "paused" else 2,
            state.name,
        ),
    )
    lines = [label]
    for state in ordered:
        current_marker = " | current" if state.name == current else ""
        pending = " | compacting" if state.pending_continuation else ""
        error = " | follow-up failed" if state.last_continuation_error else ""
        lines.append(
            f"- {state.name}: {STATUS_ICONS[state.status]} {state.status}"
            f"{pending}{error}{current_marker} (iteration {format_progress(state)})"
        )
    return "\n".join(lines)


def build_tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": "ralph_start",
            "description": "Start or restart a Pi-style Ralph loop and return iteration 1.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "taskContent"],
                "properties": {
                    "name": {"type": "string", "description": "Loop name."},
                    "taskContent": {
                        "type": "string",
                        "description": "Full Markdown task file body.",
                    },
                    "maxIterations": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Max iterations. Default: 50.",
                    },
                    "itemsPerIteration": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Suggested checklist items per iteration. Default: 0.",
                    },
                    "reflectEvery": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Reflection cadence in iterations.",
                    },
                    "reflectInstructions": {
                        "type": "string",
                        "description": "Custom reflection instructions.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Replace an existing active loop with the same name.",
                    },
                },
            },
        },
        {
            "name": "ralph_done",
            "description": "Advance the current Ralph loop and return the next prompt.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Loop name. Defaults to the current active loop.",
                    }
                },
            },
        },
        {
            "name": "ralph_status",
            "description": "Show Ralph loop status for one loop or all loops.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Loop name. Omit to list all loops.",
                    }
                },
            },
        },
        {
            "name": "ralph_resume",
            "description": "Resume a paused Ralph loop and return its next prompt.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Loop name. If omitted, resumes the only paused loop.",
                    }
                },
            },
        },
        {
            "name": "ralph_stop",
            "description": "Pause the active Ralph loop.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Loop name. Defaults to the current active loop.",
                    }
                },
            },
        },
        {
            "name": "ralph_list",
            "description": "List active/paused/completed loops or archived loops.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "archived": {
                        "type": "boolean",
                        "description": "List archived loops instead of active storage.",
                    }
                },
            },
        },
        {
            "name": "ralph_cancel",
            "description": "Delete one Ralph loop entirely.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "Loop name."}
                },
            },
        },
        {
            "name": "ralph_archive",
            "description": "Move a paused or completed Ralph loop into archive storage.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "Loop name."}
                },
            },
        },
    ]


def start_loop(arguments: dict[str, Any]) -> str:
    ensure_state_dirs()
    raw_name = str(arguments.get("name", ""))
    loop_name = normalize_loop_name(raw_name)
    if not loop_name:
        raise ValueError("Loop name must include at least one letter or digit.")

    task_content = arguments.get("taskContent")
    if not isinstance(task_content, str) or not task_content.strip():
        raise ValueError("taskContent must be a non-empty string.")

    max_iterations = parse_int(arguments.get("maxIterations"), default=50, minimum=1)
    items_per_iteration = parse_int(
        arguments.get("itemsPerIteration"), default=0, minimum=0
    )
    reflect_every = parse_int(arguments.get("reflectEvery"), default=0, minimum=0)
    reflect_instructions = str(
        arguments.get("reflectInstructions") or DEFAULT_REFLECT_INSTRUCTIONS
    )
    force = bool(arguments.get("force", False))

    existing: LoopState | None = None
    state_path = loop_state_path(loop_name)
    if state_path.exists():
        existing = load_loop_state(loop_name)
        ensure_mutation_access(existing)
        if existing.status == "active" and not force:
            raise ValueError(
                f'Loop "{loop_name}" is already active. Stop it first or pass force=true.'
            )

    paused_name = pause_active_loop_for_switch(loop_name)
    timestamp = now_iso()
    task_path = loop_task_path(loop_name)
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(task_content.strip() + "\n", encoding="utf-8")

    state = LoopState(
        name=loop_name,
        task_file=str(task_path),
        state_file=str(state_path),
        iteration=1,
        max_iterations=max_iterations,
        items_per_iteration=items_per_iteration,
        reflect_every=reflect_every,
        reflect_instructions=reflect_instructions,
        status="active",
        owner_session=loop_owner_session(),
        started_at=existing.started_at if existing else timestamp,
        completed_at=None,
        last_reflection_at=0,
        pending_continuation=False,
        last_continuation_error=None,
        last_continuation_error_at=None,
        updated_at=timestamp,
    )
    save_loop_state(state)
    session_scope = state_session_scope(state)
    set_current_loop(loop_name, session_id=session_scope)

    notes: list[str] = []
    if raw_name != loop_name:
        notes.append(f'Normalized loop name from "{raw_name}" to "{loop_name}".')
    if paused_name:
        notes.append(f'Paused active loop "{paused_name}" before starting "{loop_name}".')

    prompt = render_iteration_prompt(state)
    set_expected_prompt(loop_name, prompt, session_id=session_scope)
    if notes:
        return "\n".join(notes + ["", prompt])
    return prompt


def advance_loop(
    arguments: dict[str, Any],
    *,
    allow_pending: bool = False,
    record_prompt_trigger: bool = True,
) -> str:
    ensure_state_dirs()
    loop_name = resolve_loop_name(arguments.get("name"))
    state = load_loop_state(loop_name)
    ensure_mutation_access(state)

    if state.status != "active":
        raise ValueError(f'Loop "{loop_name}" is not active (status={state.status}).')
    if state.pending_continuation and not allow_pending:
        raise ValueError(
            f'Loop "{loop_name}" is already preparing the next iteration.'
        )

    task_text = read_task_text(Path(state.task_file))
    checklist_total, checklist_done = checklist_counts(task_text)
    if COMPLETE_MARKER in task_text:
        complete_loop(state)
        return render_complete_prompt(state, "Completion marker found in task file.")
    if checklist_total > 0 and checklist_total == checklist_done:
        complete_loop(state)
        return render_complete_prompt(state, "Checklist is fully complete.")

    state.iteration += 1
    if state.max_iterations > 0 and state.iteration > state.max_iterations:
        complete_loop(state)
        return render_complete_prompt(
            state,
            f"Max iterations ({state.max_iterations}) reached before completion.",
            stopped=True,
        )

    if should_reflect(state):
        state.last_reflection_at = state.iteration
    state.pending_continuation = False
    state.last_continuation_error = None
    state.last_continuation_error_at = None
    state.updated_at = now_iso()
    save_loop_state(state)
    session_scope = state_session_scope(state)
    set_current_loop(loop_name, session_id=session_scope)
    prompt = render_iteration_prompt(state)
    if record_prompt_trigger:
        set_expected_prompt(loop_name, prompt, session_id=session_scope)
    return prompt


def status_loop(arguments: dict[str, Any]) -> str:
    ensure_state_dirs()
    loop_name = arguments.get("name")
    if loop_name:
        state, _archived = resolve_existing_loop(str(loop_name))
        return render_status(state)
    return render_status_list(list_loops())


def resume_loop(arguments: dict[str, Any]) -> str:
    ensure_state_dirs()
    loop_name = resolve_resumable_loop_name(arguments.get("name"))
    state = load_loop_state(loop_name)
    if state.status == "completed":
        raise ValueError(
            f'Loop "{loop_name}" is completed. Start it again instead of resuming.'
        )

    paused_name = pause_active_loop_for_switch(loop_name)
    state.status = "active"
    state.owner_session = loop_owner_session()
    state.pending_continuation = False
    state.last_continuation_error = None
    state.last_continuation_error_at = None
    state.completed_at = None
    state.updated_at = now_iso()
    save_loop_state(state)
    session_scope = state_session_scope(state)
    set_current_loop(loop_name, session_id=session_scope)

    prompt = render_iteration_prompt(state)
    set_expected_prompt(loop_name, prompt, session_id=session_scope)
    if paused_name:
        return (
            f'Paused active loop "{paused_name}" before resuming "{loop_name}".\n\n'
            f"{prompt}"
        )
    return prompt


def stop_loop(arguments: dict[str, Any]) -> str:
    ensure_state_dirs()
    loop_name = resolve_loop_name(arguments.get("name"))
    state = load_loop_state(loop_name)
    ensure_mutation_access(state)
    if state.status != "active":
        raise ValueError(f'Loop "{loop_name}" is not active (status={state.status}).')
    pause_loop(state)
    return (
        f'Paused Ralph loop "{loop_name}" at iteration {format_progress(state)}.\n\n'
        f"{render_status(state)}"
    )


def list_loop_states(arguments: dict[str, Any]) -> str:
    ensure_state_dirs()
    archived = bool(arguments.get("archived", False))
    states = list_loops(archived=archived)
    if archived:
        return render_status_list_label(states, label="Archived Ralph loops:")
    return render_status_list(states)


def cancel_loop(arguments: dict[str, Any]) -> str:
    ensure_state_dirs()
    raw_name = arguments.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("name is required.")
    state, archived = resolve_existing_loop(raw_name)
    ensure_mutation_access(state)
    target_dir = loop_dir(state.name, archived)
    clear_current_loop(state.name)
    shutil.rmtree(target_dir, ignore_errors=False)
    scope = "archived " if archived else ""
    return f'Cancelled {scope}Ralph loop "{state.name}".'


def archive_loop(arguments: dict[str, Any]) -> str:
    ensure_state_dirs()
    raw_name = arguments.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("name is required.")
    loop_name = normalize_loop_name(raw_name)
    state = load_loop_state(loop_name)
    if state.status == "active":
        raise ValueError(f'Loop "{loop_name}" is active. Pause it before archiving.')
    src_dir = loop_dir(loop_name)
    dst_dir = loop_dir(loop_name, archived=True)
    if dst_dir.exists():
        raise ValueError(f'Archived loop "{loop_name}" already exists.')
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_dir), str(dst_dir))
    archived_state = load_loop_state(loop_name, archived=True)
    rebind_state_paths(archived_state, archived=True)
    archived_state.updated_at = now_iso()
    save_loop_state(archived_state)
    clear_current_loop(loop_name)
    return f'Archived Ralph loop "{loop_name}".'


def tool_result_text(
    text: str,
    is_error: bool = False,
    *,
    display_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if display_payload is not None:
        payload["structuredContent"] = {"codoxear_display": display_payload}
    if is_error:
        payload["isError"] = True
    return payload


def resolve_display_state(arguments: dict[str, Any]) -> LoopState | None:
    raw_name = arguments.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        try:
            state, _archived = resolve_existing_loop(raw_name)
            return state
        except ValueError:
            return None
    current = get_current_loop_name()
    if not current:
        return None
    try:
        state, _archived = resolve_existing_loop(current)
        return state
    except ValueError:
        return None


def handle_call(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    if method == "initialize":
        protocol_version = PROTOCOL_VERSION
        if isinstance(params, dict):
            requested_protocol_version = params.get("protocolVersion")
            if isinstance(requested_protocol_version, str) and requested_protocol_version:
                protocol_version = requested_protocol_version
        return {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    if method == "notifications/initialized":
        return {}

    if method == "ping":
        return {}

    if method == "tools/list":
        return {"tools": build_tool_list()}

    if method == "tools/call":
        params = params or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("tools/call arguments must be an object.")
        if tool_name == "ralph_start":
            text = start_loop(arguments)
            state = resolve_display_state(arguments)
            return tool_result_text(text, display_payload=codoxear_display_payload(state) if state else None)
        if tool_name == "ralph_done":
            text = advance_loop(arguments)
            state = resolve_display_state(arguments)
            return tool_result_text(text, display_payload=codoxear_display_payload(state) if state else None)
        if tool_name == "ralph_status":
            text = status_loop(arguments)
            state = resolve_display_state(arguments)
            return tool_result_text(text, display_payload=codoxear_display_payload(state) if state else None)
        if tool_name == "ralph_resume":
            text = resume_loop(arguments)
            state = resolve_display_state(arguments)
            return tool_result_text(text, display_payload=codoxear_display_payload(state) if state else None)
        if tool_name == "ralph_stop":
            text = stop_loop(arguments)
            state = resolve_display_state(arguments)
            return tool_result_text(text, display_payload=codoxear_display_payload(state) if state else None)
        if tool_name == "ralph_list":
            text = list_loop_states(arguments)
            state = resolve_display_state(arguments)
            return tool_result_text(text, display_payload=codoxear_display_payload(state) if state else None)
        if tool_name == "ralph_cancel":
            return tool_result_text(cancel_loop(arguments))
        if tool_name == "ralph_archive":
            return tool_result_text(archive_loop(arguments))
        return tool_result_text(f"Unknown tool: {tool_name}", is_error=True)

    raise ValueError(f"Unsupported method: {method}")


def read_message() -> dict[str, Any] | None:
    global STDIO_FRAMING

    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.strip()
        if not headers and stripped.startswith(b"{"):
            STDIO_FRAMING = "jsonl"
            return json.loads(stripped.decode("utf-8"))
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("utf-8").partition(":")
        headers[key.strip().lower()] = value.strip()

    content_length = headers.get("content-length")
    if not content_length:
        raise RuntimeError("Missing Content-Length header.")
    body = sys.stdin.buffer.read(int(content_length))
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if STDIO_FRAMING == "jsonl":
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()
        return
    sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            return

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")

        if request_id is None:
            try:
                handle_call(str(method), params if isinstance(params, dict) else None)
            except Exception:
                pass
            continue

        try:
            result = handle_call(str(method), params if isinstance(params, dict) else None)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # pragma: no cover - exercised via manual handshake.
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }
        write_message(response)


if __name__ == "__main__":
    main()
