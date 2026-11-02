"""Package-local specs stay local while an unrelated caller parses a deployment."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from r2kernel.core.codec_lifecycle import CodecLifecycle
from r2kernel.manifest import ManifestError, load_deployment_manifest, load_subsystem_manifest
from r2kernel.spec_paths import (
    frozen_spec_sources, package_spec_sources, resolve_spec_path, spec_root_candidates,
)


API_ID = 'api.vendor.private.digital_io.v1'
API_PATH = Path('apis/api/vendor/private/digital_io/v1.yaml')
PRIVATE_REQUEST = 'msg.vendor.private.v1/Request'


def write_yaml(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding='utf-8')


def make_package(root, name, *, role='gateway', endpoint_kind='controls', roots=None, nested=False):
    package = root / name
    manifest_relative = 'config/subsystem.yaml' if nested else 'subsystem.yaml'
    write_yaml(package / 'package.yaml', {
        'schema_version': 1, 'name': name, 'subsystem_manifest': manifest_relative,
        'contracts': {'spec_roots': ['spec'] if roots is None else roots},
    })
    endpoint = {'request': PRIVATE_REQUEST, 'response': 'msg.core.common.primitive.v1/BoolValue',
                'timing_support': 'required'}
    endpoint['topic' if endpoint_kind == 'controls' else 'name'] = 'set_output'
    (package / 'spec').mkdir(parents=True, exist_ok=True)
    if role != 'app':
        write_yaml(package / 'spec' / API_PATH, {
            'version': 1, 'id': API_ID, 'kind': 'api_contract', 'name': 'Private digital I/O',
            endpoint_kind: [endpoint],
        })
        write_yaml(package / 'spec/messages/msg/vendor/private/v1.yaml', {
            'version': 1, 'id': 'msg.vendor.private.v1', 'kind': 'message_set',
            'messages': {'Request': {'fields': [{'name': 'value', 'type': 'uint32'}]}},
        })
    write_yaml(package / manifest_relative, {
        'kind': 'r2kernel.subsystem', 'version': 1,
        'subsystem': {'name': name, 'role': role},
        'provides': {'digital_io': {'contract': API_ID, 'visibility': 'internal',
                                    'endpoints': {'controls': ['set_output']}}} if role == 'gateway' else {},
    })
    return package, package / manifest_relative


class PackageManifestSpecTests(unittest.TestCase):
    def test_unrelated_app_loads_package_graph_without_importing_private_types(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gateway, gateway_manifest = make_package(root, 'gateway.device', nested=True)
            app, app_manifest = make_package(root, 'app.client', role='app', roots=[])
            write_yaml(root / 'deployment.yaml', {
                'kind': 'r2kernel.deployment', 'version': 1,
                'instances': {
                    'gateway.device': {'package': './gateway.device'},
                    'app.client': {'package': './app.client'},
                }, 'bindings': {},
            })
            with patch.dict(os.environ, {'R2KERNEL_CONTRACT_SPEC_ROOTS': str(app / 'spec'),
                                         'R2KERNEL_SPEC_PATH': str(app / 'spec')}):
                before = dict(os.environ)
                deployment = load_deployment_manifest(root / 'deployment.yaml')
                self.assertEqual(deployment.subsystems['gateway.device'].path, gateway_manifest)
                self.assertEqual(load_subsystem_manifest(gateway_manifest).role, 'gateway')
                self.assertEqual(os.environ, before)
                self.assertIsNone(resolve_spec_path(API_PATH))
                app_owner = SimpleNamespace(_subsystem_manifest=load_subsystem_manifest(app_manifest),
                                            declared_apis=(), declared_components=())
                runtime = SimpleNamespace(
                    _owner=app_owner, _rust_message_codec=None,
                    _event=SimpleNamespace(_message_codec=None),
                    _control_dispatch_running=False, _control_target_cache={},
                    _control_contracts={}, _procedure_contracts={}, _operation_contracts={},
                    get_publish_info=lambda: (), get_subscribe_info=lambda: (),
                )
                lifecycle = CodecLifecycle(runtime)
                with lifecycle.initialization():
                    lifecycle.prepare_declarations()
                self.assertNotIn(PRIVATE_REQUEST, lifecycle.metadata())
                self.assertNotIn(API_ID, lifecycle.contract_codecs)

    def test_each_package_inspects_its_own_same_id_spec_and_scope_restores_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good, good_manifest = make_package(root, 'gateway.good')
            bad, bad_manifest = make_package(root, 'gateway.bad', endpoint_kind='procedures')
            with patch.dict(os.environ, {'R2KERNEL_CONTRACT_SPEC_ROOTS': str(good / 'spec'),
                                         'R2KERNEL_SPEC_PATH': str(good / 'spec')}):
                before = spec_root_candidates()
                self.assertEqual(load_subsystem_manifest(good_manifest).role, 'gateway')
                with self.assertRaisesRegex(ManifestError, 'must declare at least one Control'):
                    load_subsystem_manifest(bad_manifest)
                self.assertEqual(spec_root_candidates(), before)
                self.assertEqual(resolve_spec_path(API_PATH), good / 'spec' / API_PATH)
                (bad / 'spec' / API_PATH).unlink()
                with self.assertRaisesRegex(ManifestError, 'Cannot inspect contract'):
                    load_subsystem_manifest(bad_manifest)
            # Packages without declared roots retain the existing global lookup.
            write_yaml(bad / 'package.yaml', {'contracts': {'spec_roots': []}})
            with patch.dict(os.environ, {'R2KERNEL_CONTRACT_SPEC_ROOTS': str(good / 'spec'),
                                         'R2KERNEL_SPEC_PATH': str(good / 'spec')}):
                self.assertEqual(load_subsystem_manifest(bad_manifest).role, 'gateway')

    def test_configured_shared_dictionary_paths_remain_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, manifest = make_package(root, 'gateway.device')
            shared = root / 'shared'
            shared.mkdir()
            # The package declares a root, but its API is deliberately supplied
            # by an independently configured global dictionary instead.
            destination = shared / API_PATH
            destination.parent.mkdir(parents=True)
            (package / 'spec' / API_PATH).rename(destination)
            for configuration in ('R2KERNEL_SPEC_ROOT', 'R2KERNEL_SPEC_PATH'):
                with self.subTest(configuration=configuration), patch.dict(os.environ, {
                        'R2KERNEL_CONTRACT_SPEC_ROOTS': str(package / 'spec'),
                        'R2KERNEL_SPEC_ROOT': '', 'R2KERNEL_SPEC_PATH': '', configuration: str(shared)}):
                    self.assertEqual(load_subsystem_manifest(manifest).role, 'gateway')

    def test_declared_package_roots_require_contained_existing_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, manifest = make_package(root, 'gateway.device')
            outside = root / 'outside'
            outside.mkdir()
            (package / 'escape').symlink_to(outside, target_is_directory=True)
            for invalid in ('', '../outside', str(outside), 'missing', 'package.yaml', 'escape'):
                with self.subTest(root=invalid):
                    write_yaml(package / 'package.yaml', {'contracts': {'spec_roots': [invalid]}})
                    with self.assertRaises(ManifestError):
                        load_subsystem_manifest(manifest)
            for invalid in ('spec', [1], None):
                with self.subTest(shape=invalid):
                    write_yaml(package / 'package.yaml', {'contracts': {'spec_roots': invalid}})
                    with self.assertRaisesRegex(ManifestError, 'list of strings'):
                        load_subsystem_manifest(manifest)

    def test_frozen_package_scope_uses_captured_spec_and_rejects_new_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, manifest = make_package(root, 'gateway.device')
            foreign, foreign_manifest = make_package(root, 'gateway.foreign')
            captured = (package / 'spec' / API_PATH).read_text()
            with package_spec_sources([package / 'spec']):
                roots = spec_root_candidates()
            snapshot = SimpleNamespace(spec_text=lambda relative: captured if str(relative) == str(API_PATH) else None)
            with frozen_spec_sources(snapshot, roots):
                (package / 'spec' / API_PATH).write_text('not: a valid API', encoding='utf-8')
                self.assertEqual(load_subsystem_manifest(manifest).role, 'gateway')
                with self.assertRaisesRegex(RuntimeError, 'differs from the captured codec snapshot'):
                    load_subsystem_manifest(foreign_manifest)
            with self.assertRaises(ManifestError):
                load_subsystem_manifest(manifest)


if __name__ == '__main__':
    unittest.main()
