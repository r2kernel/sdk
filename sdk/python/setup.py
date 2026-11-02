from __future__ import annotations

import os

from setuptools import setup

try:
    from wheel.bdist_wheel import bdist_wheel as _bdist_wheel
except ImportError:  # pragma: no cover - wheel is expected in build envs
    _bdist_wheel = None


cmdclass: dict[str, type] = {}

if _bdist_wheel is not None:

    class bdist_wheel(_bdist_wheel):
        def finalize_options(self) -> None:
            super().finalize_options()
            self.root_is_pure = False
            plat_name = os.environ.get("R2KERNEL_WHEEL_PLAT_NAME")
            if plat_name:
                self.plat_name_supplied = True
                self.plat_name = plat_name

        def get_tag(self):  # type: ignore[no-untyped-def]
            python_tag, abi_tag, plat_tag = super().get_tag()
            plat_name = os.environ.get("R2KERNEL_WHEEL_PLAT_NAME")
            if plat_name:
                plat_tag = plat_name
            if os.environ.get("R2KERNEL_WHEEL_ABI_INDEPENDENT", "1") != "0":
                return "py3", "none", plat_tag
            return python_tag, abi_tag, plat_tag

    cmdclass["bdist_wheel"] = bdist_wheel


setup(cmdclass=cmdclass)
