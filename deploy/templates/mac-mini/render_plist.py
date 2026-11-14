"""Fill in the placeholders of dev.agentos.controller.plist and write the LaunchAgent.

    python3 render_plist.py TEMPLATE OUTPUT AGENTOS_BIN AGENTOS_HOME LOG_DIR

Only the three placeholders are replaced, and only inside string values. The result is written
with plistlib, so a path with spaces or an ampersand stays a correct XML string.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path


def fill(value, values: dict[str, str]):
    if isinstance(value, str):
        for placeholder, real in values.items():
            value = value.replace(placeholder, real)
        return value
    if isinstance(value, list):
        return [fill(item, values) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, values) for key, item in value.items()}
    return value


def render(template: Path, output: Path, agentos_bin: str, home: str, logs: str) -> None:
    values = {"__AGENTOS_BIN__": agentos_bin, "__AGENTOS_HOME__": home, "__LOG_DIR__": logs}
    with template.open("rb") as f:
        data = plistlib.load(f)
    with output.open("wb") as f:
        plistlib.dump(fill(data, values), f, fmt=plistlib.FMT_XML)


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print("usage: render_plist.py TEMPLATE OUTPUT AGENTOS_BIN AGENTOS_HOME LOG_DIR", file=sys.stderr)
        return 2
    render(Path(argv[1]), Path(argv[2]), argv[3], argv[4], argv[5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
