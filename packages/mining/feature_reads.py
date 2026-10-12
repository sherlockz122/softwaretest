"""Attempt-local immutable blob reuse; supervised hits and bounded v2 projections."""

import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select

from packages.mining.features import digest
from packages.persistence.models import FileChange
from packages.repositories.parser import sha_valid
from packages.repositories.safety import RepositoryError

CACHE_BYTES = 8 * 1024**2
CACHE_ENTRIES = 512
PROJECTION_BATCH = 100
FILE_FIELDS = (
    "old_path",
    "new_path",
    "old_blob",
    "new_blob",
    "change_type",
    "insertions",
    "deletions",
    "old_loc",
    "content_status",
    "is_binary",
    "parser_version",
)


def rule_hash():
    return digest(Path(__file__).read_text(encoding="utf-8"))


class BlobCache:
    def __init__(self, reader):
        self.reader, self.original = reader, reader.blob
        self.entries = OrderedDict()
        self.bytes = self.hits = self.misses = self.peak_bytes = self.peak_entries = 0

    def blob(self, sha, mode):
        self.reader.tick()
        if time.monotonic() >= self.reader.deadline:
            raise RepositoryError(422, "REPOSITORY_PARSE_TIMEOUT")
        # The same SHA as a gitlink must never reuse regular-file contents.
        if set(sha) == {"0"} or mode == "160000":
            return self.original(sha, mode)
        sha_valid(sha.encode("ascii"))
        if sha in self.entries:
            self.hits += 1
            self.entries.move_to_end(sha)
            return self.entries[sha]
        self.misses += 1
        value = self.original(sha, mode)
        size = len(value) if value is not None else 0
        if size <= CACHE_BYTES:
            while self.entries and (
                self.bytes + size > CACHE_BYTES or len(self.entries) >= CACHE_ENTRIES
            ):
                _, old = self.entries.popitem(last=False)
                self.bytes -= len(old) if old is not None else 0
            self.entries[sha] = value
            self.bytes += size
            self.peak_bytes = max(self.peak_bytes, self.bytes)
            self.peak_entries = max(self.peak_entries, len(self.entries))
        return value

    @property
    def metrics(self):
        return {
            "hits": self.hits,
            "misses": self.misses,
            "peak_payload_bytes": self.peak_bytes,
            "peak_entries": self.peak_entries,
            "payload_limit_bytes": CACHE_BYTES,
            "entry_limit": CACHE_ENTRIES,
        }


@contextmanager
def cached_blobs(reader):
    cache = BlobCache(reader)
    had_override = "blob" in vars(reader)
    reader.blob = cache.blob
    try:
        yield cache
    finally:
        if had_override:
            reader.blob = cache.original
        else:
            del reader.blob
        cache.entries.clear()
        cache.bytes = 0


def file_projections(db, commits, tick):
    """Bound both result materialization and SQL IN size; keep per-commit ordinals."""
    output, total = {}, 0
    for offset in range(0, len(commits), PROJECTION_BATCH):
        tick()
        identifiers = [commit.id for commit in commits[offset : offset + PROJECTION_BATCH]]
        # LIMIT avoids reading an unbounded corrupted projection before rejecting it.
        rows = db.execute(
            select(
                FileChange.commit_id,
                FileChange.ordinal,
                *(getattr(FileChange, name) for name in FILE_FIELDS),
            )
            .where(FileChange.commit_id.in_(identifiers))
            .order_by(FileChange.commit_id, FileChange.ordinal)
            .limit(10001 - total)
        )
        for row in rows:
            tick()
            total += 1
            if total > 10000:
                raise RepositoryError(422, "FEATURE_RESOURCE_LIMIT")
            output.setdefault(row.commit_id, []).append(row)
    return output
