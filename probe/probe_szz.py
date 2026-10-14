# -*- coding: utf-8 -*-
"""
探针 ③：SZZ 回溯 + bug 存活时间统计

用法：
    python probe_szz.py
    python probe_szz.py <仓库路径>
输出：
    控制台摘要 + probe/out/gaps.csv
    CSV 列：gap_days, is_noise, level, intro_sha
      level = line   按删除行计数（原始口径）
      level = commit 按引入提交去重、取最早一次修复（推荐口径）

关键点：
  1) git blame 必须用 --porcelain 拿完整 40 位 sha，否则和 TS 的键对不上
  2) 任何跳过路径都要有计数器，禁止静默 continue
  3) 末尾做一致性校验：所有分支必须加起来等于删除行总数
     （注释/空行是"命中引入提交"的一部分，不单独相加）
  4) 注释/空行与非注释行分开统计
  5) 【重要】按行计数会被批量提交（许可证头、格式化）放大：
     一个批量 fix 删 500 行、全 blame 到同一个老提交 → 500 个相同 gap → 假尖峰。
     所以额外按"引入提交"去重、取最早一次修复，作为推荐口径。
  6) 时间戳统一用作者时间（%at），与 PyDriller 的 author_date 同源，不混用提交者时间
"""
import re
import csv
import sys
import time
import collections
import subprocess
from pathlib import Path

from pydriller import Repository

# ---------------- 路径 ----------------
if len(sys.argv) > 1:
    REPO = Path(sys.argv[1]).expanduser().resolve()
else:
    REPO = Path(r"D:\develop\软件测试实训\jit-defect-prediction\probe\repos\commons-cli.git")

if not REPO.exists():
    sys.exit(f"[X] 找不到仓库：{REPO}\n    用法：python probe_szz.py <仓库路径>")

PROBE = REPO.parent.parent                 # .../probe
OUT_DIR = PROBE / "out"

MAXFIX = 400                               # 全量：commons-cli 共约 400 个 fix 提交
SHOW_SAMPLES = 10                          # 打印前 N 条删除行样本（看格式用）

FIX_RE = re.compile(
    r"\b(fix(e[sd])?|bug|patch|defect|resolve[sd]?|close[sd]?)\b", re.I
)

print(f"[i] 仓库：{REPO}")
print(f"[i] 输出：{OUT_DIR / 'gaps.csv'}")
print(f"[i] 上限：{MAXFIX} 个 fix 提交\n")


# ---------------- git 封装 ----------------
def git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


# sha -> 作者时间戳（%at，与 PyDriller 的 author_date 同源，避免字段混用）
TS = {}
for line in git("log", "--all", "--format=%H %at").splitlines():
    line = line.strip()
    if line:
        h, t = line.split()[:2]
        TS[h] = int(t)

# 默认分支提交总数（算缺陷引入率用）
_c = git("rev-list", "--count", "HEAD").strip()
TOTAL_COMMITS = int(_c) if _c.isdigit() else 0

# sha -> 父提交个数（0 表示仓库初始提交）
PARENTS = {}
for line in git("log", "--all", "--format=%H %P").splitlines():
    line = line.strip()
    if line:
        p = line.split()
        PARENTS[p[0]] = len(p) - 1

print(f"[i] 建立了 {len(TS)} 条 sha->时间 的映射")
print(f"[i] 默认分支提交数：{TOTAL_COMMITS}")
print(f"[i] 初始提交（无父提交）：{sum(1 for v in PARENTS.values() if v == 0)} 个\n")


def blame(rev, path, lineno):
    """返回 (引入提交的完整 sha, 是否边界提交)。拿不到返回 (None, False)。"""
    out = git("blame", "--porcelain", "-L", f"{lineno},{lineno}", rev, "--", path)
    if not out.strip():
        return None, False

    lines = out.split("\n")
    sha = lines[0].split()[0]

    # 硬校验：不是 40 位就当场炸，绝不让它悄悄变成 0
    if len(sha) != 40:
        raise RuntimeError(
            f"blame 返回的不是完整 sha：{sha!r}（{len(sha)} 位）\n"
            f"rev={rev} path={path} line={lineno}"
        )

    # 边界提交 = blame 追到了仓库的初始提交（没有父提交）
    # 注意：不能用 porcelain 的 boundary 标记 —— 那是给 revision range 用的，
    # 单点 git blame <rev> 永远不会输出它（实测恒为 0）。
    is_boundary = PARENTS.get(sha, -1) == 0

    return sha, is_boundary


def is_noise(txt):
    """注释行 / 空行。注意：只计数，不拦截。"""
    s = (txt or "").strip()
    return (not s) or s.startswith("//") or s.startswith("*") or s.startswith("/*")


# ---------------- 主循环 ----------------
gaps = []          # 【行级】非注释/空行
gaps_noise = []    # 【行级】注释/空行

