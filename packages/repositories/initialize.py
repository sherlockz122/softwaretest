"""Initialize only the dedicated bind root; never recursively change existing data."""

import os

from packages.platform.config import load_settings
from packages.repositories.storage import safe_path


def main():
    root = safe_path(load_settings().repository_storage_root)
    if not root.is_dir():
        raise RuntimeError("Repository bind root unavailable")
    objects = safe_path(root / "objects")
    objects.mkdir(exist_ok=True)
    for path in (root, objects):
        os.chown(path, 10001, 10001)
        os.chmod(path, 0o700)


if __name__ == "__main__":
    main()
