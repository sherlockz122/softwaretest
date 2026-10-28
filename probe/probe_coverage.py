# -*- coding: utf-8 -*-
"""探针 ⑧：从 gaps.csv 重算任意观察窗口 W 的覆盖率，并按目标覆盖率反推 W。

背景：探针结论表只给了 6/12/24/36 个月四个点，无法回答「产品默认 W=90 天会怎样」。
      gaps.csv 里有逐条 gap_days，可以按任意 W 重算。

数据：gaps.csv（由 probe_szz.py 产出，列 gap_days,is_noise,level,intro_sha,is_test）

口径（与探针结论表对齐，已对账）：
      只取非语义行命中（is_noise=0）的首次修复，按引入提交去重。
      该口径下 12 / 24 / 36 个月复现为 54.3% / 68.1% / 73.9%，与结论表一致；
      6 个月复现 43.4%，与结论表 43.7% 差 0.3 个百分点，疑为原计算的边界取整。

用法：
    python probe_coverage.py
    python probe_coverage.py <gaps.csv>
    python probe_coverage.py <gaps.csv> --target 0.70 --repo <bare.git>
"""
import argparse
import csv
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 目标覆盖率 70% 对应可容忍误标率 30%：即最多允许三成真实缺陷引入提交因窗口不够长被标成 clean。
# 依据见 docs/decisions/2026-10-19-探针参数决策.md 与工作周报-10.30-参数取值依据.md。
DEFAULT_TARGET = 0.70

MARKS = [
    (30, "1 个月"), (60, "2 个月"), (90, "3 个月"), (120, "4 个月"),
    (180, "6 个月"), (270, "9 个月"), (365, "12 个月"), (540, "18 个月"),
    (730, "24 个月"), (1095, "36 个月"),
]


def repo_facts(repo, days):
    """返回（右端被排除的提交数, 全仓提交数, 快照时间, 可观测跨度天数）。"""
    def run(*args):
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)

    head, total = run("log", "-1", "--format=%ct", "HEAD"), run("rev-list", "--count", "HEAD")
    oldest = run("log", "--reverse", "--max-parents=0", "-1", "--format=%ct", "HEAD")
    if head.returncode or total.returncode or oldest.returncode:
        return None
    snapshot = datetime.fromtimestamp(int(head.stdout.strip()), tz=timezone.utc)
    since = (snapshot - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = run("rev-list", "--count", "HEAD", "--since", since)
    if recent.returncode:
        return None
    start = datetime.fromtimestamp(int(oldest.stdout.strip()), tz=timezone.utc)
    return int(recent.stdout.strip()), int(total.stdout.strip()), snapshot, (snapshot - start).days


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gaps", nargs="?", default=str(ROOT / "out" / "gaps.csv"))
    parser.add_argument(
        "--target", type=float, default=DEFAULT_TARGET,
        help=f"目标覆盖率，默认 {DEFAULT_TARGET}（可容忍误标率 30%%）",
    )
    parser.add_argument("--repo", default=None, help="裸库路径；给了就算右端被排除的提交数与占比")
    args = parser.parse_args()

    path = Path(args.gaps).expanduser().resolve()
    if not path.exists():
        sys.exit(f"[X] 找不到 {path}\n    用法：python probe_coverage.py <gaps.csv>")
    if not 0 < args.target < 1:
        sys.exit("[X] --target 必须落在 0 与 1 之间")

    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    line = [r for r in rows if r["level"] == "line"]
    commit = [r for r in rows if r["level"] == "commit"]
    print(f"[i] {path.name}：{len(rows)} 行（line {len(line)} / commit {len(commit)}）")

    first_all = defaultdict(lambda: float("inf"))
    first_sem = defaultdict(lambda: float("inf"))
    for r in commit:
        sha = r["intro_sha"]
        if not sha:
            continue
        g = float(r["gap_days"])
        if g < first_all[sha]:
            first_all[sha] = g
        if r["is_noise"] == "0" and g < first_sem[sha]:
            first_sem[sha] = g

    allv = sorted(first_all.values())
    semv = sorted(first_sem.values())
    print(f"    BIC：全量去重 {len(allv)}，仅非语义行命中 {len(semv)}")
    print(f"    gap 中位数 {semv[len(semv) // 2]:.1f} 天，最长 {semv[-1]:.1f} 天")

    print("\n=== 与探针结论表对账（记 6mo 43.7 / 12mo 54.3 / 24mo 68.1 / 36mo 73.9）===")
    for w, label in ((180, "6 个月"), (365, "12 个月"), (730, "24 个月"), (1095, "36 个月")):
        a = sum(1 for g in allv if g <= w) / len(allv) * 100
        b = sum(1 for g in semv if g <= w) / len(semv) * 100
        print(f"  {label:>7}   全量去重 {a:5.1f}%   仅非语义行 {b:5.1f}%")

    print("\n=== 提交级覆盖率（仅非语义行口径，全文引用以此为准）===")
    n = len(semv)
    for w, label in MARKS:
        cov = sum(1 for g in semv if g <= w)
        star = "  <-- 探针当时的默认" if w == 90 else ""
        print(f"  W={w:5d} 天 ({label:>7})   覆盖 {cov:5d} / {n} = {cov / n * 100:5.1f}%"
              f"   未覆盖 {100 - cov / n * 100:5.1f}%{star}")

    print(f"\n=== 按目标覆盖率 {args.target * 100:.0f}% 标定（可容忍误标率 "
          f"{(1 - args.target) * 100:.0f}%）===")
    need = next((g for g in semv if sum(1 for x in semv if x <= g) / n >= args.target), None)
    if need is None:
        print(f"  数据内达不到 {args.target * 100:.0f}%，最长 gap 只有 {semv[-1]:.0f} 天。"
              f"不用最大值冒充达标。")
        return
    print(f"  W* = {need:.0f} 天 = {need / 30.44:.1f} 个月")
    if args.repo:
        facts = repo_facts(args.repo, need)
        if facts is None:
            print("  [X] 读不到裸库，跳过右端代价与跨度检查")
        else:
            excluded, total, snapshot, span = facts
            print(f"  右端代价：快照前 {need:.0f} 天内的 {excluded} 条提交无法判定标签，"
                  f"占全仓 {total} 条的 {excluded / total * 100:.1f}%"
                  f"（快照 {snapshot:%Y-%m-%d}）")
            if need > span:
                print(f"  达不到：该仓库可观测跨度只有 {span} 天，{need:.0f} 天的窗口会把整段历史排除。"
                      f"按决策规则回退到固定默认值或不标定。")


if __name__ == "__main__":
    main()
