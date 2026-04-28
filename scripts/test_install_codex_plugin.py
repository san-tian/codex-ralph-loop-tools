from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import scripts.install_codex_plugin as install


class InstallCodexPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old_home = os.environ.get("HOME")
        self.old_codex_home = os.environ.get("CODEX_HOME")
        os.environ["HOME"] = str(self.root / "home")
        os.environ["CODEX_HOME"] = str(self.root / "codex")

    def tearDown(self) -> None:
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        if self.old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.old_codex_home

    def make_source(self) -> Path:
        source = self.root / "source"
        (source / ".codex-plugin").mkdir(parents=True)
        (source / "scripts").mkdir()
        (source / "skills" / "ralph").mkdir(parents=True)
        (source / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": install.PLUGIN_NAME,
                    "version": "1.2.3",
                    "mcpServers": "./.mcp.json",
                    "skills": "./skills/",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (source / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"ralph-loop-tools": {"command": "python3"}}}) + "\n",
            encoding="utf-8",
        )
        (source / "scripts" / "ralph_loop_mcp_server.py").write_text(
            "print('ok')\n", encoding="utf-8"
        )
        (source / "skills" / "ralph" / "SKILL.md").write_text("# Ralph\n", encoding="utf-8")
        return source

    def test_install_paths_and_check_are_consistent(self) -> None:
        source = self.make_source()

        install.install_plugin_tree(source, install.home_plugin_mirror())
        install.write_marketplace(install.home_marketplace_path())
        install.install_plugin_tree(source, install.cache_plugin_path(source))
        install.ensure_config_enabled(install.config_path())

        self.assertEqual([], install.check_install(source))
        expected_cache = (
            self.root
            / "codex"
            / "plugins"
            / "cache"
            / install.MARKETPLACE_NAME
            / install.PLUGIN_NAME
            / "1.2.3"
        )
        self.assertEqual(expected_cache, install.cache_plugin_path(source))
        marketplace = json.loads(install.home_marketplace_path().read_text(encoding="utf-8"))
        self.assertEqual(install.MARKETPLACE_NAME, marketplace["name"])
        self.assertIn(install.marketplace_entry(), marketplace["plugins"])
        self.assertTrue(install.config_is_enabled(install.config_path()))
        runtime_mcp = json.loads((expected_cache / ".mcp.json").read_text(encoding="utf-8"))
        server = runtime_mcp["mcpServers"][install.PLUGIN_NAME]
        expected_runtime_root = self.root / "home" / "plugins" / install.PLUGIN_NAME
        self.assertEqual("python3", server["command"])
        self.assertEqual(str(expected_runtime_root), server["cwd"])
        self.assertEqual(
            ["-u", str(expected_runtime_root / "scripts" / "ralph_loop_mcp_server.py")],
            server["args"],
        )

    def test_check_uses_manifest_versioned_cache_directory(self) -> None:
        source = self.make_source()
        old_local_cache = (
            self.root
            / "codex"
            / "plugins"
            / "cache"
            / install.MARKETPLACE_NAME
            / install.PLUGIN_NAME
            / "local"
        )

        install.install_plugin_tree(source, install.home_plugin_mirror())
        install.write_marketplace(install.home_marketplace_path())
        install.install_plugin_tree(source, install.cache_plugin_path(source))
        install.ensure_config_enabled(install.config_path())

        self.assertFalse(old_local_cache.exists())
        self.assertEqual([], install.check_install(source))

    def test_check_reports_missing_install(self) -> None:
        source = self.make_source()

        errors = install.check_install(source)

        self.assertTrue(any("out of date" in error for error in errors))
        self.assertTrue(any("missing" in error for error in errors))
        self.assertTrue(any("plugin not enabled" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
