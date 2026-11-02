from __future__ import annotations

from enum import Enum


class DeviceState(Enum):
    CREATED = "Created"
    BOOTING = "Booting"
    REGISTERING = "Registering"
    DISCONNECTED = "Disconnected"
    WAITING_BINDING = "WaitingBinding"
    WAITING_TARGET = "WaitingTarget"
    READY = "Ready"
    OPERATING = "Operating"
    PAUSING = "Pausing"
    PAUSED = "Paused"
    RESUMING = "Resuming"
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    RESETTING = "Resetting"
    DEGRADED = "Degraded"
    FAULTED = "Faulted"
    ERROR = "Error"