first_fix_code = {}    # 【提交级】引入 sha -> 最早被修的时间戳（代码行命中）
first_fix_noise = {}   # 【提交级】引入 sha -> 最早被修的时间戳（注释行命中）

del_total = 0      # 删除行总数（.java，且 diff 能解析）
noise = 0          # 其中注释/空行
noblame = 0        # blame 拿不到输出
boundary = 0       # 边界提交（追到仓库初始提交）
selfskip = 0       # blame 命中 fix 提交自己
nots = 0           # sha 不在 TS 里
neg = 0            # 负间隔（时钟异常 / rebase）
samples_shown = 0
fixes = 0
hit_cap = False    # 是否因为达到 MAXFIX 上限而提前结束

t0 = time.perf_counter()

for c in Repository(str(REPO)).traverse_commits():
    if not FIX_RE.search(c.msg or ""):
        continue
    if len(c.parents) != 1:          # 跳过合并提交（它们没有 diff）
        continue

    if fixes >= MAXFIX:
        hit_cap = True
        break
    fixes += 1

    if fixes % 25 == 0:
        print(f"  ...fix {fixes:4d}   {time.perf_counter() - t0:7.1f}s   "
              f"行命中 {len(gaps) + len(gaps_noise)}")

    parent = c.parents[0]            # PyDriller 的 parents 是 sha 字符串
    fix_ts = c.author_date.timestamp()

    for m in c.modified_files:
        if not (m.new_path or "").endswith(".java"):
            continue
        old = m.old_path or m.new_path
        if not old:
            continue
        try:
            deleted = m.diff_parsed["deleted"]
        except Exception:
            continue
        if not deleted:
            continue

        for lineno, txt in deleted:
            del_total += 1

            if samples_shown < SHOW_SAMPLES:
                print(f"    [样本] lineno={lineno!r}  txt={txt!r}")
                samples_shown += 1

            noisy = is_noise(txt)
            if noisy:
                noise += 1           # 只计数，不 continue

            sha, is_b = blame(parent, old, lineno)

            if sha is None:
                noblame += 1
            elif is_b:
                boundary += 1
            elif sha == c.hash:
                selfskip += 1
            else:
                t_intro = TS.get(sha)
                if t_intro is None:
                    nots += 1        # 不再静默
                    continue
                gap = (fix_ts - t_intro) / 86400.0
                if gap < 0:
                    neg += 1
                else:
                    if noisy:
                        gaps_noise.append(gap)
                        bucket = first_fix_noise
                    else:
                        gaps.append(gap)
                        bucket = first_fix_code
                    # 提交级去重：同一个引入提交只保留"最早被修"的那次
                    if sha not in bucket or fix_ts < bucket[sha]:
                        bucket[sha] = fix_ts

dt = time.perf_counter() - t0


# ---------------- 汇总 ----------------
def to_gaps(d):
    """引入 sha -> 最早 fix 时间戳，转成 gap（天）"""
    out = []
    for sha, ts in d.items():
        t_intro = TS.get(sha)
        if t_intro is not None and ts >= t_intro:
            out.append((ts - t_intro) / 86400.0)
    return sorted(out)


commit_gaps = to_gaps(first_fix_code)
commit_noise_only = to_gaps({s: v for s, v in first_fix_noise.items()
                             if s not in first_fix_code})

gaps.sort()
gaps_noise.sort()


def report(name, arr):
    if not arr:
        print(f"\n{name}：无数据")
        return
    print(f"\n{name}（{len(arr)} 条）")
    for p in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95):
        v = arr[min(len(arr) - 1, int(len(arr) * p))]
        print(f"  P{int(p * 100):<3d} = {v:9.1f} 天  ({v / 30.4:7.1f} 个月)")
    print(f"  max  = {arr[-1]:9.1f} 天  ({arr[-1] / 30.4:7.1f} 个月)")
    same = sum(1 for g in arr if g < 1.0)
    print(f"  <1 天 = {same} 条 ({same / len(arr):.1%})")


print(f"\n{'=' * 68}")
print(f"扫了 {fixes} 个 fix 提交，耗时 {dt:.1f}s")
print(f"{'=' * 68}")
print(f"删除行总数（.java）     {del_total}")
if del_total:
    print(f"  |- 注释/空行          {noise}  ({noise / del_total:.1%})")
print(f"  |- 命中引入提交       {len(gaps) + len(gaps_noise)}")
print(f"  |- 边界提交           {boundary}")
print(f"  |- blame 命中自己     {selfskip}")
print(f"  |- blame 无输出       {noblame}")
print(f"  |- sha 不在映射表     {nots}")
print(f"  \\- 负间隔             {neg}")

