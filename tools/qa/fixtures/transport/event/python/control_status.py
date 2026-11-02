"""Authenticated Control status reads and status-row parsing for QA fixtures.

This module is the one implementation shared by ``wait_event_path.py`` and the
private communication-matrix/release QA drivers. It depends on the standard
library only, so a QA driver can run it on a remote host with
``python3 -I -c <this source> ...`` without installing anything.

Command line (one GET request, no retries)::

    python3 control_status.py --url http://127.0.0.1:8001/api/v1/control/status \
        [--api-key-file <owner-only key file>] [--auth-header Authorization]

The API key is read from a file and sent in a request header; it never
appears in a URL or in a process argument list. Exit status: 0 with the
response body on stdout for a 2xx response; otherwise one of the
``EXIT_*`` codes below with the reason on stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

AUTH_HEADERS = ("Authorization", "X-R2Kernel-API-Key")
MAX_API_KEY_BYTES = 8192

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_AUTH_REJECTED = 4
EXIT_HTTP_ERROR = 5


def topic_set(rows: Any) -> set[str]:
    """Topic names of status endpoint rows.

    Accepts the runtime row shapes: a topic string, an SDK heartbeat row
    ``[topic, type, ...]`` (list or tuple) and an object with a ``topic``
    field. Rows of any other shape contribute nothing.
    """
    topics: set[str] = set()
    if not isinstance(rows, (list, tuple)):
        return topics
    for row in rows:
        if isinstance(row, str):
            topics.add(row)
        elif isinstance(row, (list, tuple)) and row and isinstance(row[0], str):
            topics.add(row[0])
        elif isinstance(row, dict):
            topic = row.get("topic")
            if isinstance(topic, str):
                topics.add(topic)
    return topics


def credential_headers(path: str | None, header: str) -> dict[str, str]:
    """Request headers carrying the API key from an owner-only regular file."""
    if not path:
        return {}
    if header not in AUTH_HEADERS:
        raise ValueError(f"auth header must be one of: {', '.join(AUTH_HEADERS)}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ValueError
            raw = source.read(MAX_API_KEY_BYTES + 1)
        if len(raw) > MAX_API_KEY_BYTES:
            raise ValueError
        key = raw.decode("ascii").rstrip("\r\n")
        if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        raise ValueError("credential file must be an owner-only regular file containing one API key") from None
    return {header: f"Bearer {key}" if header == "Authorization" else key}


def validate_status_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("status URL must be HTTP(S) without credentials, query, or fragment")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(url: str, headers: dict[str, str] | None = None, *, timeout_s: float = 2.0) -> dict[str, Any]:
    """GET one JSON object. HTTP errors raise ``urllib.error.HTTPError``."""
    validate_status_url(url)
    request = urllib.request.Request(url, headers=headers or {})
    # Never forward a credential to a redirected endpoint.
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout_s) as response:
        status = json.loads(response.read().decode("utf-8"))
        if not isinstance(status, dict):
            raise ValueError("status response must be an object")
        return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read one R2Kernel Control API JSON document.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--api-key-file", help="Owner-only file containing the API key.")
    parser.add_argument("--auth-header", choices=AUTH_HEADERS, default="Authorization")
    parser.add_argument("--timeout-s", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        headers = credential_headers(args.api_key_file, args.auth_header)
        validate_status_url(args.url)
    except ValueError as exc:
        print(f"control status: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        status = fetch_json(args.url, headers, timeout_s=args.timeout_s)
    except ValueError as exc:
        print(f"control status: invalid response: {exc}", file=sys.stderr)
        return EXIT_HTTP_ERROR
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            hint = "pass an API key file" if not args.api_key_file else "the API key was rejected"
            print(f"control status: HTTP {exc.code} authentication failed ({hint})", file=sys.stderr)
            return EXIT_AUTH_REJECTED
        print(f"control status: HTTP {exc.code}", file=sys.stderr)
        return EXIT_HTTP_ERROR
    except (OSError, urllib.error.URLError) as exc:
        print(f"control status: unreachable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    print(json.dumps(status))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
