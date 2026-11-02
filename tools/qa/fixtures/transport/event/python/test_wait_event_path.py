from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import wait_event_path as gate
from start_gate import StartGate, StartGateError


def native_runtime(peer_id, **topics):
    return {
        **topics,
        "applied": {
            "active_peers": [peer_id],
            "connected": [peer_id],
            "subscriptions_synced": [peer_id],
            "data_plane": {"local_event_subscriptions": 1, "inbound_sink_count": 1},
        },
    }


@contextlib.contextmanager
def secured_status(key, header, runtime, time_sync=None, *, publisher=False, subscriber=False,
                   local_host=None, connections=()):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get(header))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/status")
                self.end_headers()
                return
            if self.headers.get(header) != key:
                self.send_response(401, "denied")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            status = {"stamped_stream_runtime_status": {
                "runtime": runtime, "local_host": local_host, "connections": list(connections),
            }}
            if publisher:
                status["subsystem_status"] = {"publisher": {"publishing": [["test", "bytes"]]}}
            if subscriber:
                status.setdefault("subsystem_status", {})["subscriber"] = {"subscribing": [["test", "bytes"]]}
            if time_sync is not None:
                status["time_sync_status"] = time_sync
            self.wfile.write(json.dumps(status).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class SecuredEventGateTest(unittest.TestCase):
    def key_file(self, root, name, key):
        path = root / name
        path.write_text(key + "\n", encoding="ascii")
        path.chmod(0o600)
        return path

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["wait_event_path.py", *args]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = gate.main()
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue() + stderr.getvalue()

    def test_per_side_auth_opens_only_real_ready_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sender_key = self.key_file(root, "sender", "sender-private-key")
            receiver_key = self.key_file(root, "receiver", "receiver-private-key")
            publisher = StartGate(root / "ready")
            publisher.acknowledge("run", "publisher")
            self.addCleanup(publisher.close)
            with secured_status("Bearer sender-private-key", "Authorization", native_runtime("receiver", forward_ready_topics=["test"]), publisher=True, local_host="sender") as (sender, sender_seen), secured_status("receiver-private-key", "X-R2Kernel-API-Key", native_runtime("sender", inbound_ready_topics=["test"]), local_host="receiver") as (receiver, receiver_seen):
                args = ["--sender-status-url", sender + "/status", "--receiver-status-url", receiver + "/status",
                        "--sender-api-key-file", str(sender_key), "--receiver-api-key-file", str(receiver_key),
                        "--receiver-auth-header", "X-R2Kernel-API-Key", "--topic", "test", "--timeout-s", "0.1",
                        "--start-gate-file", str(root / "ready"), "--report-format", "json",
                        "--start-gate-session", "run", "--publisher-name", "publisher",
                        "--report-path", str(root / "report.json")]
                code, text = self.invoke(args)
                self.assertEqual(code, 0)
                self.assertTrue((root / "ready").exists())
                self.assertEqual(sender_seen, ["Bearer sender-private-key"])
                self.assertEqual(receiver_seen, ["receiver-private-key"])
                for secret in ("sender-private-key", "receiver-private-key"):
                    self.assertNotIn(secret, text + (root / "report.json").read_text())

                # A new attempt must not erase the previous success to look safe.
                # Both producer and consumer refuse the stale gate before any auth request.
                receiver_key.write_text("wrong-key\n")
                with self.assertRaises(ValueError):
                    StartGate(root / "ready")
                code, text = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertTrue((root / "ready").exists())
                self.assertIn("already exists", text)
                self.assertEqual(receiver_seen, ["receiver-private-key"])

                # A correctly scoped new run remains closed on the new auth failure.
                fresh = root / "next-run"
                fresh.mkdir(mode=0o700)
                args[args.index("--start-gate-file") + 1] = str(fresh / "ready")
                next_publisher = StartGate(fresh / "ready")
                next_publisher.acknowledge("run", "publisher")
                self.addCleanup(next_publisher.close)
                code, text = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertFalse((fresh / "ready").exists())
                self.assertIn("HTTP 401", text)
                for secret in ("sender-private-key", "receiver-private-key", "wrong-key"):
                    self.assertNotIn(secret, text + (root / "report.json").read_text())

    def test_topology_only_does_not_open_secured_gate(self):
        with secured_status("Bearer secret", "Authorization", {"remote_subscribe_topics": ["test"]}, publisher=True) as (sender, _), secured_status("Bearer secret", "Authorization", {"inbound_topics": ["test"]}) as (receiver, _):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                key = self.key_file(root, "key", "secret")
                publisher = StartGate(root / "ready")
                publisher.acknowledge("run", "publisher")
                self.addCleanup(publisher.close)
                code, _ = self.invoke(["--sender-status-url", sender + "/status", "--sender-api-key-file", str(key),
                                       "--receiver-status-url", receiver + "/status", "--receiver-api-key-file", str(key),
                                       "--scope", "cross-host", "--topic", "test", "--timeout-s", "0.1",
                                       "--start-gate-file", str(root / "ready"), "--start-gate-session", "run",
                                       "--publisher-name", "publisher"])
                self.assertEqual(code, 1)
                self.assertFalse((root / "ready").exists())

    def test_mutual_native_transport_controls_acknowledged_publisher_boundary(self):
        sending = native_runtime("receiver", forward_ready_topics=["test"],
                                 remote_subscribe_topics=["test"], local_subscription_topics=["test"])
        receiving = native_runtime("sender", inbound_ready_topics=["test"],
                                   inbound_topics=["test"], local_subscription_topics=["test"])
        with secured_status("Bearer secret", "Authorization", sending, publisher=True,
                            local_host="sender", connections=["receiver"]) as (sender, _), \
                secured_status("Bearer secret", "Authorization", receiving, subscriber=True,
                               local_host="receiver", connections=["sender"]) as (receiver, _), \
                tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = self.key_file(root, "key", "secret")
            cases = (
                ("sender-empty", "sender", ("active_peers", "connected", "subscriptions_synced"), False),
                ("mutual", None, (), True),
                ("steady-state", "both", ("connected",), True),
                ("sender-peer-loss", "sender", ("active_peers",), False),
                ("receiver-peer-loss", "receiver", ("active_peers",), False),
                ("sender-unsynced", "sender", ("subscriptions_synced",), False),
                ("receiver-unsynced", "receiver", ("subscriptions_synced",), False),
                ("unrelated-sender-peer", "sender", ("active_peers", "connected", "subscriptions_synced"), False),
            )
            for name, side, missing, expected in cases:
                with self.subTest(case=name):
                    for runtime, peer in ((sending, "receiver"), (receiving, "sender")):
                        for field in ("active_peers", "connected", "subscriptions_synced"):
                            runtime["applied"][field] = [peer]
                    affected = (sending, receiving) if side == "both" else (
                        sending if side == "sender" else receiving,
                    )
                    for runtime in affected:
                        for field in missing:
                            runtime["applied"][field] = ["unrelated"] if name == "unrelated-sender-peer" else []
                    sending["applied"]["warnings"] = (
                        ["failed to accept federation peer: invalid argument"] if name == "sender-empty" else []
                    )
                    run = root / name
                    run.mkdir(mode=0o700)
                    publisher = StartGate(run / "ready")
                    published = []
                    try:
                        publisher.acknowledge(name, "publisher")
                        self.assertFalse(publisher.opened())
                        code, text = self.invoke([
                            "--scope", "cross-host", "--sender-status-url", sender + "/status",
                            "--receiver-status-url", receiver + "/status", "--topic", "test",
                            "--sender-api-key-file", str(key), "--receiver-api-key-file", str(key),
                            "--start-gate-file", str(run / "ready"), "--start-gate-session", name,
                            "--publisher-name", "publisher", "--subscriber-name", "subscriber",
                            "--timeout-s", "0.1", "--report-format", "json",
                        ])
                        report = json.loads(text)
                        self.assertTrue(report["publisher_startup_acknowledged"])
                        self.assertTrue(report["publisher_ready"])
                        self.assertTrue(report["subscriber_ready"])
                        self.assertTrue(report["sender_data_plane_ready"])
                        self.assertTrue(report["receiver_data_plane_ready"])
                        self.assertEqual(report["sender_forward_ready_topics"], ["test"])
                        self.assertEqual(report["receiver_inbound_ready_topics"], ["test"])
                        self.assertEqual(report["transport_ready"], expected)
                        self.assertEqual(report["ready"], expected)
                        self.assertEqual(code, 0 if expected else 1)
                        self.assertEqual(publisher.opened(), expected)
                        if publisher.opened():
                            published.append(b"current-run event")
                        self.assertEqual(published, [b"current-run event"] if expected else [])
                    finally:
                        publisher.close()

    def test_redirect_does_not_forward_key(self):
        with secured_status("Bearer secret", "Authorization", {}) as (url, seen):
            with self.assertRaises(urllib.error.HTTPError):
                gate._status_json(url + "/redirect", {"Authorization": "Bearer secret"})
            self.assertEqual(seen, ["Bearer secret"])

    def test_credentials_and_urls_fail_closed_without_disclosing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = self.key_file(root, "key", "secret")
            for mode, content in ((0o644, "secret"), (0o600, "secret\nInjected: header"), (0o600, "")):
                key.chmod(mode)
                key.write_text(content)
                with self.subTest(mode=mode, content=content):
                    with self.assertRaises(ValueError) as error:
                        gate._credential_headers(str(key), "Authorization")
                    self.assertNotIn("secret", str(error.exception))
            link = root / "link"
            link.symlink_to(key)
            with self.assertRaises(ValueError):
                gate._credential_headers(str(link), "Authorization")
            for url in ("http://secret@localhost/status", "http://localhost/status?key=secret", "http://localhost/status#secret"):
                with self.assertRaises(ValueError) as error:
                    gate._validate_status_url(url)
                self.assertNotIn("secret", str(error.exception))


    def test_explicit_cross_host_requires_receiver_even_with_sender_clock(self):
        with secured_status("Bearer secret", "Authorization", {"forward_ready_topics": ["test"]}, {"enabled": True, "synced": True, "role": "leader"}) as (sender, seen):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                key = self.key_file(root, "key", "secret")
                for flags in ([], ["--require-time-sync"]):
                    code, text = self.invoke([
                        "--sender-status-url", sender + "/status", "--sender-api-key-file", str(key),
                        "--scope", "cross-host", "--topic", "test", "--start-gate-file", str(root / "ready"), *flags,
                    ])
                    self.assertEqual(code, 2)
                    self.assertIn("requires --receiver-status-url", text)
                    self.assertFalse((root / "ready").exists())
                self.assertEqual(seen, [])

    def test_scope_and_both_endpoint_readiness_and_clocks(self):
        accepted = {"enabled": True, "synced": True, "role": "leader"}
        sender = {"stamped_stream_runtime_status": {"local_host": "sender", "runtime": native_runtime("receiver", forward_ready_topics=["test"], local_subscription_topics=["test"])}, "time_sync_status": accepted}
        receiver = {"stamped_stream_runtime_status": {"local_host": "receiver", "runtime": native_runtime("sender", inbound_ready_topics=["test"])}, "time_sync_status": accepted}
        cases = [
            ("cross-host", True, sender, receiver, True, 2),
            ("auto", True, sender, receiver, True, 2),
            ("cross-host", True, sender, {**receiver, "time_sync_status": {"enabled": True, "synced": False}}, False, 2),
            ("cross-host", True, {**sender, "time_sync_status": {"enabled": False}}, receiver, False, 2),
            ("cross-host", True, sender, {**receiver, "stamped_stream_runtime_status": {"runtime": {}}}, False, 2),
            ("cross-host", True, {**sender, "stamped_stream_runtime_status": {"runtime": {}}}, receiver, False, 2),
            ("local", False, sender, None, True, 1),
            ("auto", False, sender, None, True, 1),
        ]
        for scope, remote, sending, receiving, ready, clock_count in cases:
            with self.subTest(scope=scope, remote=remote, ready=ready, sending=sending, receiving=receiving):
                flags = ["--scope", scope, "--topic", "test", "--require-time-sync", "--timeout-s", "0.1", "--report-format", "json"]
                if remote:
                    flags += ["--receiver-status-url", "http://receiver/status"]
                with patch.object(gate, "_status_json", side_effect=lambda url, headers: receiving if "receiver" in url else sending):
                    code, text = self.invoke(flags)
                report = json.loads(text)
                self.assertEqual(code, 0 if ready else 1)
                self.assertEqual(report["ready"], ready)
                self.assertEqual(len(report["time_sync_checks"]), clock_count)
                self.assertEqual(report["latency_evidence_accepted"], ready)

    def test_explicit_fallback_stays_diagnostic_in_stdout_and_report_with_clocks(self):
        for clocks_ready in (True, False):
            for output_format in ("json", "text"):
                with self.subTest(clocks_ready=clocks_ready, output_format=output_format), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    clock = {"enabled": True, "synced": clocks_ready, "role": "leader"}
                    sender = {"stamped_stream_runtime_status": {
                        "local_host": "sender",
                        "runtime": native_runtime("receiver", remote_subscribe_topics=["test"], forward_ready_topics=["test"]),
                    }, "time_sync_status": clock}
                    sender["subsystem_status"] = {"publisher": {"publishing": ["test"]}}
                    publisher = StartGate(root / "ready")
                    publisher.acknowledge("run", "publisher")
                    self.addCleanup(publisher.close)
                    receiver = {"stamped_stream_runtime_status": {
                        "local_host": "receiver",
                        "runtime": native_runtime("sender", inbound_topics=["test"], inbound_ready_topics=["test"]),
                    }, "time_sync_status": clock}
                    with patch.object(gate, "_status_json", side_effect=lambda url, headers: receiver if "receiver" in url else sender):
                        code, text = self.invoke([
                            "--scope", "cross-host", "--receiver-status-url", "http://receiver/status",
                            "--topic", "test", "--require-time-sync", "--allow-topology-fallback", "--timeout-s", "0.1",
                            "--start-gate-file", str(root / "ready"), "--report-path", str(root / "report.json"),
                            "--start-gate-session", "run", "--publisher-name", "publisher",
                            "--report-format", output_format,
                        ])
                    report = json.loads((root / "report.json").read_text())
                    self.assertEqual(code, 1)
                    self.assertFalse(report["ready"])
                    self.assertTrue(report["transport_ready"])
                    self.assertFalse(report["latency_evidence_accepted"])
                    self.assertEqual(report["time_sync_ready"], clocks_ready)
                    self.assertEqual(len(report["time_sync_checks"]), 2)
                    self.assertTrue(report["sender_topology_fallback_used"])
                    self.assertTrue(report["receiver_topology_fallback_used"])
                    self.assertFalse(publisher.opened())
                    self.assertFalse((root / "ready").exists())
                    if output_format == "json":
                        self.assertEqual(json.loads(text), report)
                    else:
                        self.assertIn("one-way latency evidence: diagnostic-only", text)

    def ready_status(self, *, registered=True):
        status = {"stamped_stream_runtime_status": {"runtime": {"local_subscription_topics": ["test"]}}}
        if registered:
            status["subsystem_status"] = {"publisher": {"publishing": ["test"]}}
        return status

    def strict_args(self, path, *, session="run", publisher="publisher"):
        return ["--scope", "local", "--topic", "test", "--timeout-s", "0.1",
                "--start-gate-file", str(path), "--start-gate-session", session,
                "--publisher-name", publisher, "--report-format", "json"]

    def one_snapshot(self, args, status):
        with patch.object(gate, "_status_json", return_value=status), \
                patch.object(gate.time, "monotonic", side_effect=[0.0, 0.0, 1.0]), \
                patch.object(gate.time, "sleep"):
            return self.invoke(args)

    # runtime.applied as published by the federation runtime with subscription
    # acknowledgement (FederationRuntimeReport::extend_applied_report).
    @staticmethod
    def acked_runtime(peer_id, *, synced=True, pending=None, incompatible=False, **topics):
        applied = {
            "active_peers": [] if incompatible else [peer_id],
            "connected": [],
            "subscriptions_synced": [peer_id] if synced else [],
            "subscription_ack_pending": [] if pending is None else [{"peer_id": peer_id, **pending}],
            "security_mode_incompatible": [peer_id] if incompatible else [],
            "connect_backoff": [],
            "data_plane": {"local_event_subscriptions": 1, "inbound_sink_count": 1},
        }
        return {"stamped_stream_runtime_status": {"local_host": None, "runtime": {**topics, "applied": applied}}}

    def cross_host_report(self, sender_applied, output_format="json"):
        sender = self.acked_runtime("receiver", forward_ready_topics=["test"], **sender_applied)
        sender["stamped_stream_runtime_status"]["local_host"] = "sender"
        receiver = self.acked_runtime("sender", inbound_ready_topics=["test"])
        receiver["stamped_stream_runtime_status"]["local_host"] = "receiver"
        statuses = {"http://sender/status": sender, "http://receiver/status": receiver}
        args = ["--scope", "cross-host", "--sender-status-url", "http://sender/status",
                "--receiver-status-url", "http://receiver/status", "--topic", "test", "--timeout-s", "0.1",
                "--report-format", output_format]
        with patch.object(gate, "_status_json", side_effect=lambda url, headers=None: statuses[url]), \
                patch.object(gate.time, "monotonic", side_effect=[0.0, 0.0, 1.0]), \
                patch.object(gate.time, "sleep"):
            return self.invoke(args)

    def test_cross_host_not_ready_reports_the_acknowledgement_reason(self):
        cases = [
            ({"synced": False, "pending": {"generation": 3, "pending_ms": 420}}, "subscription_ack_pending"),
            ({"synced": False, "incompatible": True}, "security_mode_incompatible"),
        ]
        for applied, reason in cases:
            with self.subTest(reason=reason):
                code, text = self.cross_host_report(applied)
                report = json.loads(text)
                self.assertEqual(code, 1)
                self.assertFalse(report["transport_ready"])
                sender_check, receiver_check = report["transport_checks"]
                self.assertFalse(sender_check["ready"])
                self.assertTrue(sender_check["reason"].startswith(reason + ":"), sender_check["reason"])
                self.assertTrue(receiver_check["ready"])
                self.assertEqual(receiver_check["reason"], "")
                if reason == "subscription_ack_pending":
                    self.assertEqual(sender_check["subscription_ack_pending"], {"generation": 3, "pending_ms": 420})
        code, text = self.cross_host_report({"synced": False, "incompatible": True}, output_format="text")
        self.assertIn("transport sender: not ready toward receiver: security_mode_incompatible", text)

    def test_cross_host_acknowledged_peers_are_ready_without_reason(self):
        code, text = self.cross_host_report({})
        report = json.loads(text)
        self.assertEqual(code, 0)
        self.assertTrue(report["transport_ready"])
        self.assertEqual([check["reason"] for check in report["transport_checks"]], ["", ""])

    def test_local_readiness_ignores_peer_ids_named_like_topics(self):
        # applied.subscriptions_synced lists peer ids; a peer named like the topic is not a subscription.
        local = ["--scope", "local", "--topic", "test", "--timeout-s", "0.1", "--report-format", "json"]
        peer_named_like_topic = {"stamped_stream_runtime_status": {"runtime": {
            "local_subscription_topics": [], "applied": {"subscriptions_synced": ["test"]}}}}
        code, text = self.one_snapshot(local, peer_named_like_topic)
        report = json.loads(text)
        self.assertEqual(code, 1)
        self.assertFalse(report["sender_ready"])
        self.assertEqual(report["sender_topics"], [])
        subscribed = {"stamped_stream_runtime_status": {"runtime": {"local_subscription_topics": [["test", "bytes"]]}}}
        code, text = self.one_snapshot(local, subscribed)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(text)["sender_ready"])

    def test_existing_ready_path_cannot_authorize_publisher_paused_before_constructor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ready"
            paused, construct, acknowledged, gate_created = (threading.Event() for _ in range(4))
            callbacks, errors = [], []

            def publisher():
                consumer = None
                try:
                    paused.set()
                    if not construct.wait(5):
                        raise AssertionError("constructor was not released")
                    consumer = StartGate(path)
                    consumer.acknowledge("run", "publisher")
                    if consumer.opened():
                        raise AssertionError("gate preceded current-run registration")
                    acknowledged.set()
                    if not gate_created.wait(5):
                        raise AssertionError("helper did not authorize this publisher")
                    if consumer.opened():
                        callbacks.append(("run", "test"))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if consumer is not None:
                        consumer.close()

            worker = threading.Thread(target=publisher)
            worker.start()
            try:
                self.assertTrue(paused.wait(5))
                code, text = self.one_snapshot(self.strict_args(path), self.ready_status())
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(text)["publisher_startup_acknowledged"])
                self.assertFalse(path.exists())
                self.assertEqual(callbacks, [])
                construct.set()
                self.assertTrue(acknowledged.wait(5))
                code, text = self.one_snapshot(self.strict_args(path), self.ready_status())
                self.assertEqual(code, 0)
                self.assertTrue(json.loads(text)["publisher_startup_acknowledged"])
                gate_created.set()
            finally:
                construct.set()
                gate_created.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(callbacks, [("run", "test")])

    def test_missing_previous_session_wrong_name_exit_and_missing_registration_fail_closed(self):
        for case in ("missing", "previous-session", "wrong-name", "exit", "unregistered", "wrong-topic"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ready"
                publisher = StartGate(path)
                try:
                    if case != "missing":
                        publisher.acknowledge("previous" if case == "previous-session" else "run",
                                              "other" if case == "wrong-name" else "publisher")
                    if case == "exit":
                        publisher.close()
                    status = self.ready_status(registered=case != "unregistered")
                    if case == "wrong-topic":
                        status["subsystem_status"]["publisher"]["publishing"] = ["other"]
                    code, text = self.one_snapshot(self.strict_args(path), status)
                    self.assertEqual(code, 1)
                    self.assertFalse(path.exists())
                    self.assertFalse(json.loads(text)["latency_evidence_accepted"])
                finally:
                    publisher.close()

    def test_diagnostic_name_remains_optional_observation_without_gate(self):
        for registered in (False, True):
            with self.subTest(registered=registered):
                code, _ = self.one_snapshot(["--scope", "local", "--topic", "test", "--publisher-name", "publisher"],
                                            self.ready_status(registered=registered))
                self.assertEqual(code, 0)

    def test_strict_gate_requires_identity_and_protects_ack_report_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ready"
            for flags in (
                ["--start-gate-file", str(path)],
                ["--start-gate-file", str(path), "--start-gate-session", "run"],
                ["--start-gate-session", "run"],
                [*self.strict_args(path), "--report-path", str(path) + ".ack"],
            ):
                code, _ = self.invoke(flags)
                self.assertEqual(code, 2)
                self.assertFalse(path.exists())
                self.assertFalse(Path(str(path) + ".ack").exists())

    def test_invalid_gate_has_no_python_subsystem_registration_side_effects(self):
        calls = []
        sdk = types.ModuleType("r2kernel.wrapper.subsystem")
        sdk.Subsystem = type("Subsystem", (), {"__init__": lambda self, **kwargs: calls.append(kwargs)})
        runtime = types.ModuleType("runtime")
        for name in ("default_manifest_path", "default_user_data_path", "ensure_control_running",
                     "format_python_import_error", "run_subsystem"):
            setattr(runtime, name, lambda *args, **kwargs: None)
        spec = importlib.util.spec_from_file_location("gate_test_publisher", Path(__file__).with_name("publisher.py"))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"r2kernel.wrapper.subsystem": sdk, "runtime": runtime}):
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ready"
            path.touch(mode=0o600)
            before = path.stat()
            with self.assertRaises(ValueError):
                module.PublishSubsystem(start_gate_file=str(path), start_gate_session="run")
            self.assertEqual(calls, [])
            self.assertEqual(path.stat(), before)
            self.assertFalse(Path(str(path) + ".ack").exists())


