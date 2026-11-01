"""Where the project lives, resolved once so every module agrees."""
from pathlib import Path

# app/paths.py -> app/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[1]
