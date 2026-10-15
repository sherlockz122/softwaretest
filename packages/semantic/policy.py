"""Predeclared semantic pilot policy, independent of existing dataset/model rules."""

VERSION = "semantic-roles-v1"
ROLES = ("message", "added", "deleted", "base")
REPOSITORY = "apache/activemq"
MODEL = "microsoft/codebert-base"
REVISION = "3b0952feddeffad0063f274080e3c23d75e7eb39"
TOKENS = 128
WINDOWS = 2
BATCH = 8
THREADS = 4
MAX_FILES = 20
MAX_BLOB = 128 * 1024
MAX_PATCH = 1024**2
MAX_RECORD = 2 * 1024**2
MAX_RSS = 6 * 1024**3
MAX_SECONDS = 2700


class SemanticError(ValueError):
    pass
