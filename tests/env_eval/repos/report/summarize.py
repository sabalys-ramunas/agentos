#!/usr/bin/env python3
"""Sum a sales file by category.

    python3 summarize.py /work/in/sales.csv /work/out/summary.json

The input has the columns category,amount. The output names the computer it was made on (its host name),
so a summary made anywhere else is told apart.
"""
import csv
import json
import socket
import sys
from collections import defaultdict
from pathlib import Path


def main(src: str, dest: str) -> None:
    totals: dict[str, float] = defaultdict(float)
    rows = 0
    with open(src, newline="") as fh:
        for row in csv.DictReader(fh):
            totals[row["category"]] += float(row["amount"])
            rows += 1
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"rows": rows, "totals": {k: round(v, 2) for k, v in sorted(totals.items())},
                               "made_on": socket.gethostname()}, indent=2) + "\n")
    print(f"wrote {out}: {rows} rows, {len(totals)} categories")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
