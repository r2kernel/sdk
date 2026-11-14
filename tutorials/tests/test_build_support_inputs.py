"""Behavioral contracts for Cargo fingerprint delegation and CMake package selection."""
from __future__ import annotations

import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TUTORIALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TUTORIALS / "support"))
import build_support  # noqa: E402


class BuildSupportInputTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_missing_core_source_names_the_explicit_option(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--core-source"):
            build_support.ensure_source_rust_library(None, profile="release", context="test build")
        with self.assertRaisesRegex(SystemExit, f"Checked: {self.root}"):
            build_support.ensure_source_rust_library(self.root, profile="release", context="test build")

    def test_cargo_output_follows_cargo_target_dir(self) -> None:
        core = self.root / "core"
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": ""}):
            self.assertEqual(build_support.cargo_target_dir(core), core / "target")
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": "relative/cargo"}):
            self.assertEqual(build_support.cargo_target_dir(core), Path("relative/cargo").resolve())

    def prepare_core(self, profile: str) -> tuple[Path, Path]:
        core = self.root / "core"
        core.mkdir()
        (core / "Cargo.toml").write_text("[package]\n")
        built = self.root / "cargo" / profile / build_support.rust_library_filename()
        built.parent.mkdir(parents=True)
        built.write_bytes(b"previous library")
        return core, built


    def test_cargo_failure_is_not_hidden_by_existing_library(self) -> None:
        core, _ = self.prepare_core("debug")
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": str(self.root / "cargo")}), \
                mock.patch.object(build_support.shutil, "which", return_value="cargo"), \
                mock.patch.object(build_support.subprocess, "run", side_effect=subprocess.CalledProcessError(7, "cargo")):
            with self.assertRaises(subprocess.CalledProcessError):
                build_support.ensure_source_rust_library(core, profile="debug", context="test")

    def test_missing_cargo_is_an_error_even_with_previous_library(self) -> None:
        core, _ = self.prepare_core("release")
        with mock.patch.object(build_support.shutil, "which", return_value=None):
            with self.assertRaisesRegex(SystemExit, "cargo"):
                build_support.ensure_source_rust_library(core, profile="release", context="test")

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is required for the real fingerprint regression")
    def test_cargo_rebuilds_for_build_script_environment_input(self) -> None:
        core = self.root / "core"
        (core / "src").mkdir(parents=True)
        (core / "Cargo.toml").write_text(
            '[package]\nname = "r2kernel_comm"\nversion = "0.0.0"\nedition = "2021"\n'
            '[lib]\ncrate-type = ["cdylib"]\n'
        )
        (core / "build.rs").write_text(
            'fn main() {\n'
            ' println!("cargo:rerun-if-env-changed=R2KERNEL_TEST_FINGERPRINT");\n'
            ' println!("cargo:rustc-env=CORE_VALUE={}", std::env::var("R2KERNEL_TEST_FINGERPRINT").unwrap());\n'
            '}\n'
        )
        (core / "src/lib.rs").write_text(
            '#[no_mangle]\npub extern "C" fn fingerprint_value() -> u32 {\n'
            ' env!("CORE_VALUE").parse().unwrap()\n}\n'
        )
        # The build uses --locked, which current Cargo refuses without a lock
        # file; the real core source has its Cargo.lock.
        subprocess.run(["cargo", "generate-lockfile", "--offline"], cwd=core, check=True,
                       capture_output=True, text=True)
        for value in ("1", "2"):
            with mock.patch.dict(os.environ, {
                "CARGO_TARGET_DIR": str(self.root / "cargo"),
                "R2KERNEL_TEST_FINGERPRINT": value,
            }):
                library = build_support.ensure_source_rust_library(core, profile="debug", context="fingerprint regression")
            # Probe in a fresh process: no loader cache or live DLL blocks cleanup.
            result = subprocess.run([
                sys.executable, "-c",
                "import ctypes,sys; lib=ctypes.CDLL(sys.argv[1]); "
                "lib.fingerprint_value.restype=ctypes.c_uint32; print(lib.fingerprint_value())",
                str(library),
            ], check=True, capture_output=True, text=True)
            self.assertEqual(int(result.stdout), int(value))

    def test_installed_mode_uses_package_without_core_sources(self) -> None:
        package = self.root / "sdk/cpp/lib/cmake/r2kernel_cpp"
        package.mkdir(parents=True)
        (package / "r2kernel_cppConfig.cmake").write_text("# CMake package discovery marker\n")
        with mock.patch.dict(os.environ, {"R2KERNEL_PREFIX": str(self.root), "R2KERNEL_FORCE_SOURCE_SDK": ""}):
            self.assertEqual(build_support.resolve_cpp_sdk_package(
                TUTORIALS.parent, sdk="installed", core_source=None, mode="debug",
                sdk_build_dir=None, cmake_args=[], context="test",
            ), package)

    def test_forced_source_does_not_select_installed_package(self) -> None:
        with mock.patch.dict(os.environ, {"R2KERNEL_FORCE_SOURCE_SDK": "1"}):
            self.assertIsNone(build_support.resolve_installed_cpp_sdk())
            with self.assertRaisesRegex(SystemExit, "no installed C.* package"):
                build_support.resolve_cpp_sdk_package(
                    TUTORIALS.parent, sdk="installed", core_source=None, mode="release",
                    sdk_build_dir=None, cmake_args=[], context="test",
                )

    def test_cli_groups_match_cmake_fixture_declarations(self) -> None:
        cmake = (TUTORIALS / "CMakeLists.txt").read_text(encoding="utf-8")
        declared = {
            group: tuple(binaries.split())
            for group, binaries in re.findall(r"^r2kernel_add_transport_fixture\((\w+) ([\w ]+)\)$", cmake, re.M)
        }
        self.assertEqual(declared, {group: binaries for group, (binaries, _) in build_support.FIXTURE_GROUPS.items()})
        for group in declared:
            self.assertTrue((TUTORIALS / "fixtures/transport" / group / "cpp").is_dir(), group)

    def test_cli_builds_the_selected_group_into_its_default_directory(self) -> None:
        fixtures = TUTORIALS / "fixtures"
        with mock.patch.object(build_support, "build_cpp_fixtures") as build:
            self.assertEqual(build_support.main(["procedure", "--target", "client"]), 0)
        arguments = build.call_args.kwargs
        self.assertEqual(arguments["fixture_root"], fixtures)
        self.assertEqual((arguments["fixture_group"], arguments["targets"], arguments["sdk"]),
                         ("procedure", ["client"], "auto"))
        with mock.patch.object(build_support, "build_cpp_fixtures") as build:
            build_support.main(["control"])
        self.assertEqual((build.call_args.kwargs["targets"], build.call_args.kwargs["sdk"]),
                         (["gateway", "controller"], "source"))
        with self.assertRaises(SystemExit):
            build_support.main(["operation", "--target", "gateway"])
        with self.assertRaises(SystemExit):
            build_support.main(["event", "--fixture-root", str(self.root)])


if __name__ == "__main__":
    unittest.main()
