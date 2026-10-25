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

# 单次解析任务的提交上限。探针实测每提交约 22.9 次受监督的 Git 子进程、单次命令约 80 ms，
# 改动文件多的窗口每提交要 2.44 秒，700 条在 1800 秒的解析预算内留约 5% 余量。
# 该耗时是在未启用在线 v2 blob 缓存的探针上测的，落在保守侧；线上实测后可上调这一个常量。
COMMIT_LIMIT = 700
