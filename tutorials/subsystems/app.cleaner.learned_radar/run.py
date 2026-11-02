#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
TARGET = PACKAGE_ROOT / "learned_radar_app.py"

sys.argv = [str(TARGET), *sys.argv[1:]]
exec(TARGET.read_text(), {"__name__": "__main__", "__file__": str(TARGET)})
