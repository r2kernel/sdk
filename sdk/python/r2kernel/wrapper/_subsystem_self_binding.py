"""Subsystem self-binding of its own declared requirement (UDS binding_self_set / binding_self_unset)."""

from __future__ import annotations

import socket
import time
from typing import Any

from r2kernel.core.control_client import ControlRequestError, exchange_control_request

_DEFAULT_SELF_BINDING_TIMEOUT_S = 1.0


class _SelfBindingMixin:
    """`bind_requirement` / `unbind_requirement` of Subsystem.

    The backend accepts a self-binding only when ``control.self_binding.mode`` is ``local`` and its policy
    checks pass; a refusal raises :class:`ControlRequestError` with the backend ``reason`` code. The SDK never
    re-requests a self-binding by itself: a self-binding ends with the registration that created it, so call
    ``bind_requirement`` again (for example in ``on_start``) after re-registration.
    """

    def bind_requirement(self, requirement: str, target: str, *, deadline: float | None = None) -> dict[str, Any]:
        """Bind this subsystem's declared requirement to ``target`` (``"<provider_subsystem>.<interface>"``).

        ``deadline`` (keyword-only) is an absolute ``time.monotonic()`` value; without it each socket operation
        times out after 1 s. Expiry raises the built-in ``TimeoutError`` on every supported Python version. Returns the
        installed binding row (``origin == "self"``).
        """
        response = self._self_binding_request("binding_self_set", requirement, deadline, target=target)
        binding = response.get("binding")
        if not isinstance(binding, dict):
            raise ControlRequestError("", f"binding_self_set returned no binding row for {self.name}.{requirement}")
        return binding

    def unbind_requirement(self, requirement: str, *, deadline: float | None = None) -> bool:
        """Remove this subsystem's self-binding of ``requirement``; operator bindings are never touched.

        Returns ``False`` when no self-bound row existed.
        """
        response = self._self_binding_request("binding_self_unset", requirement, deadline)
        removed = response.get("removed")
        if not isinstance(removed, bool):
            raise ControlRequestError("", f"binding_self_unset returned no removed flag for {self.name}.{requirement}")
        return removed

    def _self_binding_request(
        self, command: str, requirement: str, deadline: float | None, **fields: str,
    ) -> dict[str, Any]:
        for name, value in (("requirement", requirement), *fields.items()):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{command}: {name} must be a non-empty string")
        payload = {"cmd": command, "subsystem": self.name, "session_id": self._runtime.session_id,
                   "requirement": requirement, **fields}
        try:
            if deadline is None:
                response = exchange_control_request(payload, timeout_s=_DEFAULT_SELF_BINDING_TIMEOUT_S, retries=1)
            else:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{command} deadline exceeded")
                response = exchange_control_request(payload, deadline=deadline)
        except socket.timeout as exc:
            # Before Python 3.10 socket.timeout is not TimeoutError; raise the documented type explicitly.
            if type(exc) is TimeoutError:
                raise
            raise TimeoutError(f"{command} deadline exceeded") from exc
        if not isinstance(response, dict):
            raise ControlRequestError("", f"{command} returned a malformed response")
        if response.get("status") == "error":
            error = str(response.get("error") or "")
            raise ControlRequestError("", error or f"{command} returned an error without text")
        if response.get("ok") is False:
            binding = response.get("binding")
            raise ControlRequestError(str(response.get("reason") or ""), str(response.get("error") or ""),
                                      binding=binding if isinstance(binding, dict) else None)
        if response.get("ok") is not True or response.get("res") != "ok":
            raise ControlRequestError("", f"{command} returned an unexpected response: {response!r}")
        return response