parts = len(gaps) + len(gaps_noise) + boundary + selfskip + noblame + nots + neg
print(f"\n[校验] {len(gaps)}+{len(gaps_noise)}+{boundary}+{selfskip}+{noblame}"
      f"+{nots}+{neg} = {parts}  应等于 {del_total}"
      f"  ->  {'OK' if parts == del_total else '对不上！还有静默跳过'}")
print(f"       （注释/空行 {noise} 条已含在「命中引入提交」里，不单独相加）")

print(f"\n{'=' * 68}")
print("【行级】按删除行计数（会被批量提交放大，仅供参考）")
print(f"{'=' * 68}")
report("非注释/空行", gaps)
report("注释/空行", gaps_noise)

print(f"\n{'=' * 68}")
print("【提交级】按引入提交去重、取最早一次修复  <- 推荐口径")
print(f"{'=' * 68}")
report("被代码行命中的引入提交", commit_gaps)
report("仅被注释行命中的引入提交", commit_noise_only)

# ---------------- 直方图（提交级，前 300 天，15 天桶） ----------------
if commit_gaps:
    print(f"\n{'=' * 68}")
    print("【提交级】gap 直方图（15 天桶，只画 <=300 天）")
    print(f"{'=' * 68}")
    b = collections.Counter(int(g // 15) * 15 for g in commit_gaps)
    peak = max(b.values()) if b else 1
    for k in sorted(b):
        if k <= 300:
            bar = "#" * max(1, round(b[k] * 50 / peak))
            print(f"  {k:4d}-{k + 14:4d} 天  {b[k]:4d}  {bar}")
    over = sum(v for k, v in b.items() if k > 300)
    print(f"  （>300 天另有 {over} 个引入提交，已省略）")

# ---------------- 观察窗口覆盖率 ----------------
WINDOWS = [("1 个月", 30.4), ("2 个月", 60.8), ("3 个月", 91.3),
           ("4 个月", 121.7), ("6 个月", 182.5), ("9 个月", 273.8),
           ("12 个月", 365.0), ("18 个月", 547.5), ("24 个月", 730.0),
           ("36 个月", 1095.0)]


def coverage(arr, days):
    return sum(1 for g in arr if g <= days) / len(arr) if arr else float("nan")


print(f"\n{'=' * 68}")
print("观察窗口覆盖率：窗口取 W，有多少比例的缺陷在 W 内被修掉")
print(f"{'=' * 68}")
print(f"{'W':>9}  {'行级代码':>9}  {'行级注释':>9}  {'提交级代码':>10}  {'提交级仅注释':>12}")
for label, days in WINDOWS:
    print(f"{label:>9}  {coverage(gaps, days):>8.1%}  {coverage(gaps_noise, days):>8.1%}"
          f"  {coverage(commit_gaps, days):>9.1%}  {coverage(commit_noise_only, days):>11.1%}")
print("\n以「提交级代码」列为准 —— 取覆盖开始变平的那个 W。")

# ---------------- 缺陷引入率 ----------------
bic_total = len(commit_gaps) + len(commit_noise_only)
print(f"\n{'=' * 68}")
print("缺陷引入率")
print(f"{'=' * 68}")
print(f"  引入提交（去重后）  {bic_total}  "
      f"= 代码行命中 {len(commit_gaps)} + 仅注释行命中 {len(commit_noise_only)}")
if TOTAL_COMMITS:
    print(f"  默认分支提交数      {TOTAL_COMMITS}")
    print(f"  缺陷引入率          {bic_total / TOTAL_COMMITS:.1%}")
print(f"  已扫 fix 提交        {fixes}")
if hit_cap:
    print(f"  ! 因达到 MAXFIX={MAXFIX} 上限提前结束，该比例偏低，仅供中途参考")
else:
    print("  （已扫完全部符合条件的 fix 提交，此比例为实测值）")

# ---------------- 落盘 ----------------
OUT_DIR.mkdir(exist_ok=True)
csv_path = OUT_DIR / "gaps.csv"
with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["gap_days", "is_noise", "level", "intro_sha"])
    for g in gaps:
        w.writerow([round(g, 2), 0, "line", ""])
    for g in gaps_noise:
        w.writerow([round(g, 2), 1, "line", ""])
    for sha, ts in first_fix_code.items():
        t_intro = TS.get(sha)
        if t_intro is not None and ts >= t_intro:
            w.writerow([round((ts - t_intro) / 86400.0, 2), 0, "commit", sha])
    for sha, ts in first_fix_noise.items():
        if sha in first_fix_code:
            continue
        t_intro = TS.get(sha)
        if t_intro is not None and ts >= t_intro:
            w.writerow([round((ts - t_intro) / 86400.0, 2), 1, "commit", sha])
print(f"\n[OK] {csv_path}")
print(f"     行级 {len(gaps) + len(gaps_noise)} 行 | "
      f"提交级 {len(commit_gaps) + len(commit_noise_only)} 行")
