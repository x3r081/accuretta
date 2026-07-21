"""Apple Silicon / Metal hardware detection tests (mocked sysctl + list-devices).

Run: python -m unittest tests.test_apple_silicon_hw -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge


MTL_M4_PRO = """Available devices:
  BLAS: Accelerate (0 MiB, 0 MiB free)
  MTL0: Apple M4 Pro (18186 MiB, 18185 MiB free)
"""

MTL_M2 = """Available devices:
  MTL0: Apple M2 (11456 MiB, 11000 MiB free)
"""

CPU_ONLY = """Available devices:
  BLAS: Accelerate (0 MiB, 0 MiB free)
"""


class ParseDevicesTest(unittest.TestCase):
    def test_parse_metal_m4(self):
        devices = bridge.parse_llama_list_devices(MTL_M4_PRO)
        self.assertTrue(any(d["backend"] == "MTL" for d in devices))
        mtl = next(d for d in devices if d["backend"] == "MTL")
        self.assertEqual(mtl["name"], "Apple M4 Pro")
        self.assertEqual(mtl["memory_mib"], 18186)
        self.assertEqual(mtl["free_mib"], 18185)


class AppleSiliconDetectionTest(unittest.TestCase):
    def tearDown(self):
        bridge._HW_SPECS_CACHE = None
        bridge._METAL_RUNTIME_SELECTED = False

    def _sysctl_m4_24(self):
        return {
            "machdep.cpu.brand_string": "Apple M4 Pro",
            "hw.memsize": str(24 * 1024 ** 3),
            "hw.optional.arm64": "1",
        }

    def test_m4_pro_24gb_with_metal_build(self):
        info = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values=self._sysctl_m4_24(),
            list_devices_text=MTL_M4_PRO,
            use_cache=False,
        )
        self.assertTrue(info["is_apple_silicon"])
        self.assertEqual(info["chip_name"], "Apple M4 Pro")
        self.assertEqual(info["gpu_name"], "Apple M4 Pro")
        self.assertNotEqual(info["gpu_name"], "Generic CPU")
        self.assertEqual(info["unified_memory_gb"], 24.0)
        self.assertEqual(info["memory_reserve_gb"], 8.0)
        self.assertAlmostEqual(info["usable_memory_gb"], 16.0, places=1)
        # Device free (~17.7) may tighten usable further — must stay under total.
        self.assertLess(info["usable_memory_gb"], 24.0)
        self.assertTrue(info["metal_hardware"])
        self.assertTrue(info["metal_llama_build"])
        self.assertFalse(info["metal_selected"])  # not launched yet
        self.assertEqual(info["metal_status"], "build_ready")
        self.assertEqual(info["recommended_build"], "Metal")
        self.assertIn("7B–14B", info["recommended_model_class"])
        self.assertEqual(info["recommended_context_label"], "8K–16K")
        self.assertIn(info["recommended_num_ctx"], (8192, 16384))
        self.assertEqual(info["recommended_n_parallel"], 1)
        self.assertEqual(info["recommended_num_gpu"], 99)
        joined = " | ".join(info["summary_lines"])
        self.assertIn("Apple M4 Pro", joined)
        self.assertIn("24 GB unified memory", joined)
        self.assertIn("Metal acceleration available", joined)

    def test_m2_16gb_memory_bands(self):
        info = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values={
                "machdep.cpu.brand_string": "Apple M2",
                "hw.memsize": str(16 * 1024 ** 3),
                "hw.optional.arm64": "1",
            },
            list_devices_text=MTL_M2,
            use_cache=False,
        )
        self.assertEqual(info["chip_name"], "Apple M2")
        self.assertEqual(info["unified_memory_gb"], 16.0)
        self.assertEqual(info["memory_reserve_gb"], 6.5)
        self.assertAlmostEqual(info["usable_memory_gb"], 9.5, places=1)
        self.assertIn("3B–8B", info["recommended_model_class"])
        self.assertEqual(info["recommended_context_label"], "4K–8K")

    def test_m1_max_64gb(self):
        info = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values={
                "machdep.cpu.brand_string": "Apple M1 Max",
                "hw.memsize": str(64 * 1024 ** 3),
                "hw.optional.arm64": "1",
            },
            list_devices_text="MTL0: Apple M1 Max (48000 MiB, 45000 MiB free)\n",
            use_cache=False,
        )
        self.assertEqual(info["chip_name"], "Apple M1 Max")
        self.assertEqual(info["unified_memory_gb"], 64.0)
        self.assertEqual(info["memory_reserve_gb"], 13.0)
        self.assertTrue(info["metal_llama_build"])
        self.assertIn("70B", info["recommended_model_class"])

    def test_arm64_without_metal_in_llama_build(self):
        info = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values=self._sysctl_m4_24(),
            list_devices_text=CPU_ONLY,
            use_cache=False,
        )
        self.assertTrue(info["is_apple_silicon"])
        self.assertTrue(info["metal_hardware"])
        self.assertFalse(info["metal_llama_build"])
        self.assertEqual(info["metal_status"], "hardware_only")
        self.assertNotEqual(info["recommended_build"], "Metal")
        self.assertNotIn("Metal acceleration available", " ".join(info["summary_lines"]))
        self.assertIn("not confirmed", info["metal_label"])
        # Hardware-only must not recommend full GPU offload.
        self.assertEqual(info["recommended_num_gpu"], 0)

    def test_never_generic_cpu_on_apple_silicon(self):
        info = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values={
                "machdep.cpu.brand_string": "",
                "hw.memsize": str(24 * 1024 ** 3),
                "hw.optional.arm64": "1",
            },
            list_devices_text=CPU_ONLY,
            use_cache=False,
            probe_llama=True,
        )
        self.assertNotEqual(info["gpu_name"], "Generic CPU")
        self.assertTrue(info["is_apple_silicon"])

    def test_metal_selected_only_after_runtime_flag(self):
        bridge._METAL_RUNTIME_SELECTED = True
        info = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values=self._sysctl_m4_24(),
            list_devices_text=MTL_M4_PRO,
            use_cache=False,
        )
        self.assertTrue(info["metal_selected"])
        self.assertEqual(info["metal_status"], "active")
        self.assertIn("Metal acceleration active", info["metal_label"])

    def test_detect_vram_uses_usable_unified(self):
        hw = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values=self._sysctl_m4_24(),
            list_devices_text=MTL_M4_PRO,
            use_cache=False,
        )
        mem = bridge.detect_vram_gb(hw=hw)
        self.assertEqual(mem["source"], "apple-unified")
        self.assertEqual(mem["memory_model"], "unified")
        self.assertEqual(mem["total_gb"], 24.0)
        self.assertEqual(mem["reserve_gb"], 8.0)
        self.assertGreater(mem["gb"], 0)
        self.assertLess(mem["gb"], 24.0)

    def test_auto_tune_unified_conservative_ctx(self):
        # ~8.5 GB Q4 14B-class file size vs ~16 GB usable
        with mock.patch.object(bridge, "inspect_model", return_value={
            "path": "/models/Qwen2.5-Coder-14B-Q4_K_M.gguf",
            "name": "Qwen2.5-Coder-14B-Q4_K_M.gguf",
            "size_gb": 8.5,
            "is_moe": False,
            "block_count": 48,
            "head_count_kv": 8,
            "key_length": 128,
            "value_length": 128,
            "architecture": "qwen2",
            "metadata_source": "gguf",
            "expert_count": 0,
            "expert_used_count": 0,
            "context_length": 32768,
            "has_mtp": False,
            "quant": "Q4_K_M",
        }), mock.patch.object(bridge, "_kv_per_1k_mb", return_value=40.0):
            sug = bridge.auto_tune(
                "/models/Qwen2.5-Coder-14B-Q4_K_M.gguf",
                16.0,
                memory_model="unified",
            )
        self.assertLessEqual(int(sug["num_ctx"]), 16384)
        self.assertGreaterEqual(int(sug["num_ctx"]), 8192)
        self.assertEqual(sug["num_gpu"], 99)
        self.assertEqual(sug["n_parallel"], 1)
        self.assertIn("Unified memory", sug["notes"])

    def test_windows_path_unchanged_generic_without_gpu(self):
        info = bridge.detect_hardware_specs(
            platform_name="win32",
            machine="AMD64",
            run=lambda *a, **k: mock.Mock(returncode=1, stdout="", stderr=""),
            use_cache=False,
            probe_llama=False,
        )
        self.assertFalse(info["is_apple_silicon"])
        self.assertEqual(info["gpu_name"], "Generic CPU")
        self.assertFalse(info["metal_hardware"])


class ReserveTableTest(unittest.TestCase):
    def test_reserve_bands(self):
        self.assertEqual(bridge._apple_memory_reserve_gb(16), 6.5)
        self.assertEqual(bridge._apple_memory_reserve_gb(24), 8.0)
        self.assertEqual(bridge._apple_memory_reserve_gb(32), 8.5)
        self.assertEqual(bridge._apple_memory_reserve_gb(48), 11.0)
        self.assertEqual(bridge._apple_memory_reserve_gb(64), 13.0)


if __name__ == "__main__":
    unittest.main()
