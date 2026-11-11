"""Safety tests for the real Chromium preflight, not browser integration proof."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("browser_probe", Path(__file__).with_name("browser_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeSafety(unittest.TestCase):
    def test_failed_executable_records_failure_and_cleans_only_its_own_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "unrelated"
            sentinel.write_text("keep")
            run, report = probe.run(Path("/bin/true"), root, timeout=2)
            self.assertEqual(report["result"], "FAIL")
            self.assertEqual(report["browser_exit_code"], 0)
            self.assertEqual(report["cleanup"], "PASS")
            self.assertEqual(report["remaining_processes"], [])
            self.assertFalse((run / "private").exists())
            self.assertTrue((run / "report.json").is_file())
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertEqual(run.stat().st_mode & 0o777, 0o700)

    def test_sandbox_status_accepts_legacy_and_current_chromium_with_both_layers(self):
        legacy = "Namespace Sandbox\tYes\nSeccomp-BPF sandbox\tYes\n"
        current = (
            "Layer 1 Sandbox\tNamespace\nPID namespaces\tYes\n"
            "Network namespaces\tYes\nSeccomp-BPF sandbox\tYes\n"
        )
        for text in [legacy, current, "SUID Sandbox\tYes\nSeccomp-BPF sandbox\tYes\n"]:
            with self.subTest(text=text):
                self.assertTrue(probe.sandbox_enabled(text))
        for text in [
            "", legacy.replace("sandbox\tYes", "sandbox\tNo"),
            current.replace("PID namespaces\tYes", "PID namespaces\tNo"),
            current.replace("Network namespaces\tYes", "Network namespaces\tNo"),
            current.replace("Layer 1 Sandbox\tNamespace", "Layer 1 Sandbox\tNone"),
            "Seccomp-BPF sandbox\tYes\n", "You are adequately sandboxed.",
        ]:
            with self.subTest(text=text):
                self.assertFalse(probe.sandbox_enabled(text))

    def test_launch_uses_private_pipe_and_keeps_chromium_sandbox(self):
        args = probe.launch_args(Path("/private/profile"))
        self.assertIn("--remote-debugging-pipe", args)
        self.assertIn("--headless", args)
        self.assertIn("--user-data-dir=/private/profile", args)
        self.assertFalse(any("port" in arg or "no-sandbox" in arg or "disable-setuid-sandbox" in arg for arg in args))


if __name__ == "__main__":
    unittest.main()
