"""Platform-aware UI copy + capability flags (Windows / macOS / Linux).

Run: python -m unittest tests.test_ui_copy -v
"""

from __future__ import annotations

import os
import sys
import unittest
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ui_copy
import bridge


class _CopyAttrCollector(HTMLParser):
    """Collect data-copy / data-feature bindings from index.html for render checks."""

    def __init__(self):
        super().__init__()
        self.copy_keys: list[tuple[str, str]] = []  # (key, attr)
        self.feature_names: list[str] = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if "data-copy" in ad:
            self.copy_keys.append((ad["data-copy"], ad.get("data-copy-attr") or "text"))
        if "data-feature" in ad:
            self.feature_names.append(ad["data-feature"])


def _render_placeholders(copy: dict[str, str]) -> dict[str, str]:
    """Deterministic 'DOM' map: key → resolved string (as applyUiCopy would set)."""
    return {
        "ws-input.placeholder": copy["workspace.path_placeholder"],
        "set-models-dir.placeholder": copy["models_dir.placeholder"],
        "setup-models-dir.placeholder": copy["setup.models_dir.placeholder"],
        "desktop.allowlist.placeholder": copy["desktop.allowlist.placeholder"],
        "shell.history_title": copy["shell.history_title"],
        "setup.llama_hint": copy["setup.llama_hint"],
        "setup.missing_install": copy.get("setup.missing_install", ""),
        "sandbox.visible": "yes" if copy.get("sandbox.section_hint") else "no",
        "llama_basename": copy["llama_bin.basename"],
    }


