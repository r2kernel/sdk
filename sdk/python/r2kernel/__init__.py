"""R2Kernel namespace package."""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

__all__ = [
    "AppSubsystem",
    "CompoundSubsystem",
    "ComponentSubsystem",
    "GatewaySubsystem",
    "R2KernelClient",
    "ResourceBrokerClient",
    "Subsystem",
]


def __getattr__(name):
    if name == "ResourceBrokerClient":
        from .core.resource import ResourceBrokerClient

        return ResourceBrokerClient
    if name == "R2KernelClient":
        from .core.r2kernel_client import R2KernelClient

        return R2KernelClient
    if name in {"AppSubsystem", "CompoundSubsystem", "ComponentSubsystem", "GatewaySubsystem", "Subsystem"}:
        from . import wrapper

        return getattr(wrapper, name)
    raise AttributeError(name)
