"""Resource-control tutorial package generators point PYTHONPATH at the real Python SDK.

That is the SDK sources in a checkout or the installed SDK under a prefix.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TUTORIALS = Path(__file__).resolve().parents[1]
RESOURCE_CONTROL = TUTORIALS / "advanced" / "resource_control"
GENERATORS = {
    "brokered_device": ["--device", "/dev/null"],
    "brokered_can": ["--interface", "vcan0"],
    "brokered_network": ["--interface", "lo"],
}

for source in (TUTORIALS.parent / "sdk/python", TUTORIALS / "support/python"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

import runtime  # noqa: E402
from r2kernel.manifest.loader import load_subsystem_manifest  # noqa: E402


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
                self.assertEqual(load_subsystem_manifest(out / "subsystem.yaml").role, "gateway")

    def test_installed_prefix_uses_the_installed_sdk_site_packages(self) -> None:
        # The documented flow runs the installed generators from `cd <prefix>`
        # with `--repo-root "$PWD"`. The installer publishes the SDK as
        # sdk/python/site-packages, a link into the current managed environment.
        for name in GENERATORS:
            for explicit_root in (True, False):
                with self.subTest(name=name, explicit_root=explicit_root), tempfile.TemporaryDirectory() as directory:
                    prefix = Path(directory).resolve() / "prefix"
                    shutil.copytree(
                        RESOURCE_CONTROL, prefix / "tutorials" / "advanced" / "resource_control",
                        ignore=shutil.ignore_patterns("__pycache__"),
                    )
                    python = prefix / "sdk" / "python"
                    environment_site = python / "environments" / "env-1" / "lib" / "python3" / "site-packages"
                    (environment_site / "r2kernel").mkdir(parents=True)
                    (python / "current").symlink_to(Path("environments") / "env-1")
                    (python / "site-packages").symlink_to(Path("current") / "lib" / "python3" / "site-packages")
                    script = prefix / "tutorials" / "advanced" / "resource_control" / name / "bin" / "make_package.py"
                    out = Path(directory) / "package"
                    root_arguments = ["--repo-root", str(prefix)] if explicit_root else []
                    result = subprocess.run(
                        [sys.executable, "-B", str(script), "--out", str(out), *GENERATORS[name], *root_arguments],
                        cwd=prefix, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(generated_pythonpath(out / "package.yaml"), str(python / "site-packages"))

    def test_explicit_repo_root_without_sdk_is_refused(self) -> None:
        for name in GENERATORS:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                result = self.run_generator(name, Path(directory) / "package", "--repo-root", directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("sdk/python/r2kernel", result.stderr)
                self.assertFalse((Path(directory) / "package").exists())


class ManagedTutorialStartTests(unittest.TestCase):
    def test_managed_component_start_needs_no_direct_network_probe(self) -> None:
        for runtime_variable in ("R2KERNEL_PACKAGE_RUNTIME_DIR", "R2KERNEL_PACKAGE_INSTANCE_DIR"):
            with self.subTest(variable=runtime_variable), patch.dict(
                "os.environ", {runtime_variable: "/tmp/r2kernel-test-managed-start"}, clear=True
            ), patch.object(runtime.socket, "socket", side_effect=AssertionError("direct socket")), patch.object(
                runtime.urllib.request, "urlopen", side_effect=AssertionError("direct HTTP")
            ):
                runtime.ensure_control_running()
                runtime.wait_for_control_ready()


if __name__ == "__main__":
    unittest.main()
