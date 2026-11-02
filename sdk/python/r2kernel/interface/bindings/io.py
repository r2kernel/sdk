from __future__ import annotations

from typing import Any


CORE_COMMON_V1 = "msg.core.common.primitive.v1"
IO_DIGITAL_INPUT_API = "api.io.digital.input.v1"
IO_DIGITAL_OUTPUT_API = "api.io.digital.output.v1"
IO_ANALOG_INPUT_API = "api.io.analog.input.v1"
IO_ANALOG_OUTPUT_API = "api.io.analog.output.v1"
DIGITAL_INPUT_GATEWAY_COMPONENT = "api.io.digital.input.v1"
DIGITAL_OUTPUT_GATEWAY_COMPONENT = "api.io.digital.output.v1"
DIGITAL_IO_GATEWAY_COMPONENT = "api.io.digital.output.v1"
ANALOG_INPUT_GATEWAY_COMPONENT = "api.io.analog.input.v1"
ANALOG_OUTPUT_GATEWAY_COMPONENT = "api.io.analog.output.v1"


def _common(message_name: str) -> str:
    return f"{CORE_COMMON_V1}/{message_name}"


class _DeclaredRegistrar:
    """Registers only the procedures the subsystem manifest declares.

    A combined input/output helper serves a subsystem that provides the input
    contract, the output contract or both; registering an undeclared procedure
    is refused by the SDK. A subsystem without manifest checks gets them all.
    """

    def __init__(self, subsystem: Any, helper: str) -> None:
        self._subsystem = subsystem
        self._helper = helper
        declared = getattr(subsystem, "declared_runtime_endpoints", None)
        self._declared = None if declared is None else frozenset(declared("procedures"))
        self._registered = 0

    def _register(self, method: str, name: str, handler: Any, **kwargs: Any) -> None:
        if self._declared is not None and name not in self._declared:
            return
        getattr(self._subsystem, method)(name, handler, **kwargs)
        self._registered += 1

    def register_procedure(self, name: str, handler: Any, **kwargs: Any) -> None:
        self._register("register_procedure", name, handler, **kwargs)

    def register_typed_procedure(self, name: str, handler: Any, **kwargs: Any) -> None:
        self._register("register_typed_procedure", name, handler, **kwargs)

    def finish(self) -> None:
        if not self._registered:
            raise RuntimeError(
                f"{self._helper}: the subsystem manifest declares none of its procedures; "
                "provide an input or output IO contract"
            )


def bind_digital_io_interface(subsystem: Any, driver: Any) -> None:
    registrar = _DeclaredRegistrar(subsystem, "bind_digital_io_interface")

    def get_digital_input(params: dict[str, Any] | None) -> dict[str, bool]:
        index = int((params or {}).get("value", 0))
        return {"value": bool(driver.get_digital_input(index))}

    def set_digital_output(params: dict[str, Any] | None) -> dict[str, bool]:
        payload = params or {}
        index = int(payload.get("index", 0))
        value = bool(payload.get("value", False))
        accepted = bool(driver.set_digital_output(index, value))
        return {"value": accepted}

    def get_digital_output(params: dict[str, Any] | None) -> dict[str, bool]:
        index = int((params or {}).get("value", 0))
        return {"value": bool(driver.get_digital_output(index))}

    registrar.register_procedure(
        "get_digital_input",
        get_digital_input,
        request_type=_common("UInt32Value"),
        response_type=_common("BoolValue"),
    )
    registrar.register_procedure(
        "set_digital_output",
        set_digital_output,
        request_type=_common("IndexedBoolValue"),
        response_type=_common("BoolValue"),
    )
    registrar.register_procedure(
        "get_digital_output",
        get_digital_output,
        request_type=_common("UInt32Value"),
        response_type=_common("BoolValue"),
    )
    registrar.finish()


def bind_digital_input_interface(subsystem: Any, driver: Any) -> None:
    def get_digital_input(params: dict[str, Any] | None) -> dict[str, bool]:
        index = int((params or {}).get("value", 0))
        return {"value": bool(driver.get_digital_input(index))}

    subsystem.register_procedure(
        "get_digital_input",
        get_digital_input,
        request_type=_common("UInt32Value"),
        response_type=_common("BoolValue"),
    )


def bind_digital_output_interface(subsystem: Any, driver: Any) -> None:
    def set_digital_output(params: dict[str, Any] | None) -> dict[str, bool]:
        payload = params or {}
        index = int(payload.get("index", 0))
        value = bool(payload.get("value", False))
        accepted = bool(driver.set_digital_output(index, value))
        return {"value": accepted}

    def get_digital_output(params: dict[str, Any] | None) -> dict[str, bool]:
        index = int((params or {}).get("value", 0))
        return {"value": bool(driver.get_digital_output(index))}

    subsystem.register_procedure(
        "set_digital_output",
        set_digital_output,
        request_type=_common("IndexedBoolValue"),
        response_type=_common("BoolValue"),
    )
    subsystem.register_procedure(
        "get_digital_output",
        get_digital_output,
        request_type=_common("UInt32Value"),
        response_type=_common("BoolValue"),
    )


