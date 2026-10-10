"""Immutable collection policy; changes require a new version and migration of windows."""

PARSER_VERSION = "native-pydriller-v1"
IDENTITY_VERSION = "email-normalized-v1"
BATCH_SIZE = 10
BLOB_LIMIT = 1024**2
DIFF_LIMIT = 256 * 1024
METADATA_LIMIT = 1024**2
INDEX_LIMIT = 32 * 1024**2
FILE_INDEX_LIMIT = 4 * 1024**2
PARSE_GROWTH = 512 * 1024**2
