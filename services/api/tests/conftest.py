from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "services" / "api" / "src", ROOT / "core" / "src"):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)
