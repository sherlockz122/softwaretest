"""Guard the configured project volume; never create a fallback on another drive."""

import os
import shutil
import stat
from pathlib import Path
from uuid import UUID

from packages.repositories.safety import RepositoryError

GIB = 1024**3


def safe_path(path):
    path = Path(path).absolute()
    for ancestor in [*reversed(path.parents), path]:
        if ancestor.exists() or ancestor.is_symlink():
            info = ancestor.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise RepositoryError(503, "REPOSITORY_STORAGE_UNSAFE")
    return path


def directory_bytes(path):
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        for name in [*directories, *files]:
            item = Path(root) / name
            try:
                safe_path(item)
                info = item.lstat()
            except FileNotFoundError:
                # index-pack atomically renames temporary files while Git runs.
                # A disappeared entry is counted on the next scan under its new name.
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


class Storage:
    def __init__(self, settings, usage=shutil.disk_usage):
        self.settings, self.usage = settings, usage
        self.root = safe_path(settings.repository_storage_root)

    def capacity(self, growth=0):
        if not self.root.is_dir():
            raise RepositoryError(503, "REPOSITORY_STORAGE_UNAVAILABLE")
        if self.usage(self.root).free < 2 * GIB + growth:
            raise RepositoryError(507, "REPOSITORY_STORAGE_LOW")

    def preflight(self):
        # Reserve clone and index-pack peak before accepting work, not only at the last 2 GiB.
        self.capacity(2 * self.settings.repository_max_bytes)

    def attempt(self, repository_id, token):
        repository_id, token = str(UUID(repository_id)), str(UUID(token))
        base = safe_path(self.root / "objects" / repository_id)
        base.mkdir(parents=True, exist_ok=True)
        path = safe_path(base / token)
        path.mkdir(exist_ok=False)
        return path

    def remove(self, path):
        path = safe_path(path)
        if (
            not path.is_relative_to(self.root / "objects")
            or len(path.relative_to(self.root).parts) != 3
        ):
            raise RepositoryError(503, "REPOSITORY_STORAGE_UNSAFE")
        # Verify both UUID components before deleting only this execution's attempt.
        UUID(path.parent.name)
        UUID(path.name)

        def remove_readonly(function, item, error):
            # Git for Windows makes pack/index files read-only. Change only the
            # failed file inside this validated attempt, never ACLs or ancestors.
            item = safe_path(item)
            if os.name != "nt" or not isinstance(error[1], PermissionError):
                raise error[1]
            if not item.is_relative_to(path):
                raise RepositoryError(503, "REPOSITORY_STORAGE_UNSAFE")
            item.chmod(item.stat().st_mode | stat.S_IWRITE)
            function(item)

        shutil.rmtree(path, onerror=remove_readonly)
