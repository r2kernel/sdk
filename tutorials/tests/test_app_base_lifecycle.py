from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


TUTORIALS = Path(__file__).resolve().parents[1]
ROOT = TUTORIALS.parent
for path in (ROOT / "sdk" / "python", TUTORIALS / "lib"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app_base import CleanerApplicationBase  # noqa: E402


def make_app() -> tuple[CleanerApplicationBase, dict[str, bool]]:
    app = CleanerApplicationBase.__new__(CleanerApplicationBase)
    app._mode = "test"
    transport = {"active": True}
    app.start_event_transport = Mock(side_effect=lambda: transport.__setitem__("active", True))
    app.stop_event_transport = Mock(side_effect=lambda: transport.__setitem__("active", False))
    app.start_procedure_server = Mock()
    app.stop_procedure_server = Mock()
    app._safe_stop_drive = Mock()
    app._set_status = Mock(return_value={"active": False})
    app._publish_status = Mock(
        side_effect=lambda *, reason: _publish_if_transport_is_active(transport, reason)
    )
    return app, transport


def _publish_if_transport_is_active(transport: dict[str, bool], reason: str) -> dict[str, str]:
    if not transport["active"]:
        raise RuntimeError("lifecycle notify endpoint is unavailable")
    return {"reason": reason}


class CleanerApplicationBaseLifecycleTest(unittest.TestCase):
    def test_stop_keeps_event_transport_available_for_restart(self) -> None:
        app, transport = make_app()

        self.assertTrue(app.on_stop())

        self.assertTrue(transport["active"])
        app._safe_stop_drive.assert_called_once_with()
        app.stop_procedure_server.assert_called_once_with()
        app.stop_event_transport.assert_not_called()
        app._set_status.assert_called_once_with(action="test.stop", active=False)
        app._publish_status.assert_called_once_with(reason="test.stop")

        self.assertTrue(app.on_start())

        app.start_event_transport.assert_called_once_with()
        app.start_procedure_server.assert_called_once_with()
        app._publish_status.assert_called_with(reason="start")
        self.assertTrue(transport["active"])

    def test_reset_keeps_event_transport_available_for_restart(self) -> None:
        app, transport = make_app()

        self.assertTrue(app.on_reset())

        self.assertTrue(transport["active"])
        app._safe_stop_drive.assert_called_once_with()
        app.stop_procedure_server.assert_called_once_with()
        app.stop_event_transport.assert_not_called()
        app._set_status.assert_called_once_with(action="test.reset", active=False)
        app._publish_status.assert_called_once_with(reason="test.reset")

        self.assertTrue(app.on_start())
        self.assertTrue(transport["active"])

    def test_terminate_closes_event_transport(self) -> None:
        app, transport = make_app()

        self.assertTrue(app.on_terminate())

        app.stop_procedure_server.assert_called_once_with()
        app.stop_event_transport.assert_called_once_with()
        self.assertFalse(transport["active"])


if __name__ == "__main__":
    unittest.main()
