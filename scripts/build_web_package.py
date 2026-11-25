"""Build the static React frontend and stage it under ``src/repodocs/web/static`` for wheel/sdist packaging."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist"
STATIC_DIR = ROOT / "src" / "repodocs" / "web" / "static"


def stage_static_assets(*, build_frontend: bool = True) -> Path:
    if build_frontend:
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("npm is required to build frontend assets")
        subprocess.run([npm, "ci", "--ignore-scripts"], cwd=FRONTEND_DIR, check=True)
        subprocess.run([npm, "run", "build"], cwd=FRONTEND_DIR, check=True)
    if not (FRONTEND_DIST / "index.html").is_file():
        raise FileNotFoundError(f"Built frontend not found at {FRONTEND_DIST}")
    if STATIC_DIR.exists():
        shutil.rmtree(STATIC_DIR)
    shutil.copytree(FRONTEND_DIST, STATIC_DIR)
    return STATIC_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and stage frontend assets for packaging.")
    parser.add_argument("--skip-npm", action="store_true", help="Copy existing frontend/dist without running npm.")
    args = parser.parse_args()
    dest = stage_static_assets(build_frontend=not args.skip_npm)
    print(f"Staged frontend assets at {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