def bind_digital_io_interface_typed(subsystem: Any, driver: Any) -> None:
    registrar = _DeclaredRegistrar(subsystem, "bind_digital_io_interface_typed")

    def get_digital_input(params: dict[str, Any] | None) -> dict[str, bool]:
        index = int((params or {}).get("value", 0))
        return {"value": bool(driver.get_digital_input(index))}

    def set_digital_output(params: dict[str, Any] | None) -> dict[str, bool]:
        payload = params or {}
        index = int(payload.get("index", 0))
        value = bool(payload.get("value", False))
        accepted = bool(driver.set_digital_output(index, value))
        return {"value": accepted}

    def get_digital_output(params: dict[str, Any] | None) -> dict[str, bool]:
        index = int((params or {}).get("value", 0))
        return {"value": bool(driver.get_digital_output(index))}

    registrar.register_typed_procedure(
        "get_digital_input",
        get_digital_input,
        request_type=_common("UInt32Value"),
        response_type=_common("BoolValue"),
    )
    registrar.register_typed_procedure(
        "set_digital_output",
        set_digital_output,
        request_type=_common("IndexedBoolValue"),
        response_type=_common("BoolValue"),
    )
    registrar.register_typed_procedure(
        "get_digital_output",
        get_digital_output,
        request_type=_common("UInt32Value"),
        response_type=_common("BoolValue"),
    )
    registrar.finish()


def bind_digital_input_interface_typed(subsystem: Any, driver: Any) -> None:
    def get_digital_input(params: dict[str, Any] | None) -> dict[str, bool]:
        index = int((params or {}).get("value", 0))
        return {"value": bool(driver.get_digital_input(index))}

    subsystem.register_typed_procedure(
        "get_digital_input",
        get_digital_input,
        request_type=_common("UInt32Value"),
        response_type=_common("BoolValue"),
    )


def bind_digital_output_interface_typed(subsystem: Any, driver: Any) -> None:
    def set_digital_output(params: dict[str, Any] | None) -> dict[str, bool]:
        payload = params or {}
        index = int(payload.get("index", 0))
        value = bool(payload.get("value", False))
        accepted = bool(driver.set_digital_output(index, value))
        return {"value": accepted}

    def get_digital_output(params: dict[str, Any] | None) -> dict[str, bool]:
        index = int((params or {}).get("value", 0))
        return {"value": bool(driver.get_digital_output(index))}

    subsystem.register_typed_procedure(
        "set_digital_output",
        set_digital_output,
        request_type=_common("IndexedBoolValue"),
        response_type=_common("BoolValue"),
    )
    subsystem.register_typed_procedure(
        "get_digital_output",
        get_digital_output,
        request_type=_common("UInt32Value"),
        response_type=_common("BoolValue"),
    )


def bind_analog_io_interface(subsystem: Any, driver: Any) -> None:
    registrar = _DeclaredRegistrar(subsystem, "bind_analog_io_interface")

    def get_analog_input(params: dict[str, Any] | None) -> dict[str, float]:
        index = int((params or {}).get("value", 0))
        return {"value": float(driver.get_analog_input(index))}

    def set_analog_output(params: dict[str, Any] | None) -> dict[str, bool]:
        payload = params or {}
        index = int(payload.get("index", 0))
        value = float(payload.get("value", 0.0))
        accepted = bool(driver.set_analog_output(index, value))
        return {"value": accepted}

    def get_analog_output(params: dict[str, Any] | None) -> dict[str, float]:
        index = int((params or {}).get("value", 0))
        return {"value": float(driver.get_analog_output(index))}

    registrar.register_procedure(
        "get_analog_input",
        get_analog_input,
        request_type=_common("UInt32Value"),
        response_type=_common("Float32Value"),
    )
    registrar.register_procedure(
        "set_analog_output",
        set_analog_output,
        request_type=_common("IndexedFloat32Value"),
        response_type=_common("BoolValue"),
    )
    registrar.register_procedure(
        "get_analog_output",
        get_analog_output,
        request_type=_common("UInt32Value"),
        response_type=_common("Float32Value"),
    )
    registrar.finish()


def bind_analog_io_interface_typed(subsystem: Any, driver: Any) -> None:
    registrar = _DeclaredRegistrar(subsystem, "bind_analog_io_interface_typed")

    def get_analog_input(params: dict[str, Any] | None) -> dict[str, float]:
        index = int((params or {}).get("value", 0))
        return {"value": float(driver.get_analog_input(index))}

    def set_analog_output(params: dict[str, Any] | None) -> dict[str, bool]:
        payload = params or {}
        index = int(payload.get("index", 0))
        value = float(payload.get("value", 0.0))
        accepted = bool(driver.set_analog_output(index, value))
        return {"value": accepted}

    def get_analog_output(params: dict[str, Any] | None) -> dict[str, float]:
        index = int((params or {}).get("value", 0))
        return {"value": float(driver.get_analog_output(index))}

    registrar.register_typed_procedure(
        "get_analog_input",
        get_analog_input,
        request_type=_common("UInt32Value"),
        response_type=_common("Float32Value"),
    )
    registrar.register_typed_procedure(
        "set_analog_output",
        set_analog_output,
        request_type=_common("IndexedFloat32Value"),
        response_type=_common("BoolValue"),
    )
    registrar.register_typed_procedure(
        "get_analog_output",
        get_analog_output,
        request_type=_common("UInt32Value"),
        response_type=_common("Float32Value"),
    )
    registrar.finish()
