"""Resource-control tutorial package generators point PYTHONPATH at the real Python SDK sources."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TUTORIALS = Path(__file__).resolve().parents[1]
RESOURCE_CONTROL = TUTORIALS / "advanced" / "resource_control"
GENERATORS = {
    "brokered_device": ["--device", "/dev/null"],
    "brokered_can": ["--interface", "vcan0"],
    "brokered_network": ["--interface", "lo"],
}


def generated_pythonpath(manifest: Path) -> str:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("PYTHONPATH:"):
            return line.split(":", 1)[1].strip().strip('"')
    raise AssertionError(f"no PYTHONPATH in {manifest}")


class ResourceControlPackageTests(unittest.TestCase):
    def run_generator(self, name: str, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        script = RESOURCE_CONTROL / name / "bin" / "make_package.py"
        return subprocess.run(
            [sys.executable, "-B", str(script), "--out", str(out), *GENERATORS[name], *extra],
            capture_output=True, text=True, check=False,
        )

    def test_default_pythonpath_is_the_checkout_python_sdk(self) -> None:
        for name in GENERATORS:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                out = Path(directory) / "package"
                result = self.run_generator(name, out)
                self.assertEqual(result.returncode, 0, result.stderr)
                sdk = Path(generated_pythonpath(out / "package.yaml"))
                self.assertTrue((sdk / "r2kernel").is_dir(), sdk)
                self.assertTrue((out / "bin").is_dir())

    def test_explicit_repo_root_without_sdk_is_refused(self) -> None:
        for name in GENERATORS:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                result = self.run_generator(name, Path(directory) / "package", "--repo-root", directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("sdk/python/r2kernel", result.stderr)
                self.assertFalse((Path(directory) / "package").exists())


if __name__ == "__main__":
    unittest.main()
