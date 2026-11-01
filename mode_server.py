"""Entry point kept at the repository root (see qwen_service.py for why).

The gateway itself lives in ``app.mode_server``; this wrapper keeps the process command line,
and therefore the process records, unchanged.
"""
from app.mode_server import main

if __name__ == "__main__":
    raise SystemExit(main())
