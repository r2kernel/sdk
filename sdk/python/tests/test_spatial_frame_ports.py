"""frames.policy spatial_ports (api.locomotion.odometry.v2) and the reserved-ID rule for package spec roots.

The manifest refusals and the binding cases are shared with the C++ SDK
(sdk/testdata/parity); these tests cover the loader, the provider publish
check, the codec, the v1/v2 transition and the start-time spec-root check.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from r2kernel.contract.dictionary import package_spec_root_violations
from r2kernel.contract import (
    ContractSpecError,
    load_api_contract_spec,
    load_component_spec,
    validate_declared_contracts,
    validate_package_spec_roots,
)
from r2kernel.interface import InterfaceSpecError, load_interface_spec, spatial_frame_ports
from r2kernel.manifest import ManifestError, load_subsystem_manifest, provided_frame_ports
from r2kernel.spec_paths import _REPO_SPEC_ROOT
from r2kernel.wrapper.subsystem import ComponentSubsystem

ODOMETRY_V1 = "msg.locomotion.mobile_base.v1/Odometry"
ODOMETRY_V2 = "msg.locomotion.mobile_base.v2/Odometry"
STANDARD_API_PATH = Path("apis/api/locomotion/odometry/v1.yaml")

MOBILE_BASE_V2_MANIFEST = """\
kind: r2kernel.subsystem
version: 1
subsystem: {name: component.mobile, role: component}
spatial:
  participation: required
  ports:
    reference: {direction: export, semantic: odometry_reference, frame: odom}
    body: {direction: export, semantic: mobile_body, frame: cleaner_base}
provides:
  mobile_base: {contract: component.mobile_base.odometry.v2}
