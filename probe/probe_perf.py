# -*- coding: utf-8 -*-
"""
探针 ④：规模-耗时曲线  ->  反推输入上限

用法：
    python probe_perf.py                # 自动选 repos/activemq.git，没有就用 commons-cli.git
    python probe_perf.py <仓库路径>
输出：
    控制台报表 + probe/out/perf_<仓库名>.csv

测什么：
  阶段 0  建 sha->时间 映射          （一次性）
  阶段 1  建文件历史索引             （一次性，nuc/age/ndev 用）
  阶段 2  遍历提交 + 算 Kamei 特征   （在 200/500/1000/2000 条处打点，看是否线性）
  阶段 3  git blame 单次耗时         （采样若干次）

最后反推：
  - 给定"单任务预算 30 分钟"，最多能跑多少条提交  -> 单仓库提交数上限
  - 每个 fix 的 SZZ 成本 -> 全仓 SZZ 预计耗时
"""
import re
import csv
import sys
import time
import math
import collections
import subprocess
from pathlib import Path

from pydriller import Repository

# ---------------- 配置 ----------------
CHECKPOINTS = [200, 500, 1000, 2000]   # 在这些提交数上打点
NMAX = max(CHECKPOINTS)                # 最多遍历这么多提交
BLAME_SAMPLE = 60                      # blame 采样次数
BUDGET_MIN = 30                        # 单任务可接受的耗时上限（分钟），用来反推上限
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# ---------------- 路径 ----------------
if len(sys.argv) > 1:
    REPO = Path(sys.argv[1]).expanduser().resolve()
else:
    base = Path(r"D:\develop\软件测试实训\jit-defect-prediction\probe\repos")
    cand = base / "activemq.git"
    REPO = cand if cand.exists() else base / "commons-cli.git"

if not REPO.exists():
    sys.exit(f"[X] 找不到仓库：{REPO}\n    用法：python probe_perf.py <仓库路径>")

OUT_DIR = REPO.parent.parent / "out"
NAME = REPO.name.replace(".git", "")

print(f"[i] 仓库：{REPO}")
print(f"[i] 打点：{CHECKPOINTS}   预算：{BUDGET_MIN} 分钟\n")


def git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


# ================= 阶段 0：sha -> 作者时间 =================
t = time.perf_counter()
TS = {}
for line in git("log", "--all", "--format=%H %at").splitlines():
    p = line.split()
    if len(p) >= 2:
        TS[p[0]] = int(p[1])
t_ts = time.perf_counter() - t
print(f"[0] sha->时间 映射：{len(TS)} 条，{t_ts:.2f}s")


# ================= 阶段 1：文件历史索引 =================
t = time.perf_counter()
file_hist = collections.defaultdict(list)    # path -> [sha, ...]  新 -> 旧
file_devs = collections.defaultdict(set)     # path -> {email}
cur_sha = cur_dev = None
n_files = 0
for line in git("log", "--all", "--name-only", "--format=@@%H|%ae").splitlines():
    if line.startswith("@@"):
        head = line[2:].split("|", 1)
        cur_sha = head[0]
        cur_dev = head[1] if len(head) > 1 else ""
    elif line.strip() and cur_sha:
        p = line.strip()
        file_hist[p].append(cur_sha)
        file_devs[p].add(cur_dev)
        n_files += 1
t_index = time.perf_counter() - t
print(f"[1] 文件历史索引：{len(file_hist)} 个文件 / {n_files} 条 文件-提交 记录，{t_index:.2f}s")


# ================= 阶段 2：遍历 + 简化 Kamei =================
def kamei_lite(c):
    """算一组有代表性的变更度量。数值不求精确，求的是逼近真实计算量。"""
    ns = nf = la = ld = 0
    sizes = []
    paths = []
    for m in c.modified_files:
        try:
            dp = m.diff_parsed
        except Exception:
            continue
        a = len(dp.get("added") or [])
        d = len(dp.get("deleted") or [])
        la += a
        ld += d
        ct = m.change_type.name if m.change_type else "UNKNOWN"
        if ct == "ADD":
            ns += 1
        else:
            nf += 1
        p = m.new_path or m.old_path or ""
        if p:
            paths.append(p)
        sizes.append(a + d)

    lt = la + ld

    # entropy：改动在各文件间的分布
    tot = sum(sizes)
    entropy = 0.0
    if tot > 0:
        for s in sizes:
            if s > 0:
                pr = s / tot
                entropy -= pr * math.log(pr)

    # nuc / ndev：要靠文件历史
    nuc = sum(len(file_hist.get(p, ())) for p in paths)
    ndev = len({d for p in paths for d in file_devs.get(p, ())})

    # age：文件距上次改动多少天
    cts = c.author_date.timestamp()
    ages = []
    for p in paths:
        for s in file_hist.get(p, ()):
            t2 = TS.get(s)
            if t2 is not None and t2 < cts:
                ages.append((cts - t2) / 86400.0)
                break
    age = sum(ages) / len(ages) if ages else 0.0

    return ns, nf, la, ld, lt, entropy, nuc, ndev, age


marks = {}            # n -> 累计秒
per_commit = {}       # n -> 每条提交 ms
blame_samples = []    # (parent, path, lineno)
n = 0
t0 = time.perf_counter()

