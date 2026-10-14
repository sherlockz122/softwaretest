# probe_profile.py —— 放在 D:\develop\软件测试实训\jit-defect-prediction\probe\
import re, csv, collections, sys
from pathlib import Path
from pydriller import Repository

ROOT = Path(__file__).resolve().parent
REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "repos" / "commons-cli.git"

FIX_RE = re.compile(r"\b(fix(e[sd])?|bug|patch|defect|resolve[sd]?|close[sd]?)\b", re.I)

year_total, year_fix, authors = collections.Counter(), collections.Counter(), set()
total = merges = nodiff = 0

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
    if total % 500 == 0:
        print(f"  ...{total}")
    y = c.author_date.year
    year_total[y] += 1
    authors.add(c.author.email)
    if len(c.parents) > 1:
        merges += 1
    if not has_diff(c):
        nodiff += 1
    if FIX_RE.search(c.msg or ""):
        year_fix[y] += 1

usable = total - merges - nodiff
print(f"\n总提交      {total}")
print(f"合并提交    {merges:6d}  ({merges/total:.1%})")
print(f"无 diff     {nodiff:6d}  ({nodiff/total:.1%})")
print(f"可算特征    {total - nodiff}")
print(f"作者数      {len(authors)}\n")

print(f"{'年份':<6}{'提交':>7}{'fix':>7}{'fix占比':>9}")
for y in sorted(year_total):
    f = year_fix.get(y, 0)
    print(f"{y:<6}{year_total[y]:>7}{f:>7}{f/year_total[y]:>9.1%}")

out = ROOT / "out"; out.mkdir(exist_ok=True)
p = out / "profile_commons-cli.csv"
with p.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh); w.writerow(["year", "commits", "fix", "fix_ratio"])
    for y in sorted(year_total):
        w.writerow([y, year_total[y], year_fix.get(y, 0),
                    round(year_fix.get(y, 0) / year_total[y], 4)])
print(f"[OK] {p}")