"""Immutable schema snapshots and native prepared message codecs.

Preparation belongs to initialization. Per-thread workspaces own mutable output;
the schema plans may be shared. Python values cross the ABI as positional nodes,
without a JSON intermediate. Returned Python values/bytes own their storage.
"""
from __future__ import annotations

import ctypes
import json
import math
import struct
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from r2kernel.ffi import R2KernelCore
from .codec import RustMessageCodec


class CodecNode(ctypes.Structure):
    _fields_ = [
        ('kind', ctypes.c_uint32), ('children_start', ctypes.c_uint32),
        ('children_count', ctypes.c_size_t), ('bits', ctypes.c_uint64),
        ('data', ctypes.c_void_p), ('data_len', ctypes.c_size_t),
    ]


def _check(result: int, operation: str) -> None:
    if result != 0:
        raise RuntimeError(f'{operation} failed (r2kernel-comm error {result})')


def _bind(lib: Any) -> None:
    signatures = {
        'r2kernel_schema_context_create': [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)],
        'r2kernel_schema_context_free': [ctypes.c_void_p],
        'r2kernel_schema_context_spec_text': [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p],
        'r2kernel_codec_prepare': [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)],
        'r2kernel_codec_prepared_free': [ctypes.c_void_p],
        'r2kernel_codec_output_create': [ctypes.POINTER(ctypes.c_void_p)],
        'r2kernel_codec_output_free': [ctypes.c_void_p],
        'r2kernel_codec_prepared_describe': [ctypes.c_void_p, ctypes.c_void_p],
        'r2kernel_codec_prepared_encode_nodes': [ctypes.c_void_p, ctypes.POINTER(CodecNode), ctypes.c_size_t, ctypes.c_void_p],
        'r2kernel_codec_prepared_decode_nodes': [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p],
        'r2kernel_codec_output_bytes': [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)],
        'r2kernel_codec_output_nodes': [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)],
    }
    for name, args in signatures.items():
        try:
            function = getattr(lib, name)
        except AttributeError as exc:
            raise RuntimeError('Prepared codecs require the updated r2kernel-comm library') from exc
        function.argtypes = args
        function.restype = None if name.endswith('_free') else ctypes.c_int
    lib.r2kernel_codec_output_bytes.restype = ctypes.c_void_p
    lib.r2kernel_codec_output_nodes.restype = ctypes.POINTER(CodecNode)


class _Workspace:
    def __init__(self, lib: Any):
        self.lib = lib
        self.handle = ctypes.c_void_p()
        _check(lib.r2kernel_codec_output_create(ctypes.byref(self.handle)), 'Create codec workspace')
        self.capacity = 32
        self.nodes = (CodecNode * self.capacity)()

    def byte_result(self) -> bytes:
        length = ctypes.c_size_t()
        data = self.lib.r2kernel_codec_output_bytes(self.handle, ctypes.byref(length))
        return ctypes.string_at(data, length.value) if length.value else b''

    def __del__(self):
        if getattr(self, 'handle', None):
            self.lib.r2kernel_codec_output_free(self.handle)
            self.handle = None


class PreparedSchemaContext:
    def __init__(self, roots: Sequence[str | Path] | None = None, *, core: R2KernelCore | None = None):
        self.core = core or R2KernelCore()
        self.lib = self.core.lib
        _bind(self.lib)
        self.handle = ctypes.c_void_p()
        encoded_roots = None if roots is None else json.dumps([str(Path(p).resolve()) for p in roots]).encode()
        _check(self.lib.r2kernel_schema_context_create(encoded_roots, ctypes.byref(self.handle)), 'Snapshot codec schemas')
        self._codecs: dict[str, PreparedCodec] = {}
        self._prepare_lock = threading.RLock()

    def prepare(self, message_ref: str) -> PreparedCodec:
        reference = str(message_ref).strip()
        with self._prepare_lock:
            if reference not in self._codecs:
                handle = ctypes.c_void_p()
                _check(self.lib.r2kernel_codec_prepare(self.handle, reference.encode(), ctypes.byref(handle)), f'Prepare codec {reference}')
                self._codecs[reference] = PreparedCodec(self.lib, handle, reference)
            return self._codecs[reference]

    def spec_text(self, relative_path: str | Path) -> str | None:
        output = _Workspace(self.lib)
        result = self.lib.r2kernel_schema_context_spec_text(self.handle, str(relative_path).encode(), output.handle)
        if result == -2:
            return None
        _check(result, f'Read snapshot spec {relative_path}')
        return output.byte_result().decode('utf-8')

    def __del__(self):
        if getattr(self, 'handle', None):
            self.lib.r2kernel_schema_context_free(self.handle)
            self.handle = None


