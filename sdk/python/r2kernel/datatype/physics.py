"""Physics-related datatypes and helpers."""

from __future__ import annotations

from r2kernel.datatype.basetype import NamedFloatVector


class Inertia(NamedFloatVector):
    NAMES = ("ixx", "iyy", "izz", "ixy", "ixz", "iyz")

    def __init__(self, values: list[float] | None = None, *, names: tuple[str, ...] | None = None):
        super().__init__(values, names=names or self.NAMES)


class Force(NamedFloatVector):
    NAMES = ("fx", "fy", "fz")

    def __init__(self, values: list[float] | None = None, *, names: tuple[str, ...] | None = None):
        super().__init__(values, names=names or self.NAMES)


class Torque(NamedFloatVector):
    NAMES = ("tx", "ty", "tz")

    def __init__(self, values: list[float] | None = None, *, names: tuple[str, ...] | None = None):
        super().__init__(values, names=names or self.NAMES)
