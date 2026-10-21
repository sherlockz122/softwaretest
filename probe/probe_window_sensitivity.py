# -*- coding: utf-8 -*-
"""
探针 ⑩：时间窗口敏感性 —— 缺陷引入率随「统计起点」怎么变

问题：产品里的数据窗口有两种语义（绝对日期 vs 最近 N 次提交），定默认值前
      需要知道「换窗口会不会改变标签分布」。

口径（本脚本自定义，与 probe_szz.py 打印的比率不同，见末尾说明）：
    分子 = 引入时间 >= 起点 的去重 BIC 数（is_noise=0 口径，与覆盖率一致）
    分母 = 事件时间 >= 起点 的默认分支提交数（同时给出「仅非合并」变体）

用法：
    python probe/probe_window_sensitivity.py <bare-repo> <gaps.csv> [标签]
"""
import csv
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

YEARS = [2002, 2005, 2008, 2010, 2013, 2015, 2018, 2020, 2022]
RECENT = [1000, 2000, 4000, 8000]


def git(repo, *args):
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode:
        sys.exit(f"[X] git 失败：{args}\n{out.stderr[:400]}")
    return out.stdout


def main():
    repo = Path(sys.argv[1]).expanduser().resolve()
    gaps = Path(sys.argv[2]).expanduser().resolve()
    label = sys.argv[3] if len(sys.argv) > 3 else repo.name
    if not repo.exists() or not gaps.exists():
        sys.exit(f"[X] 路径不存在：{repo} / {gaps}")

    # BIC：按引入提交去重，只取非语义行命中（与覆盖率口径一致）
    first = defaultdict(lambda: float("inf"))
    for row in csv.DictReader(gaps.open(encoding="utf-8-sig")):
        if row["level"] == "commit" and row["intro_sha"] and row["is_noise"] == "0":
            g = float(row["gap_days"])
            if g < first[row["intro_sha"]]:
                first[row["intro_sha"]] = g
    bic_sha = set(first)

    # 引入提交的时间：--all 覆盖默认分支之外的祖先（blame 可能指到 merge 之外的历史）
    stamp = {}
    for line in git(repo, "log", "--all", "--format=%H %at").splitlines():
        parts = line.split()
        if len(parts) == 2:
            stamp[parts[0]] = int(parts[1])
    missing = [s for s in bic_sha if s not in stamp]
    if missing:
        print(f"[!] {len(missing)} 个 BIC 取不到时间，已排除")

    bic_t = sorted(stamp[s] for s in bic_sha if s in stamp)

    # 默认分支提交（含/不含合并）
    commits = []
    for line in git(repo, "log", "HEAD", "--format=%at %P").splitlines():
        parts = line.split()
        if len(parts) >= 1 and parts[0].isdigit():
            commits.append((int(parts[0]), len(parts) - 1))
    commits.sort()

    total = len(commits)
    plain = [(t, p) for t, p in commits if p <= 1]
    print(f"[i] {label}：默认分支提交 {total}（非合并 {len(plain)}），BIC {len(bic_t)} 个")
    if bic_t:
        lo = datetime.fromtimestamp(bic_t[0], tz=timezone.utc).date()
        hi = datetime.fromtimestamp(bic_t[-1], tz=timezone.utc).date()
        print(f"[i] BIC 引入时间范围：{lo} ~ {hi}")

    print("\n=== 按「起始年份」切（分子分母同起点）===")
    print(f"{'起点':>6} {'BIC':>7} {'提交':>7} {'仅非合并':>9} {'引入率':>8} {'仅非合并':>9}")
    earliest_bic = bic_t[0] if bic_t else float("inf")
    earliest_year = (
        datetime.fromtimestamp(earliest_bic, tz=timezone.utc).year
        if bic_t else 9999
    )
    for year in YEARS:
        cut = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        n_bic = sum(1 for t in bic_t if t >= cut)
        n_all = sum(1 for t, _ in commits if t >= cut)
        n_plain = sum(1 for t, p in plain if t >= cut)
        if year < earliest_year:
            # 起点早于该仓库可观测的最早 BIC：分母含起点前提交，而分子缺起点前的 BIC
            print(f"{year:>6} {n_bic:>7} {n_all:>7} {n_plain:>9} {'无效':>8} {'无效':>9}"
                  f"（起点早于最早 BIC {earliest_year}，比率无效）")
            continue
        if n_all == 0 or n_bic == 0:
            print(f"{year:>6} {n_bic:>7} {n_all:>7} {n_plain:>9} {'—':>8} {'—':>9}")
            continue
        print(f"{year:>6} {n_bic:>7} {n_all:>7} {n_plain:>9} "
              f"{n_bic / n_all:>7.1%} {n_bic / n_plain:>8.1%}")

    print("\n=== 按「最近 N 次提交」切（分子分母同窗口）===")
    print(f"{'最近N':>7} {'BIC':>7} {'提交':>7} {'仅非合并':>9} {'引入率':>8} {'仅非合并':>9}")
    for n in RECENT:
        if n > total:
            continue
        cut = commits[total - n][0]
        n_bic = sum(1 for t in bic_t if t >= cut)
        n_all = sum(1 for t, _ in commits if t >= cut)
        n_plain = sum(1 for t, p in plain if t >= cut)
        print(f"{n:>7} {n_bic:>7} {n_all:>7} {n_plain:>9} "
              f"{n_bic / n_all:>7.1%} {n_bic / n_plain:>8.1%}")

    head = commits[-1][0] if commits else 0
    tail = commits[0][0] if commits else 0
    span = (head - tail) / 86400 / 365.25 if head > tail else 0
    print(f"\n[i] 默认分支时间跨度约 {span:.1f} 年")
    print("\n注：probe_szz.py 打印的「缺陷引入率」用 bic_total/全文提交数，"
          "在设定 SINCE 时分子被过滤而分母没过滤，会系统性偏低；本表分子分母同起点。")


if __name__ == "__main__":
    main()
