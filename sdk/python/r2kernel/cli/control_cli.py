"""Placeholder for the control backend CLI entrypoint.

The control backend is now the `r2kernel-control` runtime binary instead of
part of the Python subsystem wrapper package.
"""


def main():
    raise RuntimeError(
        "The control backend CLI no longer lives in `r2kernel-client`. "
        "Use `r2kernel-control --config /opt/r2kernel/runtime/config.yaml` from an installed runtime, "
        "or `<prefix>/runtime/bin/r2kernel-control --config <prefix>/runtime/config.yaml` "
        "for a custom installation prefix."
    )


if __name__ == "__main__":
    main()