class NormalizeOsTest(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(ui_copy.normalize_os("win32"), "windows")
        self.assertEqual(ui_copy.normalize_os("Windows"), "windows")
        self.assertEqual(ui_copy.normalize_os("darwin"), "macos")
        self.assertEqual(ui_copy.normalize_os("macos"), "macos")
        self.assertEqual(ui_copy.normalize_os("linux"), "linux")
        self.assertEqual(ui_copy.normalize_os("linux2"), "linux")


class FeaturesTest(unittest.TestCase):
    def test_windows_only_sandbox(self):
        fw = ui_copy.ui_features("windows")
        self.assertTrue(fw["sandbox_wsl"])
        self.assertTrue(fw["llama_one_click_windows"])
        self.assertTrue(fw["registry_tools"])
        self.assertTrue(fw["network_snapshot"])
        self.assertFalse(fw["metal_guidance"])

    def test_macos_metal_homebrew(self):
        fm = ui_copy.ui_features("macos")
        self.assertFalse(fm["sandbox_wsl"])
        self.assertFalse(fm["llama_one_click_windows"])
        self.assertTrue(fm["metal_guidance"])
        self.assertTrue(fm["homebrew_paths"])

    def test_linux_no_wsl(self):
        fl = ui_copy.ui_features("linux")
        self.assertFalse(fl["sandbox_wsl"])
        self.assertFalse(fl["network_snapshot"])
        self.assertFalse(fl["metal_guidance"])


class CopyCatalogTest(unittest.TestCase):
    def test_sanitize_all_platforms(self):
        for os_key in ("windows", "macos", "linux"):
            with self.subTest(os=os_key):
                ui_copy.assert_platform_copy_sanitized(os_key)

    def test_windows_render_map(self):
        c = ui_copy.resolve_ui_copy("windows")
        r = _render_placeholders(c)
        self.assertTrue(r["ws-input.placeholder"].startswith("C:"))
        self.assertIn("MODELS", r["set-models-dir.placeholder"])
        self.assertEqual(r["llama_basename"], "llama-server.exe")
        self.assertIn("PowerShell", r["shell.history_title"])
        self.assertIn("WSL", c["sandbox.wiz_checking"])
        self.assertIn("elevated PowerShell", c["sandbox.no_wsl_body"])

    def test_macos_render_map(self):
        c = ui_copy.resolve_ui_copy("macos")
        r = _render_placeholders(c)
        self.assertTrue(r["ws-input.placeholder"].startswith("/Users/"))
        self.assertNotIn("\\", r["ws-input.placeholder"])
        self.assertEqual(r["llama_basename"], "llama-server")
        self.assertIn("Homebrew", r["setup.llama_hint"])
        self.assertIn("brew install", r["setup.missing_install"].lower())
        self.assertIn("Metal", c["memory_budget.tooltip"])
        self.assertNotIn("WSL", c["sandbox.wiz_checking"])
        self.assertNotIn("elevated PowerShell", "\n".join(c.values()))
        self.assertEqual(r["shell.history_title"], "Shell command history")

    def test_linux_render_map(self):
        c = ui_copy.resolve_ui_copy("linux")
        r = _render_placeholders(c)
        self.assertTrue(
            r["ws-input.placeholder"].startswith("/home/")
            or r["ws-input.placeholder"].startswith("/")
        )
        self.assertEqual(r["llama_basename"], "llama-server")
        self.assertNotIn("Homebrew", r["setup.missing_install"])
        self.assertNotIn("WSL2", "\n".join(c.values()))
        self.assertNotIn("llama-server.exe", "\n".join(c.values()))

    def test_neutral_keys_always_present(self):
        for os_key in ("windows", "macos", "linux"):
            c = ui_copy.resolve_ui_copy(os_key)
            for key in (
                "llama_bin.label",
                "workspace.path_placeholder",
                "models_dir.placeholder",
                "shell.approval_sub",
                "trust.title",
            ):
                self.assertIn(key, c)
                self.assertTrue(c[key].strip())


class AttachCapabilitiesTest(unittest.TestCase):
    def test_attach_preserves_hardware_and_adds_copy(self):
        info = {
            "os": "macos",
            "gpu_name": "Apple M4 Pro",
            "is_apple_silicon": True,
        }
        out = ui_copy.attach_ui_capabilities(info)
        self.assertEqual(out["os"], "macos")
        self.assertEqual(out["gpu_name"], "Apple M4 Pro")
        self.assertIn("copy", out)
        self.assertIn("features", out)
        self.assertFalse(out["features"]["sandbox_wsl"])
        self.assertIn("/Users/", out["copy"]["workspace.path_placeholder"])

    def test_build_ui_capabilities_windows(self):
        caps = ui_copy.build_ui_capabilities(os_name="windows")
        self.assertEqual(caps["os"], "windows")
        self.assertTrue(caps["features"]["sandbox_wsl"])


class SysinfoApiCopyTest(unittest.TestCase):
    def tearDown(self):
        bridge._HW_SPECS_CACHE = None

    def test_sysinfo_includes_copy_on_darwin_detect(self):
        hw = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values={
                "machdep.cpu.brand_string": "Apple M4 Pro",
                "hw.memsize": str(24 * 1024 ** 3),
                "hw.optional.arm64": "1",
            },
            list_devices_text="Available devices:\n  MTL0: Apple M4 Pro (1 MiB, 1 MiB free)\n",
            use_cache=False,
        )
        attached = ui_copy.attach_ui_capabilities(hw)
        self.assertEqual(attached["os"], "macos")
        self.assertIn("copy", attached)
        self.assertFalse(attached["features"]["sandbox_wsl"])

    def test_sysinfo_handler_path_uses_attach(self):
        """Smoke: /api/setup/sysinfo wiring calls attach_ui_capabilities."""
        import inspect
        src = inspect.getsource(bridge.Handler._handle_api_get)
        self.assertIn("attach_ui_capabilities", src)
        self.assertIn("/api/setup/sysinfo", src)
        self.assertIn("/api/ui-capabilities", src)
        self.assertIn("build_ui_capabilities", src)

    def test_attach_without_os_uses_host_platform(self):
        """Hardware failure path: empty info still gets features from sys.platform."""
        out = ui_copy.attach_ui_capabilities({"gpu_name": "unknown"})
        self.assertIn(out["os"], ("windows", "macos", "linux"))
        self.assertIn("sandbox_wsl", out["features"])
        self.assertTrue(out["copy"])

    def test_windows_features_fail_open_when_os_windows(self):
        out = ui_copy.attach_ui_capabilities({"os": "windows", "gpu_name": "x"})
        self.assertTrue(out["features"]["sandbox_wsl"])
        self.assertTrue(out["features"]["network_snapshot"])
        self.assertTrue(out["features"]["llama_one_click_windows"])