class PreparedCodec:
    def __init__(self, lib: Any, handle: ctypes.c_void_p, reference: str):
        self.lib, self.handle, self.reference = lib, handle, reference
        self._local = threading.local()
        workspace = self._workspace()
        _check(lib.r2kernel_codec_prepared_describe(handle, workspace.handle), f'Describe codec {reference}')
        self.description = json.loads(workspace.byte_result())
        self.graph = self.description['graph']
        self.fingerprint = self.description['fingerprint']
        self.source = self.description['source']

    def _workspace(self) -> _Workspace:
        if not hasattr(self._local, 'workspace'):
            self._local.workspace = _Workspace(self.lib)
        return self._local.workspace

    def encode(self, value: Mapping[str, Any]) -> bytes:
        workspace = self._workspace()
        nodes: list[CodecNode] = [CodecNode()]
        keepalive: list[bytes] = []
        self._encode_value(self.graph, value, nodes, 0, keepalive)
        if len(nodes) > workspace.capacity:
            workspace.capacity = max(len(nodes), workspace.capacity * 2)
            workspace.nodes = (CodecNode * workspace.capacity)()
        for index, node in enumerate(nodes):
            workspace.nodes[index] = node
        _check(self.lib.r2kernel_codec_prepared_encode_nodes(self.handle, workspace.nodes, len(nodes), workspace.handle), f'Encode {self.reference}')
        return workspace.byte_result()

    def decode(self, payload: bytes) -> dict[str, Any]:
        wire = bytes(payload)
        workspace = self._workspace()
        _check(self.lib.r2kernel_codec_prepared_decode_nodes(self.handle, wire, len(wire), workspace.handle), f'Decode {self.reference}')
        count = ctypes.c_size_t()
        nodes = self.lib.r2kernel_codec_output_nodes(workspace.handle, ctypes.byref(count))
        if not count.value:
            raise RuntimeError('Prepared decoder returned no message root')
        return self._decode_value(self.graph, nodes, 0)

    @classmethod
    def _encode_value(cls, graph: dict, value: Any, nodes: list[CodecNode], index: int, keepalive: list[bytes]) -> None:
        node = nodes[index]
        if 'message' in graph:
            if not isinstance(value, Mapping):
                raise ValueError(f"{graph['message']} requires a field mapping")
            fields = graph['fields']
            node.kind, node.children_start, node.children_count = 1, len(nodes), len(fields)
            nodes.extend(CodecNode() for _ in fields)
            start = node.children_start
            for offset, field in enumerate(fields):
                item = value.get(field['name'])
                if item is None:
                    if not field['optional']:
                        raise ValueError(f"Missing required field {graph['message']}.{field['name']}")
                else:
                    cls._encode_value(field['wire'], item, nodes, start + offset, keepalive)
        elif 'vector' in graph or 'fixed' in graph:
            if not isinstance(value, Sequence) or isinstance(value, str):
                raise ValueError('Array field requires a sequence')
            if 'fixed' in graph and len(value) != graph['fixed']:
                raise ValueError(f"Array field requires {graph['fixed']} elements")
            element = graph.get('vector', graph.get('element'))
            node.kind, node.children_start, node.children_count = 2, len(nodes), len(value)
            nodes.extend(CodecNode() for _ in value)
            start = node.children_start
            for offset, item in enumerate(value):
                cls._encode_value(element, item, nodes, start + offset, keepalive)
        else:
            node.kind = int(graph['scalar'])
            kind = node.kind
            if kind in (11, 12):
                if kind == 11:
                    if not isinstance(value, str):
                        raise ValueError('String field requires str')
                    data = value.encode('utf-8')
                else:
                    if isinstance(value, str) or isinstance(value, int):
                        raise ValueError('Bytes field requires bytes or a byte sequence')
                    data = bytes(value)
                keepalive.append(data)
                node.data, node.data_len = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p).value, len(data)
            elif kind == 10:
                if not isinstance(value, bool):
                    raise ValueError('Bool field requires bool')
                node.bits = int(value)
            elif kind in (8, 9):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError('Floating field requires a finite number')
                node.bits = int.from_bytes(struct.pack('<f' if kind == 8 else '<d', value), 'little')
            else:
                widths = {3: (0, 255), 4: (0, 65535), 5: (-(1 << 31), (1 << 31)-1), 6: (0, (1 << 32)-1), 7: (-(1 << 63), (1 << 63)-1)}
                low, high = widths[kind]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise ValueError('Integer field value is outside its declared type')
                node.bits = value & ((1 << 64)-1)

    @classmethod
    def _decode_value(cls, graph: dict, nodes: Any, index: int) -> Any:
        node = nodes[index]
        if 'message' in graph:
            return {field['name']: cls._decode_value(field['wire'], nodes, node.children_start + offset)
                    for offset, field in enumerate(graph['fields']) if nodes[node.children_start + offset].kind != 0}
        if 'vector' in graph or 'fixed' in graph:
            element = graph.get('vector', graph.get('element'))
            return [cls._decode_value(element, nodes, node.children_start + offset) for offset in range(node.children_count)]
        kind = node.kind
        if kind in (11, 12):
            data = ctypes.string_at(node.data, node.data_len) if node.data_len else b''
            return data.decode('utf-8') if kind == 11 else data
        if kind == 10:
            return bool(node.bits)
        if kind in (8, 9):
            return struct.unpack('<f' if kind == 8 else '<d', node.bits.to_bytes(4 if kind == 8 else 8, 'little'))[0]
        if kind in (5, 7):
            return ctypes.c_int32(node.bits).value if kind == 5 else ctypes.c_int64(node.bits).value
        return int(node.bits)

    def metadata(self) -> dict[str, str]:
        return {'ref': self.reference, 'fingerprint': self.fingerprint, 'source': self.source}

    def __del__(self):
        if getattr(self, 'handle', None):
            self.lib.r2kernel_codec_prepared_free(self.handle)
            self.handle = None


