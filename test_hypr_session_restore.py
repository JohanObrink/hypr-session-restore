#!/usr/bin/env python3
"""Tests for hypr-session-restore.

Run with: python3 -m unittest test_hypr_session_restore
"""

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
        """Ghostty is single-instance multi-window: each window spawns."""
        clients = [
            _client("ghostty", 200, ws_id=1),
            _client("ghostty", 200, ws_id=2),
            _client("ghostty", 200, ws_id=3),
        ]
        count = self._run_save(clients)
        self.assertEqual(count, 3)
        windows = self._load_windows()
        self.assertTrue(all(w["spawn"] for w in windows))

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
            _client("waybar", 400),
            _client("myapp", 401),
        ]
        count = self._run_save(clients)
        self.assertEqual(count, 1)
        windows = self._load_windows()
        self.assertEqual(windows[0]["class"], "myapp")


if __name__ == "__main__":
    unittest.main()
