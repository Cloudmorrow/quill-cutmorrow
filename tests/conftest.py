"""The renderer's module, importable by the tests as `graph`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