class PreparedMessageCodec(RustMessageCodec):
    """SDK message API pinned to one context, sealed after initialization."""
    def __init__(self, context: PreparedSchemaContext):
        self.context = context
        self._core, self._lib = context.core, context.lib
        self._prepared: dict[str, PreparedCodec] = {}
        self._sealed = False

    def prepare_message(self, message_ref: str) -> PreparedCodec:
        reference = str(message_ref).strip()
        if reference not in self._prepared:
            if self._sealed:
                raise RuntimeError(f'Message type was not prepared before Ready: {reference}')
            self._prepared[reference] = self.context.prepare(reference)
        return self._prepared[reference]

    def seal(self) -> None:
        self._sealed = True

    def encode_message(self, message_ref: str, payload: dict[str, Any]) -> bytes:
        if str(message_ref).strip() == 'bytes':
            return super().encode_message(message_ref, payload)
        return self.prepare_message(message_ref).encode(payload)

    def decode_message(self, message_ref: str, payload: bytes) -> dict[str, Any]:
        if str(message_ref).strip() == 'bytes':
            return super().decode_message(message_ref, payload)
        return self.prepare_message(message_ref).decode(payload)

    def get_message_schema(self, message_ref: str) -> dict[str, Any]:
        return self.prepare_message(message_ref).description['schema']

    def metadata(self, message_ref: str) -> dict[str, str]:
        return self.prepare_message(message_ref).metadata()

    def _encode_timing_wire(self, kind, header_ref, header, payload_ref, payload):
        header_wire = self.encode_message(header_ref, header)
        reference = str(payload_ref).encode('utf-8')
        if not reference:
            raise ValueError('Timing wire requires a payload type')
        payload_wire = self.encode_message(payload_ref, payload)
        return struct.pack('<4sBBHIII', b'SDTM', 1, kind, 0, len(header_wire), len(reference), len(payload_wire)) + header_wire + reference + payload_wire

    def _decode_timing_wire(self, kind, header_ref, wire):
        wire = bytes(wire)
        if len(wire) < 20:
            raise ValueError('Truncated timing wire')
        magic, version, actual_kind, _, header_len, ref_len, payload_len = struct.unpack_from('<4sBBHIII', wire)
        if magic != b'SDTM' or version != 1 or actual_kind != kind or not ref_len:
            raise ValueError('Invalid timing wire header')
        header_end, ref_end = 20 + header_len, 20 + header_len + ref_len
        payload_end = ref_end + payload_len
        if payload_end > len(wire):
            raise ValueError('Truncated timing wire payload')
        reference = wire[header_end:ref_end].decode('utf-8')
        return self.decode_message(header_ref, wire[20:header_end]), reference, self.decode_message(reference, wire[ref_end:payload_end])

    def encode_timing_request_wire(self, *, request_header, payload_ref, payload):
        return self._encode_timing_wire(1, 'msg.internal.control.v1/RequestHeader', request_header, payload_ref, payload)

    def decode_timing_request_wire(self, wire):
        return self._decode_timing_wire(1, 'msg.internal.control.v1/RequestHeader', wire)

    def encode_timing_response_wire(self, *, response_header, payload_ref, payload):
        return self._encode_timing_wire(2, 'msg.internal.control.v1/ResponseHeader', response_header, payload_ref, payload)

    def decode_timing_response_wire(self, wire):
        return self._decode_timing_wire(2, 'msg.internal.control.v1/ResponseHeader', wire)
