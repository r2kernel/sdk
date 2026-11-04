"""The teleop HTTP adapter serves a Unix frontend listener across stop/reopen."""
from __future__ import annotations

import http.client
import json
import os
import select
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

TUTORIALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TUTORIALS / "subsystems/app.cleaner.teleop"))

from teleop_app import _BrokeredFrontendServer  # noqa: E402


def test_unix_listener_serves_http_and_shutdown_allows_reopen(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"peer": self.client_address, "address": self.address_string(),
                               "server": self.server.server_address}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    endpoint = tmp_path / "teleop.sock"
    metadata = {"domain": "unix", "transport": "unix_listener", "bind_host": "127.0.0.1", "bind_port": 18091}
    for _ in range(2):
        endpoint.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(endpoint))
            listener.listen(2)
            with socket.socket(fileno=os.dup(listener.fileno())) as broker_copy:
                server = _BrokeredFrontendServer(listener, Handler, metadata)
                worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
                worker.start()
                try:
                    assert server.server_name == "127.0.0.1" and server.server_port == 18091
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                        connection.settimeout(3)
                        connection.connect(str(endpoint))
                        connection.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                        response = http.client.HTTPResponse(connection)
                        response.begin()
                        assert response.status == 200
                        assert json.loads(response.read()) == {
                            "peer": ["127.0.0.1", 0], "address": "127.0.0.1", "server": ["127.0.0.1", 18091],
                        }
                        response.close()
                finally:
                    server.shutdown()
                    worker.join(3)
                    server.server_close()
                assert not worker.is_alive()
                poller = select.poll()
                poller.register(broker_copy, select.POLLHUP)
                assert any(flags & select.POLLHUP for _, flags in poller.poll(1000))
        endpoint.unlink()
