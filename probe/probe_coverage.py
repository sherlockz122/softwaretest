# -*- coding: utf-8 -*-
"""
探针 ⑧：从 gaps.csv 重算任意观察窗口 W 的覆盖率（秒级完成）

背景：探针结论表只给了 6/12/24/36 个月四个点，无法回答「产品默认 W=90 天会怎样」。
       gaps.csv 里有逐条 gap_days，可以按任意 W 重算。

数据：probe/out/gaps.csv（由 probe_szz.py 产出，列 gap_days,is_noise,level,intro_sha,is_test）

口径（与探针结论表对齐，已对账）：
      只取非语义行命中（is_noise=0）的首次修复，按引入提交去重。
      该口径下 12 / 24 / 36 个月复现为 54.3% / 68.1% / 73.9%，与结论表一致；
      6 个月复现 43.4%，与结论表 43.7% 差 0.3 个百分点，疑为原计算的边界取整。

用法：
    python probe_coverage.py
    python probe_coverage.py <gaps.csv>
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
path = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else ROOT / "out" / "gaps.csv"
if not path.exists():
    sys.exit(f"[X] 找不到 {path}\n    用法：python probe_coverage.py <gaps.csv>")

rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
line = [r for r in rows if r["level"] == "line"]
commit = [r for r in rows if r["level"] == "commit"]
print(f"[i] {path.name}：{len(rows)} 行（line {len(line)} / commit {len(commit)}）")

# 全量去重
first_all = defaultdict(lambda: float("inf"))
# 仅非语义行命中（结论表口径）
first_sem = defaultdict(lambda: float("inf"))
test_flag = {}
for r in commit:
    sha = r["intro_sha"]
    if not sha:
        continue
    g = float(r["gap_days"])
    if g < first_all[sha]:
        first_all[sha] = g
    if r["is_noise"] == "0" and g < first_sem[sha]:
        first_sem[sha] = g
    test_flag[sha] = r.get("is_test", "")

allv = sorted(first_all.values())
semv = sorted(first_sem.values())
print(f"    BIC：全量去重 {len(allv)}，仅非语义行命中 {len(semv)}")
print(f"    gap 中位数 {semv[len(semv) // 2]:.1f} 天，最长 {semv[-1]:.1f} 天")

print("\n=== 与探针结论表对账（记 6mo 43.7 / 12mo 54.3 / 24mo 68.1 / 36mo 73.9）===")
for w, label in ((180, "6 个月"), (365, "12 个月"), (730, "24 个月"), (1095, "36 个月")):
    a = sum(1 for g in allv if g <= w) / len(allv) * 100
    b = sum(1 for g in semv if g <= w) / len(semv) * 100
    print(f"  {label:>7}   全量去重 {a:5.1f}%   仅非语义行 {b:5.1f}%")

MARKS = [
    (30, "1 个月"), (60, "2 个月"), (90, "3 个月"), (120, "4 个月"),
    (180, "6 个月"), (270, "9 个月"), (365, "12 个月"), (540, "18 个月"),
    (730, "24 个月"), (1095, "36 个月"),
]
print("\n=== 提交级覆盖率（仅非语义行口径，全文引用以此为准）===")
n = len(semv)
for w, label in MARKS:
    cov = sum(1 for g in semv if g <= w)
    star = "  <-- 产品当前默认" if w == 90 else ""
    print(f"  W={w:5d} 天 ({label:>7})   覆盖 {cov:5d} / {n} = {cov / n * 100:5.1f}%   未覆盖 {100 - cov / n * 100:5.1f}%{star}")

print("\n=== 反推：目标覆盖率需要多长窗口 ===")
for target in (0.60, 0.70, 0.75, 0.80):
    need = next((b for b in semv if sum(1 for g in semv if g <= b) / n >= target), None)
    if need is None:
        print(f"  覆盖 {target * 100:.0f}% -> 数据内达不到")
    else:
        print(f"  覆盖 {target * 100:.0f}% -> 需要 W ≈ {need:.0f} 天（{need / 30.44:.1f} 个月）")
