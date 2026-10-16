"""Initialize only the dedicated output root; no recursive ownership changes."""

import os

from packages.mining import public_assets as assets
from packages.mining.research_dataset import OUTPUT


def main():
    root = assets.safe_path(OUTPUT)
    if not root.is_dir():
        raise RuntimeError("Dataset bind root unavailable")
    os.chown(root, 10001, 10001)
    os.chmod(root, 0o700)


if __name__ == "__main__":
    main()
