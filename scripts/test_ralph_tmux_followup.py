#!/usr/bin/env python3
"""Logic-level regression tests for the Ralph tmux follow-up worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ralph_loop_mcp_server as ralph
import ralph_hook
import ralph_tmux_followup as followup


@contextmanager
def temporary_ralph_repo(repo_root: Path):
    original = {
        "REPO_ROOT": ralph.REPO_ROOT,
        "STATE_ROOT": ralph.STATE_ROOT,
        "LOOPS_ROOT": ralph.LOOPS_ROOT,
        "ARCHIVE_ROOT": ralph.ARCHIVE_ROOT,
        "CURRENT_LOOP_PATH": ralph.CURRENT_LOOP_PATH,
        "CURRENT_SESSION_ROOT": ralph.CURRENT_SESSION_ROOT,
        "PROMPT_TRIGGER_PATH": ralph.PROMPT_TRIGGER_PATH,
        "PROMPT_TRIGGER_SESSION_ROOT": ralph.PROMPT_TRIGGER_SESSION_ROOT,
        "LOCK_PATH": followup.LOCK_PATH,
    }
    try:
        ralph.REPO_ROOT = repo_root
        ralph.STATE_ROOT = repo_root / ".tmp" / "ralph-loop-tools"
        ralph.LOOPS_ROOT = ralph.STATE_ROOT / "loops"
        ralph.ARCHIVE_ROOT = ralph.STATE_ROOT / "archive"
        ralph.CURRENT_LOOP_PATH = ralph.STATE_ROOT / "current.json"
        ralph.CURRENT_SESSION_ROOT = ralph.STATE_ROOT / "current-by-session"
        ralph.PROMPT_TRIGGER_PATH = ralph.STATE_ROOT / "expected-prompt.json"
        ralph.PROMPT_TRIGGER_SESSION_ROOT = ralph.STATE_ROOT / "expected-prompt-by-session"
        followup.LOCK_PATH = ralph.STATE_ROOT / "tmux-followup.lock"
        yield
    finally:
        ralph.REPO_ROOT = original["REPO_ROOT"]
        ralph.STATE_ROOT = original["STATE_ROOT"]
        ralph.LOOPS_ROOT = original["LOOPS_ROOT"]
        ralph.ARCHIVE_ROOT = original["ARCHIVE_ROOT"]
        ralph.CURRENT_LOOP_PATH = original["CURRENT_LOOP_PATH"]
        ralph.CURRENT_SESSION_ROOT = original["CURRENT_SESSION_ROOT"]
        ralph.PROMPT_TRIGGER_PATH = original["PROMPT_TRIGGER_PATH"]
        ralph.PROMPT_TRIGGER_SESSION_ROOT = original["PROMPT_TRIGGER_SESSION_ROOT"]
        followup.LOCK_PATH = original["LOCK_PATH"]


class RalphTmuxFollowupTests(unittest.TestCase):
    def make_state(self, name: str, *, iteration: int = 1, status: str = "active", owner_session: str | None = None) -> ralph.LoopState:
        task_path = ralph.loop_task_path(name)
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(
            f"# {name}\n\n## Checklist\n- [ ] First item\n",
            encoding="utf-8",
        )
        state = ralph.LoopState(
            name=name,
            task_file=str(task_path),
            state_file=str(ralph.loop_state_path(name)),
            iteration=iteration,
            max_iterations=5,
            items_per_iteration=1,
            reflect_every=3,
            reflect_instructions=ralph.DEFAULT_REFLECT_INSTRUCTIONS,
            status=status,
            owner_session=owner_session,
            started_at="2026-04-26T00:00:00+00:00",
            completed_at=None,
            last_reflection_at=0,
            pending_continuation=False,
            last_continuation_error=None,
            last_continuation_error_at=None,
            updated_at="2026-04-26T00:00:00+00:00",
        )
        ralph.save_loop_state(state)
        return state

    def write_jsonl(self, records: list[dict]) -> Path:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        path = Path(handle.name)
        try:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        finally:
            handle.close()
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def capture_hook_payload(self, func, *args) -> dict:
        payloads: list[dict] = []

        def fake_emit(payload: dict[str, object]) -> None:
            payloads.append(payload)

        with mock.patch.object(ralph_hook, "emit", side_effect=fake_emit):
            func(*args)
        self.assertEqual(len(payloads), 1)
        return payloads[0]

    def test_rollout_has_terminal_turn_detects_completed_turn(self) -> None:
        path = self.write_jsonl(
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-1"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-2"},
                },
            ]
        )

        self.assertTrue(followup.rollout_has_terminal_turn(path, "turn-1"))
        self.assertFalse(followup.rollout_has_terminal_turn(path, "turn-2"))
        self.assertIsNone(followup.rollout_has_terminal_turn(path, None))

    def test_choose_best_rollout_log_prefers_log_containing_trigger_turn(self) -> None:
        unrelated = self.write_jsonl(
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-old"},
                }
            ]
        )
        matching = self.write_jsonl(
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-target"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-target"},
                },
            ]
        )

        chosen = followup.choose_best_rollout_log(
            [unrelated, matching],
            trigger_turn_id="turn-target",
            session_id="thread-a",
        )

        self.assertEqual(chosen, matching)

    def test_choose_best_rollout_log_prefers_session_id_when_turn_unknown(self) -> None:
        unrelated = Path(self.write_jsonl([]).with_name("rollout-2026-04-26T01-00-00-thread-old.jsonl"))
        unrelated.write_text("", encoding="utf-8")
        self.addCleanup(unrelated.unlink, missing_ok=True)
        matching = Path(self.write_jsonl([]).with_name("rollout-2026-04-26T01-00-00-thread-a.jsonl"))
        matching.write_text("", encoding="utf-8")
        self.addCleanup(matching.unlink, missing_ok=True)

        chosen = followup.choose_best_rollout_log(
            [unrelated, matching],
            trigger_turn_id="turn-target",
            session_id="thread-a",
        )

        self.assertEqual(chosen, matching)

    def test_compute_idle_from_rollout_stays_busy_after_new_turn_activity(self) -> None:
        path = self.write_jsonl(
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-1"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-2"},
                },
                {
                    "type": "response_item",
                    "payload": {"type": "reasoning"},
                },
            ]
        )

        self.assertFalse(followup.compute_idle_from_rollout(path))

    def test_current_loop_resolution_is_isolated_per_codex_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                self.make_state("loop-a", owner_session="thread-a")
                self.make_state("loop-b", owner_session="thread-b")
                ralph.set_current_loop("loop-a", session_id="thread-a")
                ralph.set_current_loop("loop-b", session_id="thread-b")

                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    self.assertEqual(ralph.get_current_loop_name(), "loop-a")

                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-b"}, clear=False):
                    self.assertEqual(ralph.get_current_loop_name(), "loop-b")

    def test_legacy_shared_owner_loop_is_not_auto_adopted_by_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                state = self.make_state("legacy-loop", owner_session=ralph.SESSION_OWNER)
                ralph.save_loop_state(state)

                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    self.assertIsNone(ralph.get_current_loop_name())

    def test_stale_session_pointer_to_foreign_loop_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                self.make_state("loop-a", owner_session="thread-a")
                ralph.set_current_loop("loop-a", session_id="thread-b")

                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-b"}, clear=False):
                    self.assertIsNone(ralph.get_current_loop_name())
                    self.assertFalse(ralph.session_current_loop_path("thread-b").exists())

    def test_missing_session_id_does_not_adopt_foreign_owned_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                self.make_state("loop-a", owner_session="thread-a")
                ralph.set_current_loop("loop-a", session_id="thread-a")

                with mock.patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "", "CODEX_WEB_RESUME_SESSION_ID": ""},
                    clear=False,
                ):
                    self.assertIsNone(ralph.get_current_loop_name())

    def test_session_start_does_not_inject_active_loop_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                state = self.make_state("loop-a", owner_session="thread-a")
                state.pending_continuation = True
                state.last_continuation_error = "timed out waiting for Codex idle state"
                ralph.save_loop_state(state)
                ralph.set_current_loop("loop-a", session_id="thread-a")

                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    payload = self.capture_hook_payload(ralph_hook.handle_session_start)

                self.assertEqual(payload, {"continue": True})

    def test_session_start_ignores_legacy_shared_owner_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                state = self.make_state("legacy-loop", owner_session=ralph.SESSION_OWNER)
                ralph.save_loop_state(state)

                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    payload = self.capture_hook_payload(ralph_hook.handle_session_start)

                self.assertEqual(payload, {"continue": True})

    def test_user_prompt_submit_context_requires_expected_ralph_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                with mock.patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "thread-a", "TMUX": "/tmp/tmux-test"},
                    clear=False,
                ):
                    prompt = ralph.start_loop(
                        {"name": "loop-a", "taskContent": "# Loop A\n\n## Checklist\n- [ ] one\n"}
                    )
                    payload = self.capture_hook_payload(
                        ralph_hook.handle_user_prompt_submit,
                        {"prompt": prompt},
                    )

        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Ralph control rule: updating the task file alone does not hand off the iteration.", context)
        self.assertIn("Active loop: loop-a", context)

    def test_user_prompt_submit_prefers_hook_event_session_id_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    prompt = ralph.start_loop(
                        {"name": "loop-a", "taskContent": "# Loop A\n\n## Checklist\n- [ ] one\n"}
                    )

                with mock.patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "thread-b", "TMUX": "/tmp/tmux-test"},
                    clear=False,
                ):
                    payload = self.capture_hook_payload(
                        ralph_hook.handle_user_prompt_submit,
                        {"session_id": "thread-a", "prompt": prompt},
                    )

        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Active loop: loop-a", context)

    def test_user_prompt_submit_rejects_foreign_hook_event_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    prompt = ralph.start_loop(
                        {"name": "loop-a", "taskContent": "# Loop A\n\n## Checklist\n- [ ] one\n"}
                    )

                with mock.patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "thread-a", "TMUX": "/tmp/tmux-test"},
                    clear=False,
                ):
                    payload = self.capture_hook_payload(
                        ralph_hook.handle_user_prompt_submit,
                        {"session_id": "thread-b", "prompt": prompt},
                    )

        self.assertEqual(payload, {"continue": True})

    def test_user_prompt_submit_ignores_unrelated_prompt_even_with_active_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                state = self.make_state("loop-a", owner_session="thread-a")
                ralph.save_loop_state(state)
                ralph.set_current_loop("loop-a", session_id="thread-a")

                with mock.patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "thread-a", "TMUX": "/tmp/tmux-test"},
                    clear=False,
                ):
                    payload = self.capture_hook_payload(
                        ralph_hook.handle_user_prompt_submit,
                        {"prompt": "在 mimo 教程里注明，mimo token 由 Hera 捐赠"},
                    )

        self.assertEqual(payload, {"continue": True})

    def test_user_prompt_submit_ignores_ralph_intent_without_expected_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                state = self.make_state("loop-a", owner_session="thread-a")
                ralph.save_loop_state(state)
                ralph.set_current_loop("loop-a", session_id="thread-a")

                with mock.patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "thread-a", "TMUX": "/tmp/tmux-test"},
                    clear=False,
                ):
                    payload = self.capture_hook_payload(
                        ralph_hook.handle_user_prompt_submit,
                        {"prompt": "继续这个 ralph loop"},
                    )

        self.assertEqual(payload, {"continue": True})

    def test_consume_expected_prompt_matches_once_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()

                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    prompt = ralph.start_loop(
                        {"name": "loop-a", "taskContent": "# Loop A\n\n## Checklist\n- [ ] one\n"}
                    )
                    self.assertEqual(ralph.consume_expected_prompt(prompt), "loop-a")
                    self.assertIsNone(ralph.consume_expected_prompt(prompt))

    def test_deliver_next_prompt_arms_expected_prompt_before_paste(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                task_path = ralph.loop_task_path("demo-loop")
                task_path.parent.mkdir(parents=True, exist_ok=True)
                task_path.write_text(
                    "# Demo Loop\n\n## Checklist\n- [ ] First item\n",
                    encoding="utf-8",
                )

                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    state = ralph.LoopState(
                        name="demo-loop",
                        task_file=str(task_path),
                        state_file=str(ralph.loop_state_path("demo-loop")),
                        iteration=1,
                        max_iterations=5,
                        items_per_iteration=1,
                        reflect_every=3,
                        reflect_instructions=ralph.DEFAULT_REFLECT_INSTRUCTIONS,
                        status="active",
                        owner_session="thread-a",
                        started_at="2026-04-26T00:00:00+00:00",
                        completed_at=None,
                        last_reflection_at=0,
                        pending_continuation=True,
                        last_continuation_error=None,
                        last_continuation_error_at=None,
                        updated_at="2026-04-26T00:00:00+00:00",
                    )
                    ralph.save_loop_state(state)
                    ralph.set_current_loop(state.name, session_id="thread-a")

                    seen: list[str | None] = []

                    def fake_paste(_pane_id: str, prompt: str) -> None:
                        seen.append(ralph.consume_expected_prompt(prompt))

                    with mock.patch.object(followup, "paste_prompt_into_pane", side_effect=fake_paste):
                        prompt = followup.deliver_next_prompt(state.name, "%12", dry_run=False)

                reloaded = ralph.load_loop_state(state.name)
                self.assertEqual(seen, ["demo-loop"])
                self.assertEqual(reloaded.iteration, 2)
                self.assertIn("Iteration 2/5", prompt)

    def test_start_loop_only_pauses_current_session_active_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}, clear=False):
                    ralph.start_loop({"name": "loop-a", "taskContent": "# A\n\n## Checklist\n- [ ] one\n"})
                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-b"}, clear=False):
                    ralph.start_loop({"name": "loop-b", "taskContent": "# B\n\n## Checklist\n- [ ] one\n"})
                    ralph.start_loop({"name": "loop-c", "taskContent": "# C\n\n## Checklist\n- [ ] one\n"})

                state_a = ralph.load_loop_state("loop-a")
                state_b = ralph.load_loop_state("loop-b")
                state_c = ralph.load_loop_state("loop-c")

                self.assertEqual(state_a.status, "active")
                self.assertEqual(state_a.owner_session, "thread-a")
                self.assertEqual(state_b.status, "paused")
                self.assertIsNone(state_b.owner_session)
                self.assertEqual(state_c.status, "active")
                self.assertEqual(state_c.owner_session, "thread-b")

    def test_advance_loop_keeps_owner_session_scope_even_with_foreign_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                state = self.make_state("loop-a", owner_session="thread-a")
                ralph.set_current_loop("loop-a", session_id="thread-a")

                with mock.patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "thread-b", "CODEX_WEB_RESUME_SESSION_ID": ""},
                    clear=False,
                ):
                    prompt = ralph.advance_loop({"name": "loop-a"})

                self.assertIn("Iteration 2/5", prompt)
                self.assertTrue(ralph.session_current_loop_path("thread-a").exists())
                self.assertFalse(ralph.session_current_loop_path("thread-b").exists())
                self.assertFalse(ralph.CURRENT_LOOP_PATH.exists())
                self.assertTrue(ralph.session_prompt_trigger_path("thread-a").exists())
                self.assertFalse(ralph.session_prompt_trigger_path("thread-b").exists())
                self.assertFalse(ralph.PROMPT_TRIGGER_PATH.exists())
                reloaded = ralph.load_loop_state(state.name)
                self.assertEqual(reloaded.iteration, 2)

    def test_preview_next_prompt_does_not_advance_loop_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                task_path = ralph.loop_task_path("demo-loop")
                task_path.parent.mkdir(parents=True, exist_ok=True)
                task_path.write_text(
                    "# Demo Loop\n\n## Checklist\n- [ ] First item\n",
                    encoding="utf-8",
                )
                state = ralph.LoopState(
                    name="demo-loop",
                    task_file=str(task_path),
                    state_file=str(ralph.loop_state_path("demo-loop")),
                    iteration=1,
                    max_iterations=5,
                    items_per_iteration=1,
                    reflect_every=3,
                    reflect_instructions=ralph.DEFAULT_REFLECT_INSTRUCTIONS,
                    status="active",
                    owner_session=ralph.SESSION_OWNER,
                    started_at="2026-04-26T00:00:00+00:00",
                    completed_at=None,
                    last_reflection_at=0,
                    pending_continuation=False,
                    last_continuation_error=None,
                    last_continuation_error_at=None,
                    updated_at="2026-04-26T00:00:00+00:00",
                )
                ralph.save_loop_state(state)
                ralph.set_current_loop(state.name)

                prompt = followup.preview_next_prompt(state.name)
                reloaded = ralph.load_loop_state(state.name)

                self.assertEqual(reloaded.iteration, 1)
                self.assertIn("Iteration 2/5", prompt)

    def test_tool_result_text_carries_codoxear_display_payload(self) -> None:
        payload = ralph.tool_result_text(
            "Loop status",
            display_payload={
                "version": 1,
                "kind": "progress",
                "source": "ralph-loop",
                "title": "Ralph loop",
                "status": "running",
            },
        )

        self.assertEqual(payload["content"][0]["text"], "Loop status")
        self.assertEqual(
            payload["structuredContent"]["codoxear_display"]["source"],
            "ralph-loop",
        )

    def test_status_tool_result_emits_structured_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                task_path = ralph.loop_task_path("demo-loop")
                task_path.parent.mkdir(parents=True, exist_ok=True)
                task_path.write_text(
                    "# Demo Loop\n\n## Checklist\n- [x] Done item\n- [ ] Next item\n",
                    encoding="utf-8",
                )
                state = ralph.LoopState(
                    name="demo-loop",
                    task_file=str(task_path),
                    state_file=str(ralph.loop_state_path("demo-loop")),
                    iteration=2,
                    max_iterations=5,
                    items_per_iteration=1,
                    reflect_every=3,
                    reflect_instructions=ralph.DEFAULT_REFLECT_INSTRUCTIONS,
                    status="active",
                    owner_session=ralph.SESSION_OWNER,
                    started_at="2026-04-26T00:00:00+00:00",
                    completed_at=None,
                    last_reflection_at=0,
                    pending_continuation=False,
                    last_continuation_error=None,
                    last_continuation_error_at=None,
                    updated_at="2026-04-26T00:00:00+00:00",
                )
                ralph.save_loop_state(state)
                ralph.set_current_loop(state.name)

                result = ralph.handle_call(
                    "tools/call",
                    {"name": "ralph_status", "arguments": {"name": state.name}},
                )

                display = result["structuredContent"]["codoxear_display"]
                self.assertEqual(display["kind"], "progress")
                self.assertEqual(display["source"], "ralph-loop")
                self.assertEqual(display["progress"]["current"], 2)
                self.assertEqual(display["progress"]["total"], 5)
                self.assertEqual(display["items"][0]["status"], "completed")
                self.assertEqual(display["items"][1]["status"], "in_progress")

    def test_next_prompt_allows_worker_to_advance_pending_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                task_path = ralph.loop_task_path("demo-loop")
                task_path.parent.mkdir(parents=True, exist_ok=True)
                task_path.write_text(
                    "# Demo Loop\n\n## Checklist\n- [ ] First item\n",
                    encoding="utf-8",
                )
                state = ralph.LoopState(
                    name="demo-loop",
                    task_file=str(task_path),
                    state_file=str(ralph.loop_state_path("demo-loop")),
                    iteration=1,
                    max_iterations=5,
                    items_per_iteration=1,
                    reflect_every=3,
                    reflect_instructions=ralph.DEFAULT_REFLECT_INSTRUCTIONS,
                    status="active",
                    owner_session=ralph.SESSION_OWNER,
                    started_at="2026-04-26T00:00:00+00:00",
                    completed_at=None,
                    last_reflection_at=0,
                    pending_continuation=True,
                    last_continuation_error=None,
                    last_continuation_error_at=None,
                    updated_at="2026-04-26T00:00:00+00:00",
                )
                ralph.save_loop_state(state)
                ralph.set_current_loop(state.name)

                prompt = followup.next_prompt(state.name, dry_run=False)
                reloaded = ralph.load_loop_state(state.name)

                self.assertEqual(reloaded.iteration, 2)
                self.assertFalse(reloaded.pending_continuation)
                self.assertIsNone(reloaded.last_continuation_error)
                self.assertIn("Iteration 2/5", prompt)

    def test_deliver_next_prompt_advances_pending_loop_after_tmux_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                task_path = ralph.loop_task_path("demo-loop")
                task_path.parent.mkdir(parents=True, exist_ok=True)
                task_path.write_text(
                    "# Demo Loop\n\n## Checklist\n- [ ] First item\n",
                    encoding="utf-8",
                )
                state = ralph.LoopState(
                    name="demo-loop",
                    task_file=str(task_path),
                    state_file=str(ralph.loop_state_path("demo-loop")),
                    iteration=1,
                    max_iterations=5,
                    items_per_iteration=1,
                    reflect_every=3,
                    reflect_instructions=ralph.DEFAULT_REFLECT_INSTRUCTIONS,
                    status="active",
                    owner_session=ralph.SESSION_OWNER,
                    started_at="2026-04-26T00:00:00+00:00",
                    completed_at=None,
                    last_reflection_at=0,
                    pending_continuation=True,
                    last_continuation_error=None,
                    last_continuation_error_at=None,
                    updated_at="2026-04-26T00:00:00+00:00",
                )
                ralph.save_loop_state(state)
                ralph.set_current_loop(state.name)

                with mock.patch.object(followup, "paste_prompt_into_pane") as paste_prompt:
                    prompt = followup.deliver_next_prompt(state.name, "%12", dry_run=False)

                reloaded = ralph.load_loop_state(state.name)
                paste_prompt.assert_called_once_with("%12", prompt)
                self.assertEqual(reloaded.iteration, 2)
                self.assertFalse(reloaded.pending_continuation)
                self.assertIsNone(reloaded.last_continuation_error)
                self.assertIn("Iteration 2/5", prompt)

    def test_deliver_next_prompt_does_not_advance_before_tmux_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                task_path = ralph.loop_task_path("demo-loop")
                task_path.parent.mkdir(parents=True, exist_ok=True)
                task_path.write_text(
                    "# Demo Loop\n\n## Checklist\n- [ ] First item\n",
                    encoding="utf-8",
                )
                state = ralph.LoopState(
                    name="demo-loop",
                    task_file=str(task_path),
                    state_file=str(ralph.loop_state_path("demo-loop")),
                    iteration=1,
                    max_iterations=5,
                    items_per_iteration=1,
                    reflect_every=3,
                    reflect_instructions=ralph.DEFAULT_REFLECT_INSTRUCTIONS,
                    status="active",
                    owner_session=ralph.SESSION_OWNER,
                    started_at="2026-04-26T00:00:00+00:00",
                    completed_at=None,
                    last_reflection_at=0,
                    pending_continuation=True,
                    last_continuation_error=None,
                    last_continuation_error_at=None,
                    updated_at="2026-04-26T00:00:00+00:00",
                )
                ralph.save_loop_state(state)
                ralph.set_current_loop(state.name)

                with mock.patch.object(
                    followup,
                    "paste_prompt_into_pane",
                    side_effect=RuntimeError("tmux paste-buffer failed"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "tmux paste-buffer failed"):
                        followup.deliver_next_prompt(state.name, "%12", dry_run=False)

                reloaded = ralph.load_loop_state(state.name)
                self.assertEqual(reloaded.iteration, 1)
                self.assertTrue(reloaded.pending_continuation)
                self.assertIsNone(reloaded.last_continuation_error)

    def test_paste_prompt_into_pane_uses_tmux_bracketed_paste(self) -> None:
        calls: list[list[str]] = []

        def fake_run_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with mock.patch.object(followup, "run_tmux", side_effect=fake_run_tmux):
            followup.paste_prompt_into_pane("%12", "line 1\nline 2\n")

        self.assertEqual(calls[0][:2], ["load-buffer", "-b"])
        self.assertEqual(calls[1], ["paste-buffer", "-p", "-t", "%12", "-b", "ralph-next"])
        self.assertEqual(calls[2], ["send-keys", "-t", "%12", "Enter"])

    def test_clear_followup_pending_records_error_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with temporary_ralph_repo(repo_root):
                ralph.ensure_state_dirs()
                task_path = ralph.loop_task_path("demo-loop")
                task_path.parent.mkdir(parents=True, exist_ok=True)
                task_path.write_text(
                    "# Demo Loop\n\n## Checklist\n- [ ] First item\n",
                    encoding="utf-8",
                )
                state = ralph.LoopState(
                    name="demo-loop",
                    task_file=str(task_path),
                    state_file=str(ralph.loop_state_path("demo-loop")),
                    iteration=1,
                    max_iterations=5,
                    items_per_iteration=1,
                    reflect_every=3,
                    reflect_instructions=ralph.DEFAULT_REFLECT_INSTRUCTIONS,
                    status="active",
                    owner_session=ralph.SESSION_OWNER,
                    started_at="2026-04-26T00:00:00+00:00",
                    completed_at=None,
                    last_reflection_at=0,
                    pending_continuation=True,
                    last_continuation_error=None,
                    last_continuation_error_at=None,
                    updated_at="2026-04-26T00:00:00+00:00",
                )
                ralph.save_loop_state(state)
                ralph.set_current_loop(state.name)

                followup.clear_followup_pending(state.name, error="timed out waiting for Codex idle state")
                reloaded = ralph.load_loop_state(state.name)
                status_text = ralph.render_status(reloaded)
                list_text = ralph.render_status_list([reloaded])

                self.assertFalse(reloaded.pending_continuation)
                self.assertEqual(reloaded.last_continuation_error, "timed out waiting for Codex idle state")
                self.assertIn("Last follow-up error: timed out waiting for Codex idle state", status_text)
                self.assertIn("follow-up failed", list_text)


if __name__ == "__main__":
    unittest.main()