for c in Repository(str(REPO)).traverse_commits():
    if n >= NMAX:
        break
    n += 1
    kamei_lite(c)

    # 顺手收集 blame 采样点：找 fix 提交里被删的 .java 行
    if len(blame_samples) < BLAME_SAMPLE and len(c.parents) == 1:
        for m in c.modified_files:
            if not (m.new_path or "").endswith(".java"):
                continue
            try:
                deleted = m.diff_parsed["deleted"]
            except Exception:
                continue
            for lineno, _txt in deleted:
                blame_samples.append((c.parents[0], m.old_path or m.new_path, lineno))
                if len(blame_samples) >= BLAME_SAMPLE:
                    break
            if len(blame_samples) >= BLAME_SAMPLE:
                break

    if n in CHECKPOINTS:
        el = time.perf_counter() - t0
        marks[n] = el
        per_commit[n] = el / n * 1000

t_walk = time.perf_counter() - t0


# ================= 阶段 3：blame 单次耗时 =================
t = time.perf_counter()
k = 0
for parent, path, lineno in blame_samples:
    git("blame", "--porcelain", "-L", f"{lineno},{lineno}", parent, "--", path)
    k += 1
t_blame = time.perf_counter() - t
ms_blame = (t_blame / k * 1000) if k else float("nan")


# ================= 报表 =================
print(f"\n{'=' * 68}")
print("阶段耗时")
print(f"{'=' * 68}")
print(f"  [0] 建 sha->时间 映射        {t_ts:8.2f} s   （一次性）")
print(f"  [1] 建文件历史索引           {t_index:8.2f} s   （一次性）")
print(f"  [2] 遍历 {n} 条提交 + 特征     {t_walk:8.2f} s")
print(f"  [3] blame 采样 {k} 次          {t_blame:8.2f} s   -> {ms_blame:.1f} ms/次")

print(f"\n{'=' * 68}")
print("规模-耗时曲线（看「每条提交」那列是否稳定 —— 稳定即线性）")
print(f"{'=' * 68}")
print(f"  {'提交数':>8}  {'累计耗时':>12}  {'每条提交':>12}")
for cp in CHECKPOINTS:
    if cp in marks:
        print(f"  {cp:>8}  {marks[cp]:>10.2f} s  {per_commit[cp]:>10.1f} ms")

# 用最大的那个打点做速率（最稳）
biggest = max(marks) if marks else 0
if biggest:
    rate = marks[biggest] / biggest          # 秒/条
    print(f"\n{'=' * 68}")
    print(f"线性外推（按 {biggest} 条实测速率 {rate * 1000:.1f} ms/条；含 diff 计算，不含 blame）")
    print(f"{'=' * 68}")
    for target in (5000, 10000, 25000, 50000):
        mins = rate * target / 60
        flag = "  <-- 超出预算" if mins > BUDGET_MIN else ""
        print(f"  {target:>7,} 条  ->  {mins:8.1f} 分钟{flag}")

    cap = int(BUDGET_MIN * 60 / rate)
    print(f"\n{'=' * 68}")
    print(f"输入上限反推（单任务预算 {BUDGET_MIN} 分钟）")
    print(f"{'=' * 68}")
    print(f"  遍历+特征提取 的提交数上限  ≈ {cap:,} 条")
    print(f"  （预算改了，上限按比例变：{BUDGET_MIN * 2} 分钟 -> {cap * 2:,} 条）")

# ---- SZZ 成本 ----
fixes_total = 399        # 见探针结论表；换成 read 出来的更好
lines_per_fix = 10.5     # commons-cli 实测 4207 行 / 399 fix
if k:
    per_fix_ms = lines_per_fix * ms_blame
    print(f"\n{'=' * 68}")
    print("SZZ 成本（按 blame 单次耗时外推）")
    print(f"{'=' * 68}")
    print(f"  blame 单次                  {ms_blame:8.1f} ms")
    print(f"  每个 fix 平均删除行数        {lines_per_fix:8.1f} 行  （commons-cli 实测）")
    print(f"  -> 每个 fix 的 SZZ 成本      {per_fix_ms / 1000:8.2f} s")
    for nf_ in (100, 400, 2000, 10000):
        mins = per_fix_ms * nf_ / 1000 / 60
        print(f"  {nf_:>6,} 个 fix  ->  {mins:8.1f} 分钟")
    print("\n  注：真实 SZZ 还要叠加【遍历提交】的成本，见上面的规模-耗时曲线。")

# ================= 落盘 =================
OUT_DIR.mkdir(exist_ok=True)
csv_path = OUT_DIR / f"perf_{NAME}.csv"
with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["metric", "value", "unit"])
    w.writerow(["repo", str(REPO), ""])
    w.writerow(["ts_map_seconds", round(t_ts, 3), "s"])
    w.writerow(["file_index_seconds", round(t_index, 3), "s"])
    w.writerow(["indexed_files", len(file_hist), "个"])
    w.writerow(["walk_total_seconds", round(t_walk, 3), "s"])
    w.writerow(["walk_commits", n, "条"])
    for cp in CHECKPOINTS:
        if cp in marks:
            w.writerow([f"cum_seconds_at_{cp}", round(marks[cp], 3), "s"])
            w.writerow([f"ms_per_commit_at_{cp}", round(per_commit[cp], 1), "ms"])
    w.writerow(["blame_samples", k, "次"])
    w.writerow(["blame_ms_per_call", round(ms_blame, 2), "ms"])
    if biggest:
        w.writerow(["seconds_per_commit", round(rate, 6), "s"])
        w.writerow(["commit_cap_30min", int(BUDGET_MIN * 60 / rate), "条"])
print(f"\n[OK] {csv_path}")
