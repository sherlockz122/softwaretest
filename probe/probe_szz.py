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
from datetime import datetime, timezone
from datetime import datetime, timezone      # ← 加到 import 区
from pydriller import Repository

# ---------------- 路径 ----------------
if len(sys.argv) > 1:
    REPO = Path(sys.argv[1]).expanduser().resolve()
else:
    _base = Path(__file__).resolve().parent / "repos"
    _cand = _base / "activemq.git"
    REPO = _cand if _cand.exists() else _base / "commons-cli.git"

if not REPO.exists():
    sys.exit(f"[X] 找不到仓库：{REPO}\n    用法：python probe_szz.py <仓库路径>")

PROBE = REPO.parent.parent                 # .../probe
OUT_DIR = PROBE / "out"

MAXFIX = 1500                              # 最多处理多少个 fix 提交
SHOW_SAMPLES = 10                          # 打印前 N 条删除行样本（看格式用）

# ---- fix 识别规则：实测定案（见 探针结论表.md 2.3）----
#   保留 fix|bug|patch|defect   patch 的 550 条样本经人工核对全是真修复
#   去掉 resolve|close          样本噪声比例过高
FIX_RE = re.compile(r"\b(fix(e[sd])?|bug|patch|defect)\b", re.I)

# ---- 机器人作者过滤（dependabot 之类只改版本号，不可能是缺陷引入提交）----
BOT_RE = re.compile(r"\[bot\]|dependabot|renovate|github-actions|greenkeeper", re.I)

# ---- 时间窗口：同期群（cohort）口径 ----
# 为什么 TO 必须是 None：
#   blame 会追到窗口之前的提交。若 fix 也只扫到窗口末尾，那么窗口之前引入的 BIC
#   会因为"它真正的首次修复落在扫描范围外"而被记成更晚的修复 -> gap 被系统性拉长。
#   所以：fix 一直扫到今天（TO=None），统计时只保留 intro_ts >= SINCE 的 BIC。
SINCE = datetime(2013, 1, 1, tzinfo=timezone.utc)
TO = None

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
    """非语义行：注释 / 空行 / import / package。

    import 和 package 本身不含语义，但重命名、类移动、javax->jakarta 迁移
    这类批量变更会横扫它们，blame 就会被吸到很久以前的提交，把 gap 拉长。
    实测 ActiveMQ 的前 10 条样本全是 import，故一并剔除。
    """
    s = (txt or "").strip()
    return (not s) or s.startswith("//") or s.startswith("*") \
        or s.startswith("/*") or s.startswith("import ") or s.startswith("package ")


# ---- 测试文件判定：给每条 BIC 打 is_test 标记（方案 B：全做但可分层）----
TEST_DIR = re.compile(r"(^|/)src/test/|(^|/)test/", re.I)
TEST_NAME = re.compile(r"(Test|Tests|TestCase|IT|ITCase|IntegrationTest)\.java$")


def is_test_path(p):
    return bool(TEST_DIR.search(p) or TEST_NAME.search(p))


def commit_is_test_only(sha):
    """引入提交是否【只】改了测试文件。取不到返回 None。

    注意：git 的 --numstat 用 `old => new` 表示重命名，要取新路径再判定。
    """
    out = git("show", "--numstat", "--format=", sha)
    n_main = n_test = 0
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        path = parts[2]
        if " => " in path:                      # 重命名：取新路径
            path = path.split(" => ")[-1].strip()
        if is_test_path(path):
            n_test += 1
        else:
            n_main += 1
    if n_main + n_test == 0:
        return None
    return n_main == 0


# ---------------- 主循环 ----------------
gaps = []          # 【行级】非注释/空行
gaps_noise = []    # 【行级】注释/空行

first_fix_code = {}    # 【提交级】引入 sha -> 最早被修的时间戳（代码行命中）
first_fix_noise = {}   # 【提交级】引入 sha -> 最早被修的时间戳（注释行命中）

del_total = 0      # 删除行总数（.java，且 diff 能解析）
noise = 0          # 其中非语义行（注释 / 空行 / import / package）
noblame = 0        # blame 拿不到输出
boundary = 0       # 边界提交（追到仓库初始提交）
selfskip = 0       # blame 命中 fix 提交自己
nots = 0           # sha 不在 TS 里
neg = 0            # 负间隔（时钟异常 / rebase）
samples_shown = 0
fixes = 0
hit_cap = False    # 是否因为达到 MAXFIX 上限而提前结束
bots_skipped = 0   # 跳过的机器人提交数
pre_window = 0     # 引入时间早于 SINCE、被排除出 gap 分布的命中数

t0 = time.perf_counter()

_repo_kw = {}
if SINCE:
    _repo_kw["since"] = SINCE
if TO:
    _repo_kw["to"] = TO

