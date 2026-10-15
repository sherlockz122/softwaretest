# -*- coding: utf-8 -*-
"""
探针 ⑥：<1 天间隔人工抽查 —— 验证 SZZ 标签质量

用法：
    python probe_gapcheck.py                       # activemq / 1.0 天 / 12 条
    python probe_gapcheck.py <仓库路径> <阈值天数> <抽样条数>

读 probe/out/gaps.csv，挑出「提交级 + 非语义行之外 + gap < 阈值」的 BIC，
把「这个引入提交做了什么」和「随后改同一批文件的提交」打出来，供人工判断：

  真阳性：引入与修复是同一作者、同一天、改同一文件
          -> 开发者当天写完当天发现自己写错了，属于正常
  假阳性：引入提交本身是一次无关的小改动（格式化、日志调整），
          而 fix 其实是在修别的地方 -> blame 落错了，SZZ 误标
"""
import csv
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent

REPO = (Path(sys.argv[1]).expanduser().resolve()
        if len(sys.argv) > 1 else ROOT / "repos" / "activemq.git")
THRESH = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
NSHOW = int(sys.argv[3]) if len(sys.argv) > 3 else 12

CSV_PATH = ROOT / "out" / "gaps.csv"

if not REPO.exists():
    sys.exit(f"[X] 找不到仓库：{REPO}")
if not CSV_PATH.exists():
    sys.exit(f"[X] 找不到 {CSV_PATH}，请先跑 probe_szz.py")


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8-sig")))
cand = [r for r in rows
        if r.get("level") == "commit"
        and r.get("is_noise") == "0"
        and r.get("intro_sha")
        and float(r["gap_days"]) < THRESH]
cand.sort(key=lambda r: float(r["gap_days"]))

print(f"仓库   {REPO}")
print(f"数据   {CSV_PATH}")
print(f"筛选   提交级 + 非语义行之外 + gap < {THRESH} 天")
print(f"结果   共 {len(cand)} 条，展示前 {min(NSHOW, len(cand))} 条（按 gap 升序）\n")

for i, r in enumerate(cand[:NSHOW], 1):
    sha = r["intro_sha"]
    gap = float(r["gap_days"])

    head = git("show", "-s", "--format=%h|%ad|%an|%s", "--date=short", sha).strip()
    parts = head.split("|", 3)
    if len(parts) < 4:
        continue
    h, d, an, subj = parts

    print("=" * 78)
    print(f"样本 {i}/{min(NSHOW, len(cand))}    gap = {gap:.3f} 天")
    print(f"  引入提交   {h}   {d}   {an}")
    print(f"  提交信息   {subj[:68]}")

    stat = [l for l in git("show", "--stat", "--format=", sha).splitlines() if l.strip()]
    if stat:
        print(f"  改动统计   {stat[-1].strip()}")
        for l in stat[:-1][:5]:
            print(f"             {l.strip()}")

    files = [l.strip() for l in git("show", "--name-only", "--format=", sha).splitlines() if l.strip()]
    if files:
        try:
            since = datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
            until = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d")
        except Exception:
            since, until = d, d
        later = git("log", "--format=%h|%ad|%an|%s", "--date=short",
                    f"--since={since}", f"--until={until}", "--", *files[:4])
        lines = [l for l in later.splitlines() if l.strip() and not l.startswith(h)]
        lines.reverse()                      # 时间升序
        print(f"  之后 3 天内改同一批文件的提交（{len(lines)} 条）")
        if not lines:
            print("             （无）")
        for l in lines[:4]:
            print(f"             {l[:84]}")

print("\n" + "=" * 78)
print("怎么判：")
print("  真阳性——引入与后续提交是同一作者、同一天、改同一文件")
print("          -> 开发者当天写完当天修，标签是对的")
print("  假阳性——引入提交是一次无关的小改动（格式化 / 日志 / import），")
print("          真正被修的地方在别处 -> blame 落错了，属于 SZZ 误标")
