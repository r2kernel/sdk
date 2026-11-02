from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r2kernel.core.native_control import NativeControlClient


class ControlPermissionTests(unittest.TestCase):
    def test_applies_runtime_ipc_socket_mode_without_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "handler.sock"
            path.write_text("", encoding="utf-8")
            client = object.__new__(NativeControlClient)
            with patch.dict(
                os.environ,
                {
                    "R2KERNEL_RUNTIME_IPC_GROUP": "",
                    "R2KERNEL_RUNTIME_IPC_SOCKET_MODE": "0770",
                },
                clear=False,
            ):
                client._apply_local_ipc_socket_policy(str(path))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o770)


if __name__ == "__main__":
    unittest.main()
