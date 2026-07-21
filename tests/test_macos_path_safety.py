"""Deterministic macOS autonomous-execution path/shell safety tests.

Bypass attempts: traversal, symlinks, quotes, spaces, env vars, recursive
delete variants, chaining, subshells, /Volumes, and valid in-workspace ops.

Run: python3 -m unittest tests.test_macos_path_safety -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import bridge


@unittest.skipUnless(sys.platform == "darwin", "macOS path safety baseline")
class MacOSPathSafetyTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="accuretta-path-safety-")
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name).resolve()
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        (self.ws / "ok.txt").write_text("hello workspace\n", encoding="utf-8")
        self.spaced = self.ws / "My Project"
        self.spaced.mkdir()
        (self.spaced / "note.txt").write_text("spaced\n", encoding="utf-8")

        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".ssh").mkdir()
        (self.fake_home / ".ssh" / "id_rsa").write_text("SECRET_KEY\n", encoding="utf-8")
        (self.fake_home / ".aws").mkdir()
        (self.fake_home / ".aws" / "credentials").write_text("SECRET_AWS\n", encoding="utf-8")
        (self.fake_home / "Library" / "Keychains").mkdir(parents=True)
        (self.fake_home / "Library" / "Keychains" / "login.keychain-db").write_text(
            "SECRET_KC\n", encoding="utf-8"
        )

        self._home_env = mock.patch.dict(os.environ, {"HOME": str(self.fake_home)})
        self._home_env.start()
        self.addCleanup(self._home_env.stop)
        self._home_path = mock.patch.object(Path, "home", return_value=self.fake_home)
        self._home_path.start()
        self.addCleanup(self._home_path.stop)

        self._ws = mock.patch.object(
            bridge, "get_workspace", return_value={"folders": [str(self.ws)]}
        )
        self._ws.start()
        self.addCleanup(self._ws.stop)

    def test_valid_workspace_read_write_list(self):
        r = bridge.tool_read_file({"path": str(self.ws / "ok.txt")})
        self.assertNotIn("error", r)
        self.assertIn("hello workspace", r.get("content", ""))

        listed = bridge.tool_list_directory({"path": str(self.ws)})
        self.assertNotIn("error", listed)
        names = {e["name"] for e in listed.get("entries", [])}
        self.assertIn("ok.txt", names)

        with mock.patch.object(bridge, "request_approval",
                               return_value={"decision": "approve", "status": "ok"}):
            w = bridge.tool_write_file({
                "path": str(self.ws / "new file.txt"),
                "content": "created",
            })
        self.assertTrue(w.get("ok"), msg=w)
        self.assertEqual(
            (self.ws / "new file.txt").read_text(encoding="utf-8"), "created"
        )

    def test_list_directory_defaults_to_workspace_not_home(self):
        listed = bridge.tool_list_directory({})
        self.assertNotIn("error", listed)
        self.assertEqual(Path(listed["path"]).resolve(), self.ws)
        blob = str(listed)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("id_rsa", blob)

    def test_empty_workspace_refuses(self):
        with mock.patch.object(bridge, "get_workspace", return_value={"folders": []}):
            self.assertFalse(bridge.is_in_workspace(str(self.ws / "ok.txt")))
            r = bridge.tool_read_file({"path": str(self.ws / "ok.txt")})
            self.assertIn("outside workspace", r.get("error", ""))

    def test_dotdot_traversal(self):
        # Escape attempt via .. — canonical path leaves workspace.
        attack = str(self.ws / ".." / "home" / ".ssh" / "id_rsa")
        r = bridge.tool_read_file({"path": attack})
        self.assertIn("error", r)
        err = r["error"].lower()
        self.assertTrue(
            "blocked" in err or "outside workspace" in err,
            msg=r,
        )
        self.assertNotIn("SECRET", str(r))

    def test_symlink_to_system(self):
        link = self.ws / "escape-system"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("/System")
        self.assertTrue(bridge.is_blocked_path(str(link)))
        r = bridge.tool_list_directory({"path": str(link)})
        self.assertIn("blocked", r.get("error", "").lower())
        self.assertNotIn("/System", str(r))

    def test_symlink_to_ssh(self):
        link = self.ws / "escape-ssh"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(self.fake_home / ".ssh")
        self.assertTrue(bridge.is_blocked_path(str(link)))
        r = bridge.tool_read_file({"path": str(link / "id_rsa")})
        self.assertIn("blocked", r.get("error", "").lower())
        self.assertNotIn("SECRET", str(r))
        self.assertNotIn("id_rsa", str(r))

    def test_path_with_spaces_in_workspace(self):
        r = bridge.tool_read_file({"path": str(self.spaced / "note.txt")})
        self.assertNotIn("error", r)
        self.assertIn("spaced", r.get("content", ""))

    def test_env_variable_path_to_secret(self):
        os.environ["ACCURETTA_PROBE_SECRET"] = str(self.fake_home / ".ssh" / "id_rsa")
        self.addCleanup(lambda: os.environ.pop("ACCURETTA_PROBE_SECRET", None))
        r = bridge.tool_read_file({"path": "$ACCURETTA_PROBE_SECRET"})
        self.assertIn("blocked", r.get("error", "").lower())
        self.assertNotIn("SECRET", str(r))

    def test_quoted_and_home_secret_paths_blocked(self):
        for p in (
            str(self.fake_home / ".ssh" / "id_rsa"),
            str(self.fake_home / ".aws" / "credentials"),
            "~/.ssh/id_rsa",
            str(self.fake_home / "Library" / "Keychains" / "login.keychain-db"),
            "/System/Library/CoreServices",
            "/Library/Preferences",
        ):
            with self.subTest(p=p):
                self.assertTrue(bridge.is_blocked_path(p), p)

    def test_replace_ast_requires_workspace(self):
        outside = self.root / "outside.py"
        outside.write_text("def f():\n    return 1\n", encoding="utf-8")
        r = bridge.tool_replace_ast_node({
            "path": str(outside),
            "node_type": "function_definition",
            "node_name": "f",
            "new_text": "def f():\n    return 2\n",
        })
        self.assertIn("outside workspace", r.get("error", ""))

    def test_windows_block_patterns_still_match(self):
        # Preserved Windows deny list (string match after normalize).
        self.assertTrue(bridge.is_blocked_path(r"C:\Windows\System32\cmd.exe")
                        or bridge.BLOCKED_PATH_PATTERNS[0].search(
                            r"C:\Windows\System32\cmd.exe"))


class MacOSDestructiveCommandTest(unittest.TestCase):
    def test_recursive_delete_variants(self):
        samples = [
            "rm -rf /",
            "rm -rf /*",
            "rm -rf ~",
            "rm -rf ~/*",
            "rm -rf /System",
            "rm -rf '/System'",
            'rm -rf "/Library"',
            "rm -rf /Users",
            "rm -rf /Volumes",
            "rm -rf /Volumes/Backup",
            "rm -rf /private",
            "rm -rf /private/var",
            "rm -rf /Applications",
            "rm -rf ~/Library",
            "rm -rf $HOME/Library",
            "rm -fr /System",
            "rm -rf -- /System",
        ]
        for cmd in samples:
            with self.subTest(cmd=cmd):
                why = bridge._catastrophic_cmd(cmd)
                self.assertIsNotNone(why, cmd)

    def test_command_chaining_and_subshell(self):
        for cmd in (
            "echo hi; rm -rf /System",
            "true && rm -rf /Volumes",
            "echo $(rm -rf /Users)",
            "echo `rm -rf /Library`",
            "rm -rf /System | true",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(bridge._catastrophic_cmd(cmd), cmd)

    def test_normal_workspace_delete_not_catastrophic(self):
        self.assertIsNone(bridge._catastrophic_cmd("rm -rf ./build"))
        self.assertIsNone(bridge._catastrophic_cmd("rm -rf /tmp/accuretta-build-xyz"))
        # Must NOT hard-block ordinary project paths under /Users/...
        self.assertIsNone(
            bridge._catastrophic_cmd("rm -rf /Users/me/Documents/Accuretta\\ Workspace/build")
        )


@unittest.skipUnless(sys.platform == "darwin", "macOS shell path policy")
class MacOSShellApprovalTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="accuretta-shell-safety-")
        self.addCleanup(self._tmpdir.cleanup)
        self.ws = Path(self._tmpdir.name).resolve() / "ws"
        self.ws.mkdir()
        self._ws = mock.patch.object(
            bridge, "get_workspace", return_value={"folders": [str(self.ws)]}
        )
        self._ws.start()
        self.addCleanup(self._ws.stop)
        fake_home = Path(self._tmpdir.name) / "home"
        fake_home.mkdir()
        (fake_home / ".ssh").mkdir()
        self._home = mock.patch.dict(os.environ, {"HOME": str(fake_home)})
        self._home.start()
        self.addCleanup(self._home.stop)
        self._ph = mock.patch.object(Path, "home", return_value=fake_home)
        self._ph.start()
        self.addCleanup(self._ph.stop)

    def test_outside_workspace_requires_approval(self):
        self.assertTrue(bridge.command_touches_outside_workspace("cat /etc/hosts"))
        self.assertTrue(bridge.shell_requires_approval("cat /etc/hosts"))
        self.assertTrue(
            bridge.command_touches_outside_workspace('cat "/etc/hosts"')
        )
        self.assertTrue(bridge.command_touches_outside_workspace("cat $HOME/Documents/x"))

    def test_dotdot_in_command_requires_approval(self):
        self.assertTrue(bridge.command_touches_outside_workspace("cat ../secret"))
        self.assertTrue(bridge.shell_requires_approval("cat ../secret"))

    def test_blocked_path_in_command(self):
        self.assertTrue(bridge.command_touches_blocked_path("cat ~/.ssh/id_rsa"))
        self.assertTrue(bridge.command_touches_blocked_path('cat "/System/Library"'))

    def test_in_workspace_absolute_read_not_outside(self):
        target = self.ws / "a.txt"
        target.write_text("x", encoding="utf-8")
        self.assertFalse(
            bridge.command_touches_outside_workspace(f"cat {target}")
        )

    def test_volumes_path_outside_requires_approval(self):
        self.assertTrue(
            bridge.command_touches_outside_workspace("ls /Volumes/SomeDisk")
        )

    def test_run_powershell_refuses_blocked_without_echoing_secret(self):
        res = bridge.tool_run_powershell({"command": "cat ~/.ssh/id_rsa"})
        self.assertIn("protected location", res.get("error", "").lower())
        self.assertNotIn("SECRET", str(res))

    def test_run_tests_requires_approval(self):
        with mock.patch.object(bridge, "request_approval",
                               return_value={"decision": "deny", "status": "denied"}) as req:
            res = bridge.tool_run_tests({
                "command": "python3 -c 'print(1)'",
                "cwd": str(self.ws),
            })
        req.assert_called_once()
        self.assertIn("denied", res.get("error", "").lower())


class BrowseFolderLoopbackTest(unittest.TestCase):
    def test_loopback_helper(self):
        h = bridge.Handler.__new__(bridge.Handler)
        h.client_address = ("127.0.0.1", 9)
        self.assertTrue(h._peer_is_loopback())
        h.client_address = ("::1", 9)
        self.assertTrue(h._peer_is_loopback())
        h.client_address = ("100.64.1.50", 9)
        self.assertFalse(h._peer_is_loopback())

    def test_browse_folder_rejects_tailscale_peer(self):
        h = bridge.Handler.__new__(bridge.Handler)
        h.client_address = ("100.64.2.3", 4444)
        sent = {}

        def _send_json(status, obj):
            sent["status"] = status
            sent["obj"] = obj
            return None

        h._send_json = _send_json  # type: ignore
        h._read_json = lambda: {"title": "Pick"}  # type: ignore
        # Invoke the browse-folder branch via _handle_api_post
        with mock.patch.object(bridge, "pick_folder") as pick:
            h._handle_api_post("/api/browse-folder", None)
        pick.assert_not_called()
        self.assertEqual(sent.get("status"), 403)
        self.assertEqual(sent["obj"].get("code"), "picker_local_only")


class WindowsPatternsPreservedTest(unittest.TestCase):
    def test_catastrophic_windows_still_works(self):
        self.assertIsNotNone(
            bridge._catastrophic_cmd(r"Remove-Item -Recurse -Force C:\Windows")
        )
        self.assertIsNotNone(
            bridge._catastrophic_cmd(r"rm -rf /mnt/c/Users")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
