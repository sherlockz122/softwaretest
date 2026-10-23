"""Lightweight prediction contracts shared by the API and the worker executor."""

from packages.mining.public_dataset import NUMERIC_FIELDS

VERSION = "prediction-candidate-v1"

# 同步返回时限：超过就把计算交给异步任务，响应口径与非功能需求的九成五请求一秒内一致。
SYNC_SECONDS = 1.0

# 单个候选改动的输入上限，超限在计算前拒绝并说明当前上限。
MAX_CANDIDATE_FILES = 200
MAX_CANDIDATE_LINES = 20_000
MAX_DIFF_BYTES = 2 * 1024 * 1024

# 批量扫描的范围上限，时间窗与已声明的观察窗口一致。
MAX_BATCH_CANDIDATES = 500
MAX_WINDOW_DAYS = 90

# 超大改动按审查成本固定阈值标识，独立于风险密度展示。
OVERSIZED_EFFORT = 500

# 批量结果沿用特征轮次的惯例，每批十个原子写入。
BATCH_SIZE = 10

# 模型工件声明的研究数据列合同到产品特征名的映射；LT 与 FIX 不在十二维合同内。
COLUMN_FEATURES = {
    "la": "LA",
    "ld": "LD",
    "nf": "NF",
    "nd": "ND",
    "ns": "NS",
    "ent": "Entropy",
    "ndev": "NDEV",
    "age": "AGE",
    "nuc": "NUC",
    "aexp": "EXP",
    "arexp": "REXP",
    "asexp": "SEXP",
}
FEATURE_COLUMNS = {value: key for key, value in COLUMN_FEATURES.items()}
MODEL_COLUMNS = list(NUMERIC_FIELDS)

DISCLAIMER = "因素贡献是相关性排序的结果，不代表缺陷的因果根因。"

# 与训练报告的口径一致：验证集按时间切分，逻辑回归在 sigmoid 输入上校准。
CALIBRATION = "validation-chronological-sigmoid-logit-v1"


class PredictionError(ValueError):
    def __init__(self, code, status=422, reason=None):
        self.code, self.status, self.reason = code, status, reason
        super().__init__(code)
