"""Install pinned dependencies into the active venv from a prepared wheel cache."""

import argparse
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-cache", type=Path, default=root / ".wheel-cache")
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix:
        parser.error("Run this installer with the virtual environment's Python")
    bootstrap = [line.strip() for line in (root / "requirements-bootstrap.lock").read_text().splitlines() if line.strip() and not line.startswith("#")]
    if len(bootstrap) != 1 or not bootstrap[0].startswith("pip=="):
        parser.error("Expected one pinned pip bootstrap requirement")
    version = bootstrap[0].split("==", 1)[1]
    wheels = list(args.wheel_cache.glob(f"pip-{version}-*.whl"))
    if len(wheels) != 1:
        parser.error("Prepare the documented wheel cache before offline installation")
    sys.path.insert(0, str(wheels[0].resolve()))
    from pip._internal.cli.main import main as pip_main

    return pip_main([
        "--isolated", "install", "--no-index", "--disable-pip-version-check", "--no-cache-dir",
        "--only-binary=:all:", "--find-links", str(args.wheel_cache.resolve()),
        "-r", str(root / "requirements.lock"), "-r", str(root / "requirements-bootstrap.lock"),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
