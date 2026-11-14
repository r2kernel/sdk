"""Tutorial packages started from a copy of their package directory.

A built package, an installed package or an approved-package snapshot holds only
the package directory, so the shared tutorial modules must come from a named or
installed tutorials tree instead of the package's ancestors.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir())
PACKAGES = tuple(sorted(path.parent.name for path in (ROOT / "subsystems").glob("*/run.py")))
MISSING_ROOT = "could not locate R2Kernel tutorial runtime"

# Packages that read tutorial data (fixture, mount) while they are constructed.
CONSTRUCTED = {
    "component.localizer": ("localizer.py", "LocalizerSubsystem"),
    "gateway.device_gateway": ("device_gateway.py", "DeviceGatewaySubsystem"),
}

# Builds the subsystem object without the SDK runtime, which needs a running Control.
CONSTRUCT = """
import importlib.util
import sys
from unittest.mock import patch

source, class_name = sys.argv[1:3]
spec = importlib.util.spec_from_file_location("copied_package", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
subsystem_class = getattr(module, class_name)
with patch.object(subsystem_class.__mro__[1], "__init__", lambda self, **kwargs: None):
    subsystem_class()
print("constructed")
"""


def _sdk_search_path() -> str | None:
    """The directory that holds an SDK importable with its runtime dependencies."""
    try:
        importlib.import_module("r2kernel.wrapper.subsystem")
    except ImportError:
        return None
    spec = importlib.util.find_spec("r2kernel")
    if spec is None or not spec.submodule_search_locations:
        return None
    return str(Path(next(iter(spec.submodule_search_locations))).parent)


class PackageOutsideTutorialTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)

    def copy_package(self, package_name: str) -> Path:
        package = self.work / package_name
        shutil.copytree(ROOT / "subsystems" / package_name, package, ignore=shutil.ignore_patterns("__pycache__"))
        return package

    def run_python(
        self, package: Path, arguments: list[str], sdk: str | None = None, **environment: str
    ) -> subprocess.CompletedProcess[str]:
        inherited = {"R2KERNEL_TUTORIAL_ROOT", "R2KERNEL_HOME", "PYTHONPATH"}
        env = {name: value for name, value in os.environ.items() if name not in inherited}
        env.update(PYTHONDONTWRITEBYTECODE="1", **environment)
        if sdk is not None:
            env["PYTHONPATH"] = sdk
        return subprocess.run(
            [sys.executable, *arguments], cwd=package, env=env,
            capture_output=True, text=True, timeout=60, check=False,
        )

    def start(self, package: Path, sdk: str | None = None, **environment: str) -> subprocess.CompletedProcess[str]:
        return self.run_python(package, ["run.py", "--help"], sdk, **environment)

    def test_every_tutorial_package_is_covered(self) -> None:
        packages = {path.parent.name for path in (ROOT / "subsystems").glob("*/package.yaml")}
        self.assertTrue(packages)
        self.assertEqual(set(PACKAGES), packages)
        self.assertLessEqual(set(CONSTRUCTED), packages)

    def test_a_copied_package_loads_the_shared_modules_from_the_named_tutorials_tree(self) -> None:
        sdk = _sdk_search_path()
        if sdk is None:
            self.skipTest("the R2Kernel Python SDK and its dependencies are not importable")
        for package_name in PACKAGES:
            with self.subTest(package=package_name):
                result = self.start(self.copy_package(package_name), sdk, R2KERNEL_TUTORIAL_ROOT=str(ROOT))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_a_copied_package_reads_its_tutorial_data_from_the_named_tutorials_tree(self) -> None:
        sdk = _sdk_search_path()
        if sdk is None:
            self.skipTest("the R2Kernel Python SDK and its dependencies are not importable")
        for package_name, (source, class_name) in sorted(CONSTRUCTED.items()):
            with self.subTest(package=package_name):
                package = self.copy_package(package_name)
                result = self.run_python(
                    package, ["-c", CONSTRUCT, str(package / source), class_name], sdk,
                    R2KERNEL_TUTORIAL_ROOT=str(ROOT),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("constructed", result.stdout)

    def test_a_copied_package_without_a_tutorials_tree_names_the_missing_runtime(self) -> None:
        # The lookup runs before any SDK import, so this needs no SDK.
        if (Path("/opt/r2kernel/tutorials") / "subsystems").is_dir():
            self.skipTest("this host has an installed /opt/r2kernel/tutorials, which the packages would use")
        for package_name in PACKAGES:
            with self.subTest(package=package_name):
                result = self.start(self.copy_package(package_name), R2KERNEL_HOME=str(self.work / "no-installation"))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(MISSING_ROOT, result.stderr)
                self.assertNotIn("StopIteration", result.stderr)


if __name__ == "__main__":
    unittest.main()
