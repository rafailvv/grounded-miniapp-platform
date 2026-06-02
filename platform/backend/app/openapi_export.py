from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from app.main import app


def export_openapi() -> dict[str, object]:
    return app.openapi()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the Upmini AI Studio OpenAPI schema without starting a server.")
    parser.add_argument("--output", "-o", type=Path, help="Output JSON path. Writes to stdout when omitted.")
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)

    payload = json.dumps(export_openapi(), ensure_ascii=False, indent=args.indent, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{payload}\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