class IndexHtmlBindingsTest(unittest.TestCase):
    """Ensure markup exposes catalog keys + feature gates (no layout change required)."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "index.html"), encoding="utf-8") as f:
            html = f.read()
        parser = _CopyAttrCollector()
        parser.feed(html)
        cls.keys = {k for k, _ in parser.copy_keys}
        cls.features = set(parser.feature_names)
        cls.html = html

    def test_critical_copy_bindings(self):
        for key in (
            "workspace.path_placeholder",
            "models_dir.placeholder",
            "setup.models_dir.placeholder",
            "setup.llama_hint",
            "shell.history_title",
            "trust.title",
            "desktop.allowlist.placeholder",
            "sandbox.wiz_checking",
        ):
            self.assertIn(key, self.keys, f"missing data-copy={key}")

    def test_feature_gates_present(self):
        self.assertIn("sandbox_wsl", self.features)
        self.assertIn("network_snapshot", self.features)

    def test_sandbox_sections_default_hidden(self):
        # Avoid WSL flash on macOS/Linux before sysinfo returns.
        self.assertIn('id="settings-sandbox-section"', self.html)
        self.assertIn('id="step-sandbox"', self.html)
        self.assertRegex(
            self.html,
            r'id="settings-sandbox-section"[^>]*\bhidden\b',
        )
        self.assertRegex(
            self.html,
            r'id="step-sandbox"[^>]*\bhidden\b',
        )

    def test_no_hardcoded_windows_placeholders_in_defaults(self):
        # Neutral HTML fallbacks must not be Windows-only (catalog overwrites later).
        self.assertNotIn('placeholder="C:\\path\\to\\folder"', self.html)
        self.assertNotIn("placeholder=\"C:\\Users\\you\\MODELS\"", self.html)
        self.assertNotIn("e.g. D:\\\\MODELS", self.html)


class SimulatedDomApplyTest(unittest.TestCase):
    """Simulate frontend applyUiCopy for all three platforms without a browser."""

    def _apply(self, os_key: str) -> dict[str, str | bool]:
        caps = ui_copy.build_ui_capabilities(os_name=os_key)
        copy = caps["copy"]
        feats = caps["features"]
        return {
            "os": caps["os"],
            "ws_placeholder": copy["workspace.path_placeholder"],
            "models_placeholder": copy["models_dir.placeholder"],
            "sandbox_shown": feats["sandbox_wsl"],
            "one_click_shown": feats["llama_one_click_windows"],
            "netscan_shown": feats["network_snapshot"],
            "shell_title": copy["shell.history_title"],
            "missing_install": copy.get("setup.missing_install", ""),
            "llama_name": copy["llama_bin.basename"],
        }

    def test_three_platform_matrix(self):
        win = self._apply("windows")
        mac = self._apply("macos")
        lin = self._apply("linux")

        self.assertTrue(win["sandbox_shown"])
        self.assertFalse(mac["sandbox_shown"])
        self.assertFalse(lin["sandbox_shown"])

        self.assertTrue(win["one_click_shown"])
        self.assertFalse(mac["one_click_shown"])
        self.assertFalse(lin["one_click_shown"])

        self.assertEqual(win["llama_name"], "llama-server.exe")
        self.assertEqual(mac["llama_name"], "llama-server")
        self.assertEqual(lin["llama_name"], "llama-server")

        self.assertIn("C:", win["ws_placeholder"])
        self.assertIn("/Users/", mac["ws_placeholder"])
        self.assertIn("/home/", lin["ws_placeholder"])

        self.assertIn("PowerShell", win["shell_title"])
        self.assertEqual(mac["shell_title"], "Shell command history")
        self.assertEqual(lin["shell_title"], "Shell command history")

        self.assertIn("brew", mac["missing_install"].lower())
        self.assertNotIn("brew", lin["missing_install"].lower())


if __name__ == "__main__":
    unittest.main()
