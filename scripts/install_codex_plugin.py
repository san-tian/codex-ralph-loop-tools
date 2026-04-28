#!/usr/bin/env python3
"""Install Ralph Loop Tools into Codex's always-discovered plugin locations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable


PLUGIN_NAME = "ralph-loop-tools"
MARKETPLACE_NAME = "workspace-local"
PLUGIN_CONFIG_KEY = f'{PLUGIN_NAME}@{MARKETPLACE_NAME}'
MARKETPLACE_SOURCE_PATH = f"./plugins/{PLUGIN_NAME}"
IGNORED_DIRS = {".git", ".tmp", "__pycache__"}
IGNORED_FILE_SUFFIXES = {".pyc"}


def plugin_root(script_file: Path) -> Path:
    return script_file.resolve().parent.parent


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def home_root() -> Path:
    return Path.home()


def home_plugin_mirror() -> Path:
    return home_root() / "plugins" / PLUGIN_NAME


def home_marketplace_path() -> Path:
    return home_root() / ".agents" / "plugins" / "marketplace.json"


def cache_plugin_path() -> Path:
    return codex_home() / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME / "local"


def config_path() -> Path:
    return codex_home() / "config.toml"


def should_ignore_dir(path: Path) -> bool:
    return path.name in IGNORED_DIRS


def iter_tree_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(should_ignore_dir(parent) for parent in path.relative_to(root).parents):
            continue
        if should_ignore_dir(path):
            continue
        if path.is_file() and path.suffix not in IGNORED_FILE_SUFFIXES:
            yield path


def tree_digest(root: Path) -> str | None:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in iter_tree_files(root):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def copy_plugin_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)

    def ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = set()
        for name in names:
            path = Path(name)
            if name in IGNORED_DIRS or path.suffix in IGNORED_FILE_SUFFIXES:
                ignored.add(name)
        return ignored

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=ignore)


def marketplace_entry() -> dict:
    return {
        "name": PLUGIN_NAME,
        "source": {
            "source": "local",
            "path": MARKETPLACE_SOURCE_PATH,
        },
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Productivity",
    }


def expected_marketplace() -> dict:
    return {
        "name": MARKETPLACE_NAME,
        "interface": {"displayName": "Workspace Local Plugins"},
        "plugins": [marketplace_entry()],
    }


def marketplace_has_entry(payload: dict) -> bool:
    if payload.get("name") != MARKETPLACE_NAME:
        return False
    return any(plugin == marketplace_entry() for plugin in payload.get("plugins", []))


def write_marketplace(path: Path) -> None:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("name") != MARKETPLACE_NAME:
            raise ValueError(
                f"Refusing to rewrite marketplace {path}: expected name {MARKETPLACE_NAME!r}."
            )
        payload.setdefault("interface", {"displayName": "Workspace Local Plugins"})
        plugins = [
            plugin
            for plugin in payload.get("plugins", [])
            if plugin.get("name") != PLUGIN_NAME
        ]
        plugins.append(marketplace_entry())
        payload["plugins"] = plugins
    else:
        payload = expected_marketplace()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def section_bounds(lines: list[str], section: str) -> tuple[int | None, int]:
    header = f"[{section}]"
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == header:
            start = idx
            break
    if start is None:
        return None, len(lines)
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = idx
            break
    return start, end


def ensure_toml_bool(lines: list[str], section: str, key: str, value: bool) -> list[str]:
    desired = f"{key} = {'true' if value else 'false'}\n"
    start, end = section_bounds(lines, section)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend([f"[{section}]\n", desired])
        return lines
    for idx in range(start + 1, end):
        if lines[idx].strip().startswith(f"{key} "):
            lines[idx] = desired
            return lines
    lines.insert(start + 1, desired)
    return lines


def config_is_enabled(path: Path) -> bool:
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    checks = [("features", "plugins"), (f'plugins."{PLUGIN_CONFIG_KEY}"', "enabled")]
    for section, key in checks:
        start, end = section_bounds(lines, section)
        if start is None:
            return False
        matched = False
        for idx in range(start + 1, end):
            stripped = lines[idx].strip()
            if stripped.startswith(f"{key} "):
                matched = stripped == f"{key} = true"
                break
        if not matched:
            return False
    return True


def ensure_config_enabled(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []
    lines = ensure_toml_bool(lines, "features", "plugins", True)
    lines = ensure_toml_bool(lines, f'plugins."{PLUGIN_CONFIG_KEY}"', "enabled", True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


def check_install(source: Path) -> list[str]:
    errors = []
    source_digest = tree_digest(source)
    for target in (home_plugin_mirror(), cache_plugin_path()):
        if tree_digest(target) != source_digest:
            errors.append(f"out of date: {target}")
    marketplace = home_marketplace_path()
    if not marketplace.is_file():
        errors.append(f"missing: {marketplace}")
    else:
        try:
            payload = json.loads(marketplace.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON: {marketplace}: {exc}")
        else:
            if not marketplace_has_entry(payload):
                errors.append(f"missing Ralph entry: {marketplace}")
    if not config_is_enabled(config_path()):
        errors.append(f"plugin not enabled: {config_path()}")
    return errors


def main() -> int:
    source = plugin_root(Path(__file__))
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the Codex plugin install/cache is out of date",
    )
    args = parser.parse_args()

    if args.check:
        errors = check_install(source)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print("up to date: Codex Ralph plugin install")
        return 0

    copy_plugin_tree(source, home_plugin_mirror())
    write_marketplace(home_marketplace_path())
    copy_plugin_tree(source, cache_plugin_path())
    ensure_config_enabled(config_path())
    print(f"wrote {home_plugin_mirror()}")
    print(f"wrote {home_marketplace_path()}")
    print(f"wrote {cache_plugin_path()}")
    print(f"updated {config_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