for c in Repository(str(REPO), **_repo_kw).traverse_commits():
    if BOT_RE.search(c.author.name or ""):    # 跳过机器人
        bots_skipped += 1
        continue
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
                if SINCE and t_intro < SINCE.timestamp():
                    pre_window += 1  # 引入时间在统计起点之前 -> 不计入 gap 分布
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

print(f"\n[i] fix 规则：{FIX_RE.pattern}")
print(f"[i] 机器人过滤：跳过 {bots_skipped} 条")
print(f"[i] 时间窗口：{SINCE or '不限'} ~ {TO or '不限'}")

# ---- 给每条 BIC 打 is_test 标记（方案 B）----
_bic_shas = sorted(set(first_fix_code) | set(first_fix_noise))
bic_is_test = {}
print(f"\n[i] 计算 {len(_bic_shas)} 条 BIC 的 is_test 标记 ...")
for _i, _sha in enumerate(_bic_shas, 1):
    if _i % 250 == 0:
        print(f"    ...{_i}/{len(_bic_shas)}")
    _r = commit_is_test_only(_sha)
    if _r is not None:
        bic_is_test[_sha] = _r
_test_only = sum(1 for v in bic_is_test.values() if v)
print(f"[i] 其中【只改测试文件】的 BIC：{_test_only} / {len(bic_is_test)}"
      f"（{_test_only / len(bic_is_test):.1%}）")


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
    print(f"  |- 非语义行          {noise}  ({noise / del_total:.1%})")
print(f"  |- 命中引入提交       {len(gaps) + len(gaps_noise)}")
print(f"  |- 边界提交           {boundary}")
print(f"  |- 负间隔             {neg}")
print(f"  \\- 同期群排除         {pre_window}")

# selfskip / noblame / nots 结构性恒为 0，不作为指标展示；只在异常时报警
if selfskip or noblame or nots:
    print(f"\n  !! 异常：blame 命中自己 {selfskip} / blame 无输出 {noblame}"
          f" / sha 不在映射表 {nots}")
    print("     这三项按设计不可能非 0，请检查 blame 的调用方式")

parts = (len(gaps) + len(gaps_noise) + boundary + selfskip + noblame + nots
         + neg + pre_window)
print(f"\n[校验] {len(gaps)}+{len(gaps_noise)}+{boundary}+{selfskip}+{noblame}"
      f"+{nots}+{neg}+{pre_window} = {parts}  应等于 {del_total}"
      f"  ->  {'OK' if parts == del_total else '对不上！还有静默跳过'}")
print(f"       后三项（blame 命中自己 / 无输出 / sha 失配）结构性为 0，见代码注释")
print(f"       （非语义行 {noise} 条已含在「命中引入提交」里，不单独相加）")
if pre_window:
    print(f"       （其中 {pre_window} 条因引入时间早于 "
          f"{SINCE.date() if SINCE else '不限'} 被排除）")

# ---- BIC 的测试/主代码分层（方案 B）----
if bic_is_test:
    _n_test = sum(1 for v in bic_is_test.values() if v)
    _n_main = len(bic_is_test) - _n_test
    print(f"\n{'=' * 68}")
    print("BIC 分层（is_test）—— 只改测试文件的 BIC 不是产品缺陷")
    print(f"{'=' * 68}")
    print(f"  is_test=1（只改测试）   {_n_test:6d}  ({_n_test / len(bic_is_test):.1%})")
    print(f"  is_test=0（改过主代码） {_n_main:6d}  ({_n_main / len(bic_is_test):.1%})")
    print(f"  取不到                   {len(_bic_shas) - len(bic_is_test):6d}")

print(f"\n{'=' * 68}")
print("【行级】按删除行计数（会被批量提交放大，仅供参考）")
print(f"{'=' * 68}")
report("非注释/空行", gaps)
report("非语义行(注释/空行/import)", gaps_noise)

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
    w.writerow(["gap_days", "is_noise", "level", "intro_sha", "is_test"])
    for g in gaps:
        w.writerow([round(g, 2), 0, "line", "", ""])
    for g in gaps_noise:
        w.writerow([round(g, 2), 1, "line", "", ""])
    for sha, ts in first_fix_code.items():
        t_intro = TS.get(sha)
        if t_intro is not None and ts >= t_intro:
            w.writerow([round((ts - t_intro) / 86400.0, 2), 0, "commit", sha,
                        int(bic_is_test.get(sha, False))])
    for sha, ts in first_fix_noise.items():
        if sha in first_fix_code:
            continue
        t_intro = TS.get(sha)
        if t_intro is not None and ts >= t_intro:
            w.writerow([round((ts - t_intro) / 86400.0, 2), 1, "commit", sha,
                        int(bic_is_test.get(sha, False))])
print(f"\n[OK] {csv_path}")
print(f"     行级 {len(gaps) + len(gaps_noise)} 行 | "
      f"提交级 {len(commit_gaps) + len(commit_noise_only)} 行")
