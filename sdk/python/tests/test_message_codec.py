from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from r2kernel.interface.codec import RustMessageCodec
from r2kernel.spec_paths import _PACKAGE_SPEC_ROOT


class RustMessageCodecBytesTest(unittest.TestCase):
    def setUp(self) -> None:
        # The public messages live in the SDK's packaged spec root, which the native resolver only
        # searches when configured; never rely on another test having configured it first.
        spec_root = patch.dict(os.environ, {"R2KERNEL_SPEC_ROOT": str(_PACKAGE_SPEC_ROOT)})
        spec_root.start()
        self.addCleanup(spec_root.stop)
        self.codec = RustMessageCodec()

    def test_bytes_fields_round_trip_as_python_bytes(self) -> None:
        message_ref = "msg.core.common.result.v1/Result"
        payload = {"success": True, "status": "done", "detail": b'{"ok":true}'}

        encoded = self.codec.encode_message(message_ref, payload)
        decoded = self.codec.decode_message(message_ref, encoded)

        self.assertEqual(decoded, payload)

    def test_required_bytes_field_round_trip(self) -> None:
        message_ref = "msg.mapping.v1/OccupancyGridMap"
        payload = {
            "map_id": "floor_1",
            "frame_id": "map",
            "resolution": 0.5,
            "width": 2,
            "height": 1,
            "origin": {
                "position": {"x": 1.0, "y": -2.0, "z": 0.0},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            },
            "data": b"\x00\x64",
        }

        encoded = self.codec.encode_message(message_ref, payload)
        decoded = self.codec.decode_message(message_ref, encoded)

        self.assertEqual(decoded, payload)

    def test_internal_bytes_contract_round_trips_json_object(self) -> None:
        payload = {"active": True, "last_result": {"state": "ready"}}

        encoded = self.codec.encode_message("bytes", payload)
        decoded = self.codec.decode_message("bytes", encoded)

        self.assertEqual(decoded, payload)


class _CountingLibrary:
    """Proxy over the native library that records which entry points the codec looks up."""

    def __init__(self, library) -> None:
        self._library = library
        self.names: list[str] = []

    def __getattr__(self, name: str):
        self.names.append(name)
        return getattr(self._library, name)


class GenericCodecSingleConversionTest(unittest.TestCase):
    MESSAGE = "msg.actuator.servo.v1/ActuatorCommand"

    def test_large_generic_round_trip_uses_only_single_conversion_abi(self) -> None:
        codec = RustMessageCodec()
        counting = _CountingLibrary(codec._lib)
        codec._lib = counting
        codec._generic_output()  # binds the ABI once for this thread; not a conversion
        counting.names.clear()
        axes = 1500  # decoded JSON is far larger than the old 4096-byte first attempt
        payload = {
            "emergency": False,
            "servo_cmd": 1,
            "drive_mode": [8] * axes,
            "target": {"target_position": [0.25] * axes, "target_velocity": [0.5] * axes},
        }
        encoded = codec.encode_message(self.MESSAGE, payload)
        decoded = codec.decode_message(self.MESSAGE, encoded)
        self.assertEqual(decoded["drive_mode"], payload["drive_mode"])
        self.assertEqual(decoded["target"], payload["target"])
        conversions = [name for name in counting.names if name.startswith("r2kernel_codec_")
                       and "message_json" in name]
        self.assertEqual(conversions, ["r2kernel_codec_encode_message_json_into",
                                       "r2kernel_codec_decode_message_json_into"])

    def test_generic_failure_is_explicit(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Rust message decode failed rc="):
            RustMessageCodec().decode_message(self.MESSAGE, b"\x01")

    def test_unprepared_warning_is_logged_once(self) -> None:
        import r2kernel.interface.codec as codec_module

        class _Logger:
            def __init__(self) -> None:
                self.records: list[tuple[str, str]] = []

            def warning(self, log_id: str, message: str) -> None:
                self.records.append((log_id, message))

        logger = _Logger()
        with patch.object(codec_module, "_unprepared_warning_emitted", False):
            self.assertTrue(codec_module.warn_unprepared_codec_once(logger))
            self.assertFalse(codec_module.warn_unprepared_codec_once(logger))
        self.assertEqual(logger.records, [(codec_module.LOG_ID_CODEC, codec_module.UNPREPARED_CODEC_WARNING)])


if __name__ == "__main__":
    unittest.main()
