"""Combined IO helpers register only the procedures of the contracts a manifest declares."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r2kernel.interface.bindings import bind_analog_io_interface, bind_digital_io_interface, bind_digital_io_interface_typed
from r2kernel.manifest import allowed_runtime_endpoints, load_subsystem_manifest
from r2kernel.spec_paths import _PACKAGE_SPEC_ROOT

DIGITAL_INPUT = ("get_digital_input",)
DIGITAL_OUTPUT = ("set_digital_output", "get_digital_output")


class RecordingSubsystem:
    def __init__(self, declared: tuple[str, ...] | None) -> None:
        self.registered: list[tuple[str, str]] = []
        if declared is not None:
            self.declared_runtime_endpoints = lambda kind: declared if kind == "procedures" else ()

    def register_procedure(self, name, handler, **kwargs) -> None:
        self.registered.append(("plain", name))

    def register_typed_procedure(self, name, handler, **kwargs) -> None:
        self.registered.append(("typed", name))


class Driver:
    def __getattr__(self, name):
        return lambda *args: True


class IoBindingTests(unittest.TestCase):
    def names(self, subsystem: RecordingSubsystem) -> list[str]:
        return [name for _, name in subsystem.registered]

    def test_output_contract_gets_only_output_procedures(self) -> None:
        subsystem = RecordingSubsystem(DIGITAL_OUTPUT)
        bind_digital_io_interface(subsystem, Driver())
        self.assertEqual(self.names(subsystem), list(DIGITAL_OUTPUT))

    def test_input_contract_gets_only_the_input_procedure(self) -> None:
        subsystem = RecordingSubsystem(DIGITAL_INPUT)
        bind_digital_io_interface_typed(subsystem, Driver())
        self.assertEqual(subsystem.registered, [("typed", "get_digital_input")])

    def test_both_contracts_get_every_procedure(self) -> None:
        subsystem = RecordingSubsystem(DIGITAL_INPUT + DIGITAL_OUTPUT)
        bind_digital_io_interface(subsystem, Driver())
        self.assertEqual(sorted(self.names(subsystem)), sorted(DIGITAL_INPUT + DIGITAL_OUTPUT))

    def test_no_declared_io_contract_is_an_explicit_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "declares none of its procedures"):
            bind_digital_io_interface(RecordingSubsystem(("unrelated",)), Driver())
        with self.assertRaisesRegex(RuntimeError, "bind_analog_io_interface"):
            bind_analog_io_interface(RecordingSubsystem(()), Driver())

    def test_subsystem_without_manifest_checks_keeps_every_procedure(self) -> None:
        subsystem = RecordingSubsystem(None)
        bind_analog_io_interface(subsystem, Driver())
        self.assertEqual(self.names(subsystem), ["get_analog_input", "set_analog_output", "get_analog_output"])

    def test_output_contract_manifest_allows_exactly_the_output_procedures(self) -> None:
        manifest = """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: digital_output_server
  role: component
provides:
  digital_io:
    contract: api.io.digital.output.v1
"""
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"R2KERNEL_SPEC_ROOT": str(_PACKAGE_SPEC_ROOT)}):
            path = Path(tmpdir) / "subsystem.yaml"
            path.write_text(manifest)
            allowed = allowed_runtime_endpoints(load_subsystem_manifest(path))
        subsystem = RecordingSubsystem(tuple(allowed.get("procedures", ())))
        bind_digital_io_interface(subsystem, Driver())
        self.assertEqual(self.names(subsystem), list(DIGITAL_OUTPUT))


if __name__ == "__main__":
    unittest.main()
