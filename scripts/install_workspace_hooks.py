#!/usr/bin/env python3
"""Install the workspace-level hooks.json that activates Ralph follow-up."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_hooks(hook_script: Path) -> dict:
    command = f'python3 "{hook_script}"'

    def event_group(status_message: str) -> list[dict]:
        return [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "timeout": 5,
                        "statusMessage": status_message,
                    }
                ],
            }
        ]

    return {
        "hooks": {
            "SessionStart": event_group("Loading Ralph loop context"),
            "UserPromptSubmit": event_group("Checking Ralph loop context"),
            "Stop": event_group("Advancing Ralph loop"),
        }
    }


def default_target(script_file: Path) -> Path:
    plugin_root = script_file.resolve().parent.parent
    workspace_root = plugin_root.parent.parent
    return workspace_root / ".codex" / "hooks.json"


def expected_contents(script_file: Path) -> str:
    plugin_root = script_file.resolve().parent.parent
    hook_script = plugin_root / "scripts" / "ralph_hook.py"
    return json.dumps(build_hooks(hook_script), indent=2) + "\n"


def main() -> int:
    script_file = Path(__file__).resolve()

    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, default=default_target(script_file))
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the target file does not match the expected content",
    )
    args = parser.parse_args()

    target = args.target.expanduser()
    expected = expected_contents(script_file)

    if args.check:
        if not target.is_file():
            print(f"missing: {target}", file=sys.stderr)
            return 1
        actual = target.read_text(encoding="utf-8")
        if actual != expected:
            print(f"out of date: {target}", file=sys.stderr)
            return 1
        print(f"up to date: {target}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
