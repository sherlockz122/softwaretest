"""Initialize only the dedicated models bind root, preserving existing contents."""

import os

from packages.mining.public_assets import safe_path
from packages.modeling.policy import OUTPUT


def main():
    root = safe_path(OUTPUT)
    if not root.is_dir():
        raise RuntimeError("Model bind root unavailable")
    os.chown(root, 10001, 10001)
    os.chmod(root, 0o700)


if __name__ == "__main__":
    main()
