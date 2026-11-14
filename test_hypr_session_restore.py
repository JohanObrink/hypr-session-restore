#!/usr/bin/env python3
"""Tests for hypr-session-restore.

Run with: python3 -m unittest test_hypr_session_restore
"""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from unittest.mock import patch

# Load the script as a module even though its filename contains a hyphen.
SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "hypr-session-restore")
loader = importlib.machinery.SourceFileLoader("hypr_session_restore", SCRIPT_PATH)
spec = importlib.util.spec_from_loader("hypr_session_restore", loader)
hsr = importlib.util.module_from_spec(spec)
loader.exec_module(hsr)


def _client(cls, pid, ws_id=1, ws_name=None, mapped=True):
    """Build a minimal hyprctl client dict for testing."""
    if ws_name is None:
        ws_name = str(ws_id)
    return {
        "address": f"0x{cls}{pid}",
        "mapped": mapped,
        "hidden": False,
        "visible": True,
        "acceptsInput": True,
        "at": [0, 0],
        "size": [800, 600],
        "workspace": {"address": str(ws_id), "id": ws_id, "type": "normal", "name": ws_name},
        "floating": False,
        "monitor": 0,
        "class": cls,
        "title": cls,
        "initialClass": cls,
        "initialTitle": cls,
        "pid": pid,
        "xwayland": False,
        "pinned": False,
        "fullscreen": 0,
        "fullscreenClient": 0,
        "focusHistoryID": 0,
    }


class TestSaveSpawnLogic(unittest.TestCase):
    """Tests for how save() decides which windows to relaunch."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.session_file = os.path.join(self.tmpdir.name, "session.json")
        # Point the module at our temp session file for this test.
        self.session_file_patcher = patch.object(hsr, "SESSION_FILE", self.session_file)
        self.session_file_patcher.start()

    def tearDown(self):
        self.session_file_patcher.stop()
        self.tmpdir.cleanup()

    def _run_save(self, clients):
        """Run save() with mocked hyprctl and /proc helpers."""
        with patch.object(hsr, "hyprctl_json", return_value=clients), \
             patch.object(hsr, "proc_cmdline", return_value=["app"]), \
             patch.object(hsr, "proc_cwd", return_value="/home"), \
             patch.object(hsr, "proc_comm", return_value=None), \
             patch.object(hsr, "proc_children", return_value=[]):
            return hsr.save()

    def _load_windows(self):
        with open(self.session_file) as f:
            return json.load(f)["windows"]

    def test_regular_app_collapses_same_pid(self):
        """Normal apps: only the first window per PID is relaunched."""
        clients = [
            _client("myapp", 100, ws_id=1),
            _client("myapp", 100, ws_id=2),
        ]
        count = self._run_save(clients)
        self.assertEqual(count, 2)
        windows = self._load_windows()
        self.assertTrue(windows[0]["spawn"])
        self.assertFalse(windows[1]["spawn"])

    def test_ghostty_spawns_every_window(self):
        """Ghostty is single-instance multi-window: each window spawns with +new-window."""
        clients = [
            _client("com.mitchellh.ghostty", 200, ws_id=1),
            _client("com.mitchellh.ghostty", 200, ws_id=2),
            _client("com.mitchellh.ghostty", 200, ws_id=3),
        ]
        count = self._run_save(clients)
        self.assertEqual(count, 3)
        windows = self._load_windows()
        self.assertTrue(all(w["spawn"] for w in windows))
        for w in windows:
            self.assertEqual(w["cmd"], "ghostty --working-directory=/home +new-window")

    def test_chromium_spawns_once(self):
        """Chromium is single-instance self-restoring: only one spawn."""
        clients = [
            _client("chromium-browser", 300, ws_id=1),
            _client("chromium-browser", 300, ws_id=2),
            _client("chromium-browser", 300, ws_id=3),
        ]
        count = self._run_save(clients)
        self.assertEqual(count, 3)
        windows = self._load_windows()
        spawns = [w["spawn"] for w in windows]
        self.assertEqual(spawns.count(True), 1)
        self.assertEqual(spawns.count(False), 2)

    def test_excluded_classes_are_skipped(self):
        """Windows in EXCLUDE_CLASSES are not saved."""
        clients = [
            _client("org.quickshell", 400),
            _client("myapp", 401),
        ]
        count = self._run_save(clients)
        self.assertEqual(count, 1)
        windows = self._load_windows()
        self.assertEqual(windows[0]["class"], "myapp")

    def test_class_cmd_mapping_rewrites_command(self):
        """User-defined class-to-command mappings rewrite the relaunch cmd."""
        class_cmd_file = os.path.join(self.tmpdir.name, "class-cmd.conf")
        with open(class_cmd_file, "w") as f:
            f.write(r"chrome-(?P<domain>[^_]+)__-Profile_\d+ = app --url=https://{domain}/" + "\n")
        with patch.object(hsr, "CLASS_CMD_FILE", class_cmd_file):
            clients = [_client("chrome-example.com__-Profile_1", 500, ws_id=2)]
            count = self._run_save(clients)
            self.assertEqual(count, 1)
            windows = self._load_windows()
            self.assertEqual(windows[0]["cmd"], "app --url=https://example.com/")
            self.assertTrue(windows[0]["spawn"])

    def test_class_cmd_classes_spawn_once(self):
        """Classes with a class-cmd mapping spawn once per unique class."""
        class_cmd_file = os.path.join(self.tmpdir.name, "class-cmd.conf")
        with open(class_cmd_file, "w") as f:
            f.write(r"chrome-(?P<domain>[^_]+)__-Profile_\d+ = app --url=https://{domain}/" + "\n")
        with patch.object(hsr, "CLASS_CMD_FILE", class_cmd_file):
            clients = [
                _client("chrome-a.com__-Profile_1", 600, ws_id=1),
                _client("chrome-a.com__-Profile_1", 600, ws_id=2),
                _client("chrome-b.com__-Profile_1", 600, ws_id=3),
            ]
            count = self._run_save(clients)
            self.assertEqual(count, 3)
            windows = self._load_windows()
            spawns = [w["spawn"] for w in windows]
            self.assertEqual(spawns.count(True), 2)
            self.assertEqual(spawns.count(False), 1)


class TestClassCmdHelpers(unittest.TestCase):
    """Tests for class-cmd.conf parsing and command generation."""

    def test_load_class_cmd_patterns_parses_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write("# comment\n")
            f.write("foo-(?P<x>\\d+) = bar {x}\n")
            f.write("bad line with no equals\n")
            path = f.name
        try:
            with patch.object(hsr, "CLASS_CMD_FILE", path):
                patterns = hsr.load_class_cmd_patterns()
            self.assertEqual(len(patterns), 1)
            pattern, template = patterns[0]
            self.assertEqual(pattern.pattern, r"foo-(?P<x>\d+)")
            self.assertEqual(template, "bar {x}")
        finally:
            os.unlink(path)

    def test_class_relaunch_command_substitutes_groups(self):
        patterns = [(hsr.re.compile(r"chrome-(?P<domain>[^_]+)__"), "app https://{domain}/")]
        cmd = hsr.class_relaunch_command("chrome-example.com__-Profile_1", patterns)
        self.assertEqual(cmd, "app https://example.com/")

    def test_class_relaunch_command_no_match_returns_none(self):
        patterns = [(hsr.re.compile(r"foo-(?P<x>\d+)"), "bar {x}")]
        self.assertIsNone(hsr.class_relaunch_command("baz", patterns))


if __name__ == "__main__":
    unittest.main()
