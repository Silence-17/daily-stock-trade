# -*- coding: utf-8 -*-
"""Run or inspect the two pure-forward hotspot paper campaigns."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.hotspot_0945_paper_service import Hotspot0945PaperService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("auto", "watch-exit", "open", "close", "status"),
        default="auto",
    )
    parser.add_argument("--setup", action="store_true", help="Create both isolated accounts without trading.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service = Hotspot0945PaperService()
    payload = service.setup() if args.setup else service.run(args.phase)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
