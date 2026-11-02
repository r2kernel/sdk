from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


class R2KernelClockSnapshot(ctypes.Structure):
    _fields_ = [
        ("realtime_ns", ctypes.c_int64),
        ("monotonic_ns", ctypes.c_int64),
        ("logical_ns", ctypes.c_int64),
        ("offset_ns", ctypes.c_int64),
        ("rtt_ns", ctypes.c_int64),
        ("uncertainty_ns", ctypes.c_int64),
        ("source", ctypes.c_uint8),
        ("stable", ctypes.c_uint8),
        ("accepted_for_one_way", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8),
    ]


class R2KernelShmSlotInfo(ctypes.Structure):
    _fields_ = [
        ("seq", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
        ("length", ctypes.c_uint32),
        ("generation", ctypes.c_uint64),
        ("timestamp", ctypes.c_int64),
        ("prev_timestamp", ctypes.c_int64),
        ("published_timestamp", ctypes.c_int64),
    ]


class R2KernelCore:
    def __init__(self, lib_path: str | None = None):
        self._lib = self._load_library(lib_path)
        self._bind_signatures()

    @property
    def lib(self):
        return self._lib

    def _load_library(self, lib_path: str | None):
        if lib_path:
            return ctypes.CDLL(lib_path)

        candidates = []
        prefix_candidates = self._prefix_candidates()
        for lib_name in self._lib_filenames():
            bundled = Path(__file__).resolve().parent / "lib" / lib_name
            candidates.append(bundled)
            for prefix in prefix_candidates:
                candidates.append(prefix / "runtime" / "lib" / lib_name)

        for path in candidates:
            if path.exists():
                return ctypes.CDLL(str(path))

        raise FileNotFoundError(
            "R2Kernel runtime core library not found. Install the R2Kernel runtime under "
            "`/opt/r2kernel`, set `R2KERNEL_PREFIX` to an installed runtime prefix, or source "
            "`<prefix>/env.sh` for a custom prefix."
        )

    @staticmethod
    def _prefix_candidates() -> tuple[Path, ...]:
        candidates: list[Path] = []
        env_prefix = os.environ.get("R2KERNEL_PREFIX")
        if env_prefix:
            candidates.append(Path(env_prefix).expanduser())
        candidates.append(Path("/opt/r2kernel"))

        unique: list[Path] = []
        for path in candidates:
            if path not in unique:
                unique.append(path)
        return tuple(unique)

    @staticmethod
    def _lib_filenames() -> list[str]:
        if sys.platform == "darwin":
            return ["libr2kernel_core.dylib"]
        if sys.platform.startswith("linux"):
            return ["libr2kernel_core.so"]
        if sys.platform == "win32":
            return ["r2kernel_core.dll"]
        raise RuntimeError(f"Unsupported platform: {sys.platform}")

    def _bind_signatures(self):
        self._lib.r2kernel_clock_realtime_ns.argtypes = []
        self._lib.r2kernel_clock_realtime_ns.restype = ctypes.c_int64
        self._lib.r2kernel_clock_monotonic_ns.argtypes = []
        self._lib.r2kernel_clock_monotonic_ns.restype = ctypes.c_int64
        self._lib.r2kernel_clock_now_ns.argtypes = []
        self._lib.r2kernel_clock_now_ns.restype = ctypes.c_int64
        self._lib.r2kernel_clock_snapshot.argtypes = [
            ctypes.POINTER(R2KernelClockSnapshot),
        ]
        self._lib.r2kernel_clock_snapshot.restype = ctypes.c_int
        self._lib.r2kernel_clock_configure.argtypes = [
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_uint8,
        ]
        self._lib.r2kernel_clock_configure.restype = ctypes.c_int
        self._lib.r2kernel_clock_reset.argtypes = []
        self._lib.r2kernel_clock_reset.restype = ctypes.c_int

        # Canonical schema / codec
        self._lib.r2kernel_schema_get_message_json.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_schema_get_message_json.restype = ctypes.c_int
        self._lib.r2kernel_schema_get_message_set_json.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_schema_get_message_set_json.restype = ctypes.c_int
        self._lib.r2kernel_codec_encode_message_json.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_encode_message_json.restype = ctypes.c_int
        self._lib.r2kernel_codec_decode_message_json.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_decode_message_json.restype = ctypes.c_int
        self._lib.r2kernel_codec_encode_timing_request_json.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_encode_timing_request_json.restype = ctypes.c_int
        self._lib.r2kernel_codec_decode_timing_request_json.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_decode_timing_request_json.restype = ctypes.c_int
        self._lib.r2kernel_codec_encode_timing_response_json.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_encode_timing_response_json.restype = ctypes.c_int
        self._lib.r2kernel_codec_decode_timing_response_json.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_decode_timing_response_json.restype = ctypes.c_int
        self._lib.r2kernel_codec_encode_control_request_v2.argtypes = [
            ctypes.c_uint64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_uint16,
            ctypes.c_uint8,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_encode_control_request_v2.restype = ctypes.c_int
        self._lib.r2kernel_codec_decode_control_request_v2.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_decode_control_request_v2.restype = ctypes.c_int
        self._lib.r2kernel_codec_encode_control_response_v2.argtypes = [
            ctypes.c_uint64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_int32,
            ctypes.c_uint8,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_encode_control_response_v2.restype = ctypes.c_int
        self._lib.r2kernel_codec_decode_control_response_v2.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_decode_control_response_v2.restype = ctypes.c_int
        self._lib.r2kernel_codec_encode_primitive_vector.argtypes = [
            ctypes.c_uint8,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_encode_primitive_vector.restype = ctypes.c_int
        self._lib.r2kernel_codec_decode_primitive_vector.argtypes = [
            ctypes.c_uint8,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_codec_decode_primitive_vector.restype = ctypes.c_int

        # SHM
        self._lib.r2kernel_shm_create.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        self._lib.r2kernel_shm_create.restype = ctypes.c_int
        self._lib.r2kernel_shm_open.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        self._lib.r2kernel_shm_open.restype = ctypes.c_int
        self._lib.r2kernel_shm_close.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_shm_close.restype = ctypes.c_int
        self._lib.r2kernel_shm_unlink.argtypes = [ctypes.c_char_p]
        self._lib.r2kernel_shm_unlink.restype = ctypes.c_int

        self._lib.r2kernel_shm_publish.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint64, ctypes.c_int64, ctypes.c_int64,
        ]
        self._lib.r2kernel_shm_publish.restype = ctypes.c_int
        self._lib.r2kernel_shm_read_snapshot.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(R2KernelShmSlotInfo), ctypes.c_void_p, ctypes.c_size_t,
        ]
        self._lib.r2kernel_shm_read_snapshot.restype = ctypes.c_int
        self._lib.r2kernel_shm_reset_slot.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self._lib.r2kernel_shm_reset_slot.restype = ctypes.c_int

        # Event client
        self._lib.r2kernel_event_client_create.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        self._lib.r2kernel_event_client_create.restype = ctypes.c_int
        self._lib.r2kernel_event_client_close.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_event_client_close.restype = ctypes.c_int
        self._lib.r2kernel_event_client_subscribe.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
        self._lib.r2kernel_event_client_subscribe.restype = ctypes.c_int
        try:
            self._lib.r2kernel_event_client_subscribe_ex.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_size_t,
                ctypes.c_size_t,
                ctypes.c_size_t,
                ctypes.c_char_p,
            ]
            self._lib.r2kernel_event_client_subscribe_ex.restype = ctypes.c_int
            self._lib.r2kernel_event_client_subscribe_latest_ex.argtypes = (
                self._lib.r2kernel_event_client_subscribe_ex.argtypes
            )
            self._lib.r2kernel_event_client_subscribe_latest_ex.restype = ctypes.c_int
        except AttributeError:
            pass
        self._lib.r2kernel_event_client_recv.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_client_recv.restype = ctypes.c_int
        self._lib.r2kernel_event_client_recv_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_client_recv_ex.restype = ctypes.c_int

        # Event transport
        self._lib.r2kernel_event_transport_create.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._lib.r2kernel_event_transport_create.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_close.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_event_transport_close.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_bind.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_uint16),
        ]
        self._lib.r2kernel_event_transport_bind.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_apply_state_json.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_transport_apply_state_json.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_accept_peer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_transport_accept_peer.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_enqueue_event.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint16,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int64,
            ctypes.c_uint16,
        ]
        self._lib.r2kernel_event_transport_enqueue_event.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_enqueue_event_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint16,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_uint16,
        ]
        self._lib.r2kernel_event_transport_enqueue_event_ex.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_flush_n.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_transport_flush_n.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_recv_event.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_transport_recv_event.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_recv_event_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_transport_recv_event_ex.restype = ctypes.c_int
        # Same outputs as recv_event_ex plus an explicit timeout (0 = never block).
        self._lib.r2kernel_event_transport_recv_event_timeout.argtypes = [
            *self._lib.r2kernel_event_transport_recv_event_ex.argtypes[:2],
            ctypes.c_int64,
            *self._lib.r2kernel_event_transport_recv_event_ex.argtypes[2:],
        ]
        self._lib.r2kernel_event_transport_recv_event_timeout.restype = ctypes.c_int
        # Session establishment and apply connects without the transport handle.
        self._lib.r2kernel_event_transport_session_setup.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._lib.r2kernel_event_transport_session_setup.restype = ctypes.c_int
        self._lib.r2kernel_event_session_setup_close.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_event_session_setup_close.restype = ctypes.c_int
        self._lib.r2kernel_event_session_setup_accept.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._lib.r2kernel_event_session_setup_accept.restype = ctypes.c_int
        self._lib.r2kernel_event_session_peer_id.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_session_peer_id.restype = ctypes.c_int
        self._lib.r2kernel_event_session_close.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_event_session_close.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_install_session.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._lib.r2kernel_event_transport_install_session.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_apply_begin_json.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._lib.r2kernel_event_transport_apply_begin_json.restype = ctypes.c_int
        self._lib.r2kernel_event_apply_plan_connect.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_event_apply_plan_connect.restype = ctypes.c_int
        self._lib.r2kernel_event_transport_apply_finish.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._lib.r2kernel_event_transport_apply_finish.restype = ctypes.c_int
        self._lib.r2kernel_event_apply_plan_close.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_event_apply_plan_close.restype = ctypes.c_int

        # Locks
        self._lib.r2kernel_lock_create.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self._lib.r2kernel_lock_create.restype = ctypes.c_int
        self._lib.r2kernel_lock_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self._lib.r2kernel_lock_open.restype = ctypes.c_int
        self._lib.r2kernel_lock_close.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_lock_close.restype = ctypes.c_int
        self._lib.r2kernel_lock_unlink.argtypes = [ctypes.c_char_p]
        self._lib.r2kernel_lock_unlink.restype = ctypes.c_int
        self._lib.r2kernel_lock_post.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_lock_post.restype = ctypes.c_int
        self._lib.r2kernel_lock_trywait.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_lock_trywait.restype = ctypes.c_int
        self._lib.r2kernel_lock_wait.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        self._lib.r2kernel_lock_wait.restype = ctypes.c_int

        # Control transport
        self._lib.r2kernel_control_client_create.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self._lib.r2kernel_control_client_create.restype = ctypes.c_int
        self._lib.r2kernel_control_client_close.argtypes = [ctypes.c_void_p]
        self._lib.r2kernel_control_client_close.restype = ctypes.c_int
        self._lib.r2kernel_control_client_connect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32]
        self._lib.r2kernel_control_client_connect.restype = ctypes.c_int
        self._lib.r2kernel_control_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int64, ctypes.c_float, ctypes.c_float]
        self._lib.r2kernel_control_client_send.restype = ctypes.c_int
        self._lib.r2kernel_control_client_recv.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int64, ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_uint16), ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self._lib.r2kernel_control_client_recv.restype = ctypes.c_int


    # Minimal helpers for raw calls can be added later