class FreshStartGateTest(unittest.TestCase):
    def test_fresh_consumer_waits_then_accepts_exclusive_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ready"
            consumer = StartGate(path)
            producer = StartGate(path)
            consumer.acknowledge("run", "publisher")
            self.addCleanup(consumer.close)
            self.assertFalse(consumer.opened())
            previous_umask = os.umask(0o777)
            try:
                producer.open("run", "publisher")
            finally:
                os.umask(previous_umask)
            self.assertTrue(consumer.opened())
            self.assertEqual(path.stat().st_mode & 0o7777, 0o600)
            with self.assertRaises(StartGateError) as caught:
                producer.open("run", "publisher")
            self.assertEqual(caught.exception.code, "gate_exists")
            with self.assertRaises(ValueError):
                StartGate(path)

    def test_preexisting_and_unsafe_gates_are_refused_without_modification(self):
        for kind in ("file", "directory", "symlink", "fifo", "hardlink", "content", "public"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "ready"
                consumer = StartGate(path)
                if kind == "directory":
                    path.mkdir(mode=0o700)
                elif kind == "symlink":
                    path.symlink_to(root / "absent")
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.write_text("stale" if kind == "content" else "")
                    path.chmod(0o644 if kind == "public" else 0o600)
                    if kind == "hardlink":
                        os.link(path, root / "other-link")
                before = path.lstat()
                with self.assertRaises(ValueError):
                    StartGate(path)
                if kind != "file":
                    with self.assertRaises(ValueError):
                        consumer.opened()
                self.assertEqual(path.lstat(), before)

    def test_private_real_parent_required_and_replacement_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "run"
            parent.mkdir(mode=0o700)
            path = parent / "ready"
            for mode in (0o755, 0o770):
                parent.chmod(mode)
                with self.assertRaises(ValueError):
                    StartGate(path)
            parent.chmod(0o700)
            link = root / "linked-run"
            link.symlink_to(parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                StartGate(link / "ready")
            consumer = StartGate(path)
            parent.rename(root / "old-run")
            parent.mkdir(mode=0o700)
            with self.assertRaises(ValueError):
                consumer.opened()
            with self.assertRaises(ValueError):
                consumer.open("run", "publisher")


if __name__ == "__main__":
    unittest.main()
