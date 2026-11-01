"""Entry point kept at the repository root.

The launcher, the supervisor's process records and every "is this our process" check match on
this file path, so the wrapper stays here and the implementation lives in ``app.qwen_service``.
"""
from app.qwen_service import main

if __name__ == "__main__":
    raise SystemExit(main())
