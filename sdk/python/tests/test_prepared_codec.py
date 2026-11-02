from __future__ import annotations

import concurrent.futures
from pathlib import Path
import tempfile
import unittest
import yaml
from unittest.mock import patch

from r2kernel.interface.codec import RustMessageCodec
from r2kernel.interface.prepared_codec import PreparedSchemaContext, PreparedMessageCodec
from r2kernel.spec_paths import frozen_spec_sources, resolve_spec_path, read_spec_text
from r2kernel.core.codec_lifecycle import CodecLifecycle


REF = 'msg.core.common.primitive.v1/IndexedBoolValue'


class PreparedCodecTests(unittest.TestCase):
    def test_standard_native_exact_wire_and_no_json_packet_conversion(self):
        context = PreparedSchemaContext([])
        codec = context.prepare(REF)
        value = {'index': 7, 'value': True}
        generic = RustMessageCodec().encode_message(REF, value)
        self.assertEqual(codec.source, 'standard')
        with patch('json.dumps', side_effect=AssertionError('packet JSON conversion')):
            wire = codec.encode(value)
            self.assertEqual(wire, generic)
            self.assertEqual(codec.decode(wire), value)
        for invalid in ({'index': -1, 'value': True}, {'index': 0, 'value': 1}, {'value': True}):
            with self.assertRaises(ValueError):
                codec.encode(invalid)

    def test_shared_codec_independent_thread_workspaces(self):
        codec = PreparedSchemaContext([]).prepare(REF)
        def worker(seed):
            for number in range(seed, seed + 300):
                value = {'index': number, 'value': bool(number % 2)}
                self.assertEqual(codec.decode(codec.encode(value)), value)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(worker, (0, 1000, 2000, 3000)))

    def test_prepared_timing_envelopes_match_existing_core_wire(self):
        prepared = PreparedMessageCodec(PreparedSchemaContext([]))
        generic = RustMessageCodec()
        value = {'index': 7, 'value': True}
        headers = [('request', {'request_id': 'r1', 'sent_at_sync_ns': 123}),
                   ('response', {'request_id': 'r1', 'responded_at_sync_ns': 234, 'status_code': 0})]
        for direction, header in headers:
            arguments = {direction + '_header': header, 'payload_ref': REF, 'payload': value}
            wire = getattr(prepared, 'encode_timing_' + direction + '_wire')(**arguments)
            self.assertEqual(wire, getattr(generic, 'encode_timing_' + direction + '_wire')(**arguments))
            self.assertEqual(getattr(prepared, 'decode_timing_' + direction + '_wire')(wire),
                             getattr(generic, 'decode_timing_' + direction + '_wire')(wire))

    def test_active_callback_blocks_snapshot_replacement(self):
        lifecycle = CodecLifecycle(object())
        with lifecycle.callback():
            with self.assertRaisesRegex(RuntimeError, 'active callback'):
                with lifecycle.initialization():
                    self.fail('Active callback cannot allow initialization')
        self.assertEqual(lifecycle._active_callbacks, 0)

    def test_custom_types_snapshot_edit_and_frozen_contract_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'messages/msg/test/prepared/v1.yaml'
            source.parent.mkdir(parents=True)
            source.write_text('''version: 1
id: msg.test.prepared.v1
kind: message_set
messages:
  Child:
    fields:
      - {name: value, type: int64}
  Value:
    fields:
      - {name: child, type: Child}
      - {name: names, type: "vector<string>"}
      - {name: counts, type: "array<int32,2>"}
      - {name: data, type: bytes}
      - {name: optional, type: bool, optional: true}
''')
            context = PreparedSchemaContext([root])
            codec = context.prepare('msg.test.prepared.v1/Value')
            value = {'child': {'value': -123}, 'names': ['한글', '', 'a\0b'], 'counts': [-1, 2], 'data': b'\0\xff'}
            self.assertEqual(codec.source, 'dynamic')
            self.assertEqual(codec.decode(codec.encode(value)), value)
            before = source.read_text()
            source.write_text(before.replace('type: int64', 'type: int32'))
            changed = PreparedSchemaContext([root]).prepare('msg.test.prepared.v1/Value')
            self.assertNotEqual(codec.fingerprint, changed.fingerprint)
            source.unlink()
            self.assertEqual(codec.decode(codec.encode(value)), value)
            with frozen_spec_sources(context, [root]):
                selected = resolve_spec_path('messages/msg/test/prepared/v1.yaml')
                self.assertEqual(read_spec_text(selected), before)
            with self.assertRaises(RuntimeError):
                PreparedSchemaContext([root]).prepare('msg.test.prepared.v1/Value')

    def test_override_standard_is_dynamic_only_when_wire_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            relative = 'messages/msg/core/common/primitive/v1.yaml'
            original = PreparedSchemaContext([]).spec_text(relative)
            source = root / relative
            source.parent.mkdir(parents=True)
            document = yaml.safe_load(original)
            document['description'] = 'edited prose'
            source.write_text(yaml.safe_dump(document, sort_keys=False))
            self.assertEqual(PreparedSchemaContext([root]).prepare(REF).source, 'standard')
            source.write_text('''version: 1
id: msg.core.common.primitive.v1
kind: message_set
messages:
  IndexedBoolValue:
    fields:
      - {name: index, type: int64}
      - {name: value, type: bool}
''')
            self.assertEqual(PreparedSchemaContext([root]).prepare(REF).source, 'dynamic')

    def test_sealed_messages_fail_explicitly_and_peer_identity_is_required(self):
        codec = PreparedMessageCodec(PreparedSchemaContext([]))
        codec.prepare_message(REF)
        codec.seal()
        with self.assertRaisesRegex(RuntimeError, 'not prepared before Ready'):
            codec.encode_message('msg.core.common.primitive.v1/Int32Value', {'value': 1})
        lifecycle = CodecLifecycle(object())
        lifecycle.codec = codec
        peer = {'subsystem': 'gateway', 'codec_metadata': {REF: codec.metadata(REF)}}
        lifecycle.validate_route(peer, [REF])
        with self.assertRaisesRegex(RuntimeError, 'missing codec identity'):
            lifecycle.validate_route({'subsystem': 'gateway'}, [REF])
        peer['codec_metadata'][REF] = dict(codec.metadata(REF), fingerprint='0' * 64)
        with self.assertRaisesRegex(RuntimeError, 'schema mismatch'):
            lifecycle.validate_route(peer, [REF])


if __name__ == '__main__':
    unittest.main()