"""


def _write_yaml(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _api_document(api_id: str, *, frames: dict, events: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "id": api_id,
        "kind": "api_contract",
        "events": events if events is not None else [
            {"topic": "pose", "type": "msg.acme.pose.v1/StampedPose", "direction": "publish"},
        ],
        "frames": frames,
    }


class _Probe(ComponentSubsystem):
    def on_initialize(self) -> bool:
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        return True

    def on_reset(self) -> bool:
        return True


class _RecordingRuntime:
    def __init__(self, **_kwargs) -> None:
        self.published: list[tuple[str, object]] = []

    def publish_event(self, *, topic: str, value, timestamp=None) -> bool:
        self.published.append((topic, value))
        return True


class SpatialPortsLoaderTests(unittest.TestCase):
    def test_v2_contracts_load_with_their_frame_ports(self) -> None:
        api = load_api_contract_spec("api.locomotion.odometry.v2")
        self.assertEqual(api.events[0].type, ODOMETRY_V2)
        self.assertEqual(spatial_frame_ports(api), {"frame_id": "reference", "child_frame_id": "body"})
        self.assertEqual(api.frames["dynamic_frames"], "owning_message_only")
        self.assertEqual(spatial_frame_ports(load_api_contract_spec("api.locomotion.odometry.v1")), {})
        component = load_component_spec("component.mobile_base.odometry.v2")
        self.assertIn("api.locomotion.odometry.v2", component.conformance_apis)
        self.assertEqual({name: port.direction for name, port in component.spatial.ports.items()},
                         {"reference": "export", "body": "export"})

    def test_invalid_spatial_ports_declarations_are_refused(self) -> None:
        message_set = {
            "version": 1, "id": "msg.acme.pose.v1", "kind": "message_set",
            "messages": {"StampedPose": {"fields": [
                {"name": "frame_id", "type": "string"},
                {"name": "label", "type": "string", "optional": True},
                {"name": "count", "type": "uint32"},
            ]}},
        }
        cases = {
            "no ports": ({"policy": "spatial_ports"}, None, "requires a non-empty frames.ports mapping"),
            "empty ports": ({"policy": "spatial_ports", "ports": {}}, None, "requires a non-empty frames.ports"),
            "port is not a name": ({"policy": "spatial_ports", "ports": {"frame_id": ["body"]}}, None,
                                   "must map message field names to spatial port names"),
            "missing field": ({"policy": "spatial_ports", "ports": {"child_frame_id": "body"}}, None,
                              "field 'child_frame_id' must be a required string field of msg.acme.pose.v1/StampedPose"),
            "optional field": ({"policy": "spatial_ports", "ports": {"label": "body"}}, None,
                               "field 'label' must be a required string field"),
            "not a string": ({"policy": "spatial_ports", "ports": {"count": "body"}}, None,
                             "field 'count' must be a required string field"),
            "nothing published": ({"policy": "spatial_ports", "ports": {"frame_id": "body"}},
                                  [{"topic": "pose", "type": "msg.acme.pose.v1/StampedPose", "direction": "subscribe"}],
                                  "requires a published event"),
            "unknown message": ({"policy": "spatial_ports", "ports": {"frame_id": "body"}},
                                [{"topic": "pose", "type": "msg.acme.unknown.v1/Pose", "direction": "publish"}],
                                "message msg.acme.unknown.v1/Pose of event 'pose' is not found"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_yaml(root / "messages/msg/acme/pose/v1.yaml", message_set)
            with patch.dict(os.environ, {"R2KERNEL_SPEC_PATH": str(root)}):
                for name, (frames, events, error) in cases.items():
                    with self.subTest(name):
                        path = _write_yaml(root / "apis/api/acme/pose/v1.yaml",
                                           _api_document("api.acme.pose.v1", frames=frames, events=events))
                        with self.assertRaisesRegex(InterfaceSpecError, error):
                            load_interface_spec(path)
                accepted = load_interface_spec(_write_yaml(
                    root / "apis/api/acme/pose/v1.yaml",
                    _api_document("api.acme.pose.v1", frames={"policy": "spatial_ports", "ports": {"frame_id": "body"}})))
                self.assertEqual(spatial_frame_ports(accepted), {"frame_id": "body"})
                # An unversioned message reference resolves through the API's imports.
                imported = _api_document(
                    "api.acme.pose.v1", frames={"policy": "spatial_ports", "ports": {"frame_id": "body"}},
                    events=[{"topic": "pose", "type": "msg.acme.pose/StampedPose", "direction": "publish"}])
                imported["imports"] = {"msg.acme.pose": "v1"}
                imported_spec = load_interface_spec(_write_yaml(root / "apis/api/acme/pose/v1.yaml", imported))
                self.assertEqual(imported_spec.events[0].type, "msg.acme.pose.v1/StampedPose")
                self.assertEqual(spatial_frame_ports(imported_spec), {"frame_id": "body"})
                # Other policies remain metadata, as before.
                metadata = load_interface_spec(_write_yaml(
                    root / "apis/api/acme/pose/v1.yaml",
                    _api_document("api.acme.pose.v1", frames={"policy": "vendor_defined", "ports": 7})))
                self.assertEqual(metadata.frames, {"policy": "vendor_defined", "ports": 7})
                self.assertEqual(spatial_frame_ports(metadata), {})

    def test_provided_frame_ports_come_from_the_provider_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "subsystem.yaml"
            manifest.write_text(MOBILE_BASE_V2_MANIFEST, encoding="utf-8")
            checks = provided_frame_ports(load_subsystem_manifest(manifest))
        self.assertEqual(list(checks), ["odometry"])
        self.assertEqual([(check.field, check.port, check.frame, check.contract) for check in checks["odometry"]], [
            ("frame_id", "reference", "odom", "api.locomotion.odometry.v2"),
            ("child_frame_id", "body", "cleaner_base", "api.locomotion.odometry.v2"),
        ])


class ProviderPublishCheckTests(unittest.TestCase):
    def _provider(self, root: Path) -> _Probe:
        manifest = root / "subsystem.yaml"
        manifest.write_text(MOBILE_BASE_V2_MANIFEST, encoding="utf-8")
        with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", _RecordingRuntime):
            provider = _Probe(name="component.mobile", user_data_path=str(root / "userdata"), manifest_path=manifest)
        provider._logger = Mock()
        return provider

    def test_matching_frames_are_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = self._provider(Path(temporary))
            sample = {"frame_id": "odom", "child_frame_id": "cleaner_base", "pose": {}}
            self.assertTrue(provider.publish_event(topic="odometry", value=sample))
            self.assertEqual(provider._runtime.published, [("odometry", sample)])
            # Topics without a spatial_ports API are not inspected.
            self.assertTrue(provider.publish_event(topic="mobile_base_state", value={"frame_id": "anything"}))
            provider.logger.error.assert_not_called()

    def test_mismatched_frames_are_refused_and_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = self._provider(Path(temporary))
            for field, value in (("child_frame_id", "base_footprint"), ("frame_id", "map")):
                with self.subTest(field):
                    provider.logger.error.reset_mock()
                    sample = {"frame_id": "odom", "child_frame_id": "cleaner_base", "pose": {}, field: value}
                    self.assertFalse(provider.publish_event(topic="odometry", value=sample))
                    self.assertEqual(provider._runtime.published, [])
                    message = provider.logger.error.call_args.args[1]
                    self.assertTrue(message.startswith(f"frame_mismatch: odometry.{field} is '{value}'"), message)
                    self.assertIn("the sample was not published", message)

    def test_a_missing_frame_field_is_left_to_the_codec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = self._provider(Path(temporary))
            sample = {"child_frame_id": "cleaner_base", "pose": {}}
            self.assertTrue(provider.publish_event(topic="odometry", value=sample))
            self.assertEqual(provider._runtime.published, [("odometry", sample)])

    def test_codec_refuses_a_v2_sample_without_its_frames(self) -> None:
        try:
            from r2kernel.interface import PreparedSchemaContext

            context = PreparedSchemaContext([_REPO_SPEC_ROOT])
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            self.skipTest(f"r2kernel-comm library unavailable: {exc}")
        codec = context.prepare(ODOMETRY_V2)
        pose = {"position": {"x": 0.0, "y": 0.0, "z": 0.0}, "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}
        wire = codec.encode({"frame_id": "odom", "child_frame_id": "cleaner_base", "pose": pose})
        self.assertEqual(codec.decode(wire)["child_frame_id"], "cleaner_base")
        for missing in ("frame_id", "child_frame_id"):
            sample = {"frame_id": "odom", "child_frame_id": "cleaner_base", "pose": pose}
            del sample[missing]
            with self.subTest(missing), self.assertRaisesRegex(ValueError, f"Missing required field {ODOMETRY_V2}.{missing}"):
                codec.encode(sample)
        # v1 keeps its optional frames.
        self.assertNotIn("frame_id", context.prepare(ODOMETRY_V1).decode(context.prepare(ODOMETRY_V1).encode({"pose": pose})))


class OdometryTransitionTests(unittest.TestCase):
    """Endpoint names form one flat set per subsystem: v1 and v2 both publish `odometry`."""

    @staticmethod
    def _runtime(odometry_type: str) -> SimpleNamespace:
        return SimpleNamespace(
            get_publish_info=lambda: [("odometry", odometry_type)],
            get_subscribe_info=lambda: [],
            get_control_info=lambda: [],
            get_procedure_info=lambda: [],
            get_operation_info=lambda: [],
            get_control_contract_info=lambda: [],
            get_procedure_contract_info=lambda: [],
            get_operation_contract_info=lambda: [],
        )

    def test_one_subsystem_cannot_provide_v1_and_v2(self) -> None:
        owner = SimpleNamespace(declared_apis=("api.locomotion.odometry.v1", "api.locomotion.odometry.v2"),
                                declared_components=())
        for registered, refused in ((ODOMETRY_V2, ODOMETRY_V1), (ODOMETRY_V1, ODOMETRY_V2)):
            with self.subTest(registered=registered):
                report = validate_declared_contracts(owner, self._runtime(registered))
                self.assertFalse(report.ok)
                self.assertEqual([issue.code for issue in report.issues], ["event_publish_type_mismatch"])
                self.assertIn(f"must use type '{refused}', found '{registered}'", report.format_errors())

    def test_separate_provider_instances_can_serve_v1_and_v2(self) -> None:
        for api_id, odometry_type in (("api.locomotion.odometry.v1", ODOMETRY_V1),
                                      ("api.locomotion.odometry.v2", ODOMETRY_V2)):
            with self.subTest(api_id):
                owner = SimpleNamespace(declared_apis=(api_id,), declared_components=())
                self.assertTrue(validate_declared_contracts(owner, self._runtime(odometry_type)).ok)


class ReservedStandardIdTests(unittest.TestCase):
    def _package(self, root: Path, files: dict[Path, str | None]) -> Path:
        package = root / "package"
        _write_yaml(package / "package.yaml", {"schema_version": 1, "name": "demo.mobile",
                                               "entrypoint": {"command": "bin/run"},
                                               "contracts": {"spec_roots": ["spec"]}})
        (package / "subsystem.yaml").write_text(MOBILE_BASE_V2_MANIFEST, encoding="utf-8")
        for relative, text in files.items():
            destination = package / "spec" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                shutil.copyfile(_REPO_SPEC_ROOT / relative, destination)
            else:
                destination.write_text(text, encoding="utf-8")
        return package

    def test_identical_copies_of_standard_ids_stay_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._package(Path(temporary), {
                STANDARD_API_PATH: None,
                Path("messages/msg/locomotion/mobile_base/v2.yaml"): None,
                Path("apis/api/acme/odometry/v1.yaml"): "version: 1\nid: api.acme.odometry.v1\nkind: api_contract\n",
            })
            self.assertEqual(load_subsystem_manifest(package / "subsystem.yaml").name, "component.mobile")
            validate_package_spec_roots([package / "spec"], dictionary=_REPO_SPEC_ROOT)

    def test_a_differing_definition_and_a_new_id_under_a_standard_prefix_are_refused(self) -> None:
        cases = {
            "differing standard API": (STANDARD_API_PATH, "api.locomotion.odometry.v1 differs from the installed standard definition"),
            "differing standard component": (Path("components/component/mobile_base/odometry/v2.yaml"),
                                             "component.mobile_base.odometry.v2 differs from the installed standard definition"),
            "new message set": (Path("messages/msg/locomotion/mobile_base/v3.yaml"),
                                "msg.locomotion.mobile_base.v3 uses the standard prefix msg.locomotion"),
            "new component": (Path("components/component/mobile_base/tracked/v1.yaml"),
                              "component.mobile_base.tracked.v1 uses the standard prefix component.mobile_base"),
        }
        for name, (relative, error) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                package = self._package(Path(temporary), {relative: "version: 1\nid: changed\n"})
                with self.assertRaisesRegex(ManifestError, error):
                    load_subsystem_manifest(package / "subsystem.yaml")
                with self.assertRaisesRegex(ContractSpecError, error):
                    validate_package_spec_roots([package / "spec"], dictionary=_REPO_SPEC_ROOT)

    def test_the_launcher_injected_roots_are_checked_when_the_subsystem_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package(root, {STANDARD_API_PATH: "version: 1\nid: api.locomotion.odometry.v1\n"})
            manifest = root / "subsystem.yaml"
            manifest.write_text(MOBILE_BASE_V2_MANIFEST, encoding="utf-8")
            with patch.dict(os.environ, {"R2KERNEL_CONTRACT_SPEC_ROOTS": str(package / "spec")}), \
                    patch("r2kernel.wrapper.subsystem.SubsystemRuntime", _RecordingRuntime):
                with self.assertRaisesRegex(RuntimeError, "api.locomotion.odometry.v1 differs from the installed standard"):
                    _Probe(name="component.mobile", user_data_path=str(root / "userdata"), manifest_path=manifest)
                (package / "spec" / STANDARD_API_PATH).unlink()
                provider = _Probe(name="component.mobile", user_data_path=str(root / "userdata"), manifest_path=manifest)
                self.assertEqual(provider.name, "component.mobile")


class SpecTreeAliasTests(unittest.TestCase):
    """Cross-audit SDK-104-01: the guard refuses what the core refuses instead of skipping it."""

    def _aliased(self, root: Path, text: str | None) -> Path:
        spec = root / "spec"
        target = spec / "apis/api/aaa/odometry/v2.yaml"
        target.parent.mkdir(parents=True)
        if text is None:
            shutil.copyfile(_REPO_SPEC_ROOT / "apis/api/locomotion/odometry/v2.yaml", target)
        else:
            target.write_text(text, encoding="utf-8")
        (spec / "apis/api/locomotion").symlink_to("aaa", target_is_directory=True)
        return spec

    def test_the_four_audit_controls(self) -> None:
        standard = (_REPO_SPEC_ROOT / "apis/api/locomotion/odometry/v2.yaml").read_text(encoding="utf-8")
        changed = standard.replace("policy: spatial_ports", "policy: vendor_defined")
        alias_error = "apis/api/locomotion is the same directory as apis/api/aaa; spec directories must not alias each other"
        with tempfile.TemporaryDirectory() as temporary:
            spec = Path(temporary) / "spec"
            (spec / "apis/api/locomotion/odometry").mkdir(parents=True)
            shutil.copyfile(_REPO_SPEC_ROOT / "apis/api/locomotion/odometry/v2.yaml", spec / "apis/api/locomotion/odometry/v2.yaml")
            self.assertEqual(package_spec_root_violations(spec, _REPO_SPEC_ROOT), [])  # normal identical copy
            (spec / "apis/api/locomotion/odometry/v2.yaml").write_text(changed, encoding="utf-8")
            violations = package_spec_root_violations(spec, _REPO_SPEC_ROOT)  # direct modification
            self.assertEqual(len(violations), 1)
            self.assertIn("api.locomotion.odometry.v2 differs from the installed standard definition", violations[0])
        for name, text in (("alias identical", None), ("alias modified", changed)):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                spec = self._aliased(Path(temporary), text)
                self.assertIn(f"Package spec root {spec}: {alias_error}", package_spec_root_violations(spec, _REPO_SPEC_ROOT))
                with self.assertRaisesRegex(ContractSpecError, "must not alias each other"):
                    validate_package_spec_roots([spec], dictionary=_REPO_SPEC_ROOT)

    def test_unreadable_entries_and_misplaced_documents_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spec = Path(temporary) / "spec"
            (spec / "apis/api").mkdir(parents=True)
            (spec / "apis/api/broken").symlink_to("missing", target_is_directory=True)
            (spec / "apis/api/stray.yaml").write_text("version: 1\n", encoding="utf-8")
            violations = package_spec_root_violations(spec, _REPO_SPEC_ROOT)
            self.assertEqual(violations, [
                f"Package spec root {spec}: apis/api/broken cannot be read: No such file or directory",
                f"Package spec root {spec}: apis/api/stray.yaml is not at <tree>/<kind>/<segment>/.../<version>.yaml",
            ])

    def test_a_link_out_of_the_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "outside/odometry").mkdir(parents=True)
            spec = root / "spec"
            (spec / "apis/api").mkdir(parents=True)
            (spec / "apis/api/acme").symlink_to(root / "outside", target_is_directory=True)
            self.assertEqual(package_spec_root_violations(spec, _REPO_SPEC_ROOT),
                             [f"Package spec root {spec}: apis/api/acme leads outside the package spec root"])


class SpecDocumentIdTests(unittest.TestCase):
    """Cross-audit SDK-108-01: a message or API document must declare the ID of its path."""

    def _spec(self, root: Path, files: dict[str, str]) -> Path:
        spec = root / "spec"
        for relative, text in files.items():
            (spec / relative).parent.mkdir(parents=True, exist_ok=True)
            (spec / relative).write_text(text, encoding="utf-8")
        return spec

    def test_a_standard_id_at_a_vendor_path_is_refused_and_the_corrected_id_accepted(self) -> None:
        standard = (_REPO_SPEC_ROOT / "apis/api/locomotion/odometry/v2.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            spec = self._spec(Path(temporary), {"apis/api/aaa/odometry/v2.yaml": standard})
            self.assertEqual(package_spec_root_violations(spec, _REPO_SPEC_ROOT), [
                f"Package spec root {spec}: apis/api/aaa/odometry/v2.yaml declares id api.locomotion.odometry.v2; "
                "a document at this path must declare api.aaa.odometry.v2",
            ])
        corrected = standard.replace("id: api.locomotion.odometry.v2", "id: api.aaa.odometry.v2")
        with tempfile.TemporaryDirectory() as temporary:
            spec = self._spec(Path(temporary), {"apis/api/aaa/odometry/v2.yaml": corrected})
            self.assertEqual(package_spec_root_violations(spec, _REPO_SPEC_ROOT), [])

    def test_missing_ids_mismatched_messages_and_invalid_yaml_are_refused(self) -> None:
        cases = {
            "messages/msg/acme/pose/v1.yaml": ("version: 1\nid: msg.acme.other.v1\nkind: message_set\nmessages: {}\n",
                                               "messages/msg/acme/pose/v1.yaml declares id msg.acme.other.v1; "
                                               "a document at this path must declare msg.acme.pose.v1"),
            "apis/api/acme/pose/v1.yaml": ("version: 1\nkind: api_contract\n", "apis/api/acme/pose/v1.yaml has no id"),
            "apis/api/acme/broken/v1.yaml": ("id: [unclosed\n", "apis/api/acme/broken/v1.yaml is not a valid YAML document"),
        }
        for relative, (text, problem) in cases.items():
            with self.subTest(relative), tempfile.TemporaryDirectory() as temporary:
                spec = self._spec(Path(temporary), {relative: text})
                self.assertEqual(package_spec_root_violations(spec, _REPO_SPEC_ROOT), [f"Package spec root {spec}: {problem}"])


if __name__ == "__main__":
    unittest.main()
