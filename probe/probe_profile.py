# -*- coding: utf-8 -*-
"""
探针 ②：仓库画像 + fix 提交识别（三档正则对照）

用法：
    python probe_profile.py
    python probe_profile.py <仓库路径>
输出：
    控制台摘要 + probe/out/profile_<仓库名>.csv

为什么给三档正则：
  ActiveMQ 这类中间件仓库的提交信息里满是 "close the connection""resolve the URL"
  这类**非缺陷语义**的词，用松正则会把 fix 占比抬到 30%+（文献区间是 15%~30%）。
  所以同时统计三档，用「松严差」来判断正则噪声有多大。

  松 LOOSE ：fix/bug/patch/defect/resolve/close
  中 MID   ：只留 fix/bug/defect
  严 STRICT：MID 词 + 必须带 issue 号（AMQ-1234 或 #123）
"""
import re
import csv
import sys
import collections
from pathlib import Path

from pydriller import Repository

ROOT = Path(__file__).resolve().parent
if len(sys.argv) > 1:
    REPO = Path(sys.argv[1]).expanduser().resolve()
else:
    cand = ROOT / "repos" / "activemq.git"
    REPO = cand if cand.exists() else ROOT / "repos" / "commons-cli.git"

if not REPO.exists():
    sys.exit(f"[X] 找不到仓库：{REPO}\n    用法：python probe_profile.py <仓库路径>")

NAME = REPO.name.replace(".git", "")

PAT_LOOSE = re.compile(r"\b(fix(e[sd])?|bug|patch|defect|resolve[sd]?|close[sd]?)\b", re.I)
PAT_MID = re.compile(r"\b(fix(e[sd])?|bug|defect)\b", re.I)
ISSUE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b|(?<!\w)#\d+")

print(f"[i] 仓库：{REPO}\n")

year_total = collections.Counter()
year_loose = collections.Counter()
year_mid = collections.Counter()
year_strict = collections.Counter()
authors = set()
total = merges = nodiff = 0
loose_only = []          # 松命中但中不命中 —— 疑似误报样本


def has_diff(c):
    try:
        for m in c.modified_files:
            d = m.diff_parsed
            if d.get("added") or d.get("deleted"):
                return True
    except Exception:
        pass
    return False


for c in Repository(str(REPO)).traverse_commits():
    total += 1
    if total % 2000 == 0:
        print(f"  ...{total}")

    y = c.author_date.year
    year_total[y] += 1
    authors.add(c.author.email)
    if len(c.parents) > 1:
        merges += 1
    if not has_diff(c):
        nodiff += 1

    msg = c.msg or ""
    lo = PAT_LOOSE.search(msg)
    mi = PAT_MID.search(msg)
    if lo:
        year_loose[y] += 1
    if mi:
        year_mid[y] += 1
        if ISSUE_RE.search(msg):
            year_strict[y] += 1
    if lo and not mi and len(loose_only) < 15:
        loose_only.append((c.hash[:8], y, msg.strip().split("\n")[0][:72]))

usable = total - nodiff
print(f"\n{'=' * 68}")
print(f"仓库画像：{NAME}")
print(f"{'=' * 68}")
print(f"总提交（默认分支）  {total}")
print(f"合并提交            {merges:6d}  ({merges / total:.1%})  天然无 diff")
print(f"无 diff（含合并）   {nodiff:6d}  ({nodiff / total:.1%})")
print(f"可算特征            {usable:6d}  ({usable / total:.1%})")
print(f"作者数              {len(authors)}")

tl = sum(year_loose.values())
tm = sum(year_mid.values())
ts = sum(year_strict.values())
print(f"\nfix 提交数（三档正则对照）")
print(f"  松 LOOSE   {tl:6d}  ({tl / total:.1%})")
print(f"  中 MID     {tm:6d}  ({tm / total:.1%})")
print(f"  严 STRICT  {ts:6d}  ({ts / total:.1%})")
print(f"  -> 松严差   {tl - tm:6d} 条，就是 close/resolve/patch 这类词带进来的")

print(f"\n{'年份':<7}{'提交':>7}{'松':>7}{'中':>7}{'严':>7}{'松占比':>9}")
for y in sorted(year_total):
    print(f"{y:<7}{year_total[y]:>7}{year_loose.get(y, 0):>7}"
          f"{year_mid.get(y, 0):>7}{year_strict.get(y, 0):>7}"
          f"{year_loose.get(y, 0) / year_total[y]:>9.1%}")

if loose_only:
    print(f"\n{'=' * 68}")
    print("疑似误报样本（松命中、中不命中）—— 用来判断正则该不该收紧")
    print(f"{'=' * 68}")
    for sha, y, subj in loose_only:
        print(f"  {sha}  {y}  {subj}")

out = ROOT / "out"
out.mkdir(exist_ok=True)
p = out / f"profile_{NAME}.csv"
with p.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["year", "commits", "fix_loose", "fix_mid", "fix_strict"])
    for y in sorted(year_total):
        w.writerow([y, year_total[y], year_loose.get(y, 0),
                    year_mid.get(y, 0), year_strict.get(y, 0)])
print(f"\n[OK] {p}")
