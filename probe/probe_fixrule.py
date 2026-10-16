# -*- coding: utf-8 -*-
"""
探针 ⑤：fix 正则逐关键词验伪 + 作者身份归一化（只读提交信息，秒级完成）

用法：
    python probe_fixrule.py
    python probe_fixrule.py <仓库路径>

输出：
    1) 每个关键词单独命中多少条，以及"只被它命中"的样本 —— 判断该不该留
    2) 六种候选规则的 fix 数与占比（含合并 / 不含合并 两套分母）
    3) 推荐规则的按年分布
    4) 作者身份归一化（并查集：共享邮箱或共享归一化姓名 -> 同一人）
"""
import re
import sys
import collections
import datetime
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if len(sys.argv) > 1:
    REPO = Path(sys.argv[1]).expanduser().resolve()
else:
    cand = ROOT / "repos" / "activemq.git"
    REPO = cand if cand.exists() else ROOT / "repos" / "commons-cli.git"

if not REPO.exists():
    sys.exit(f"[X] 找不到仓库：{REPO}\n    用法：python probe_fixrule.py <仓库路径>")

print(f"[i] 仓库：{REPO}\n")


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


SEP = "\x01"
rows = []
for line in git("log", "--all", f"--format=%H{SEP}%at{SEP}%an{SEP}%ae{SEP}%P{SEP}%s").splitlines():
    p = line.split(SEP)
    if len(p) >= 6:
        rows.append({"sha": p[0], "ts": int(p[1]), "an": p[2], "ae": p[3],
                     "nparent": len(p[4].split()), "subj": p[5]})

BOT_RE = re.compile(r"\[bot\]|dependabot|renovate|github-actions|greenkeeper", re.I)
ISSUE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b|(?<!\w)#\d+")

humans = [r for r in rows if not BOT_RE.search(r["an"])]
bots = [r for r in rows if BOT_RE.search(r["an"])]
nonmerge = [r for r in humans if r["nparent"] <= 1]

print(f"提交总数（全分支）      {len(rows)}")
print(f"  机器人               {len(bots)} ({len(bots) / len(rows):.1%})")
print(f"  人类                 {len(humans)}")
print(f"  人类且非合并提交      {len(nonmerge)}   <- SZZ 实际会处理的集合\n")


# ---------------- 1) 逐关键词 ----------------
KW = {
    "fix":     r"\bfix(e[sd])?\b",
    "bug":     r"\bbug\b",
    "defect":  r"\bdefect\b",
    "patch":   r"\bpatch\b",
    "resolve": r"\bresolve[sd]?\b",
    "close":   r"\bclose[sd]?\b",
}
kw_re = {k: re.compile(v, re.I) for k, v in KW.items()}

print(f"{'=' * 74}")
print("逐关键词：命中数 + 「只被它命中」的样本（非合并提交）")
print(f"{'=' * 74}")
for k, rx in kw_re.items():
    hit = [r for r in nonmerge if rx.search(r["subj"])]
    only = [r for r in hit
            if not any(o.search(r["subj"]) for k2, o in kw_re.items() if k2 != k)]
    print(f"\n[{k}]  命中 {len(hit)}   只被它命中 {len(only)}")
    for r in only[:4]:
        print(f"    {r['sha'][:8]}  {r['subj'][:76]}")

# 看 patch 里有多少是 GitHub 的 patch-N 分支名
pn = [r for r in nonmerge if re.search(r"/patch-\d+|\bpatch-\d+\b", r["subj"], re.I)]
print(f"\n注：含 'patch-N' 分支名的提交 {len(pn)} 条（GitHub 网页改文件的默认分支名，非补丁语义）")

# ---------------- 2) 候选规则 ----------------
CAND = [
    ("A 松 fix|bug|patch|defect|resolve|close",
     re.compile(r"\b(fix(e[sd])?|bug|patch|defect|resolve[sd]?|close[sd]?)\b", re.I)),
    ("B 去掉 close",
     re.compile(r"\b(fix(e[sd])?|bug|patch|defect|resolve[sd]?)\b", re.I)),
    ("C 去掉 close+resolve (=fix|bug|patch|defect)",
     re.compile(r"\b(fix(e[sd])?|bug|patch|defect)\b", re.I)),
    ("D 只留 fix|bug|defect",
     re.compile(r"\b(fix(e[sd])?|bug|defect)\b", re.I)),
]

print(f"\n{'=' * 74}")
print("候选规则对照（分母：左=非合并人类提交，右=全部提交）")
print(f"{'=' * 74}")
for label, rx in CAND:
    h = sum(1 for r in nonmerge if rx.search(r["subj"]))
    a = sum(1 for r in rows if rx.search(r["subj"]))
    print(f"{label:<44}{h:>6} ({h / len(nonmerge):>5.1%}){a:>8} ({a / len(rows):>5.1%})")

for label, rx in [("E C + 必须带 issue 号",
                   re.compile(r"\b(fix(e[sd])?|bug|patch|defect)\b", re.I)),
                  ("F fix|bug|defect + issue 号",
                   re.compile(r"\b(fix(e[sd])?|bug|defect)\b", re.I))]:
    h = sum(1 for r in nonmerge if rx.search(r["subj"]) and ISSUE_RE.search(r["subj"]))
    a = sum(1 for r in rows if rx.search(r["subj"]) and ISSUE_RE.search(r["subj"]))
    print(f"{label:<44}{h:>6} ({h / len(nonmerge):>5.1%}){a:>8} ({a / len(rows):>5.1%})")

# ---------------- 3) 推荐规则按年 ----------------
REC = re.compile(r"\b(fix(e[sd])?|bug|patch|defect)\b", re.I)
yt, yf = collections.Counter(), collections.Counter()
for r in nonmerge:
    y = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).year
    yt[y] += 1
    if REC.search(r["subj"]):
        yf[y] += 1

print(f"\n{'=' * 74}")
print("推荐规则 C（fix|bug|patch|defect，剔机器人、剔合并提交）按年分布")
print(f"{'=' * 74}")
print(f"{'年份':<7}{'提交':>7}{'fix':>7}{'占比':>9}")
for y in sorted(yt):
    print(f"{y:<7}{yt[y]:>7}{yf.get(y, 0):>7}{yf.get(y, 0) / yt[y]:>9.1%}")
t_all, t_fix = sum(yt.values()), sum(yf.values())
print(f"{'合计':<7}{t_all:>7}{t_fix:>7}{t_fix / t_all:>9.1%}")

# ---------------- 4) 作者身份归一化 ----------------
print(f"\n{'=' * 74}")
print("作者身份归一化（并查集：共享邮箱 或 共享归一化姓名 -> 同一人）")
print(f"{'=' * 74}")


def norm_name(s):
    """归一化姓名：去括号后缀、丢单个字母（中间名缩写）、词序无关"""
    s = s.lower()
    s = re.sub(r"\(.*?\)", " ", s)               # 去掉 (cshannon) 这类括号后缀
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if len(t) > 1]   # Timothy A. Bish -> timothy bish
    return " ".join(sorted(set(toks)))            # Bosanac Dejan == Dejan Bosanac


uf = {}


def find(x):
    uf.setdefault(x, x)
    while uf[x] != x:
        uf[x] = uf[uf[x]]
        x = uf[x]
    return x


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        uf[ra] = rb


for r in humans:
    union("n:" + norm_name(r["an"]), "e:" + r["ae"].lower())

raw_names = len({r["an"].strip() for r in humans})                 # ① 完全原始
normed = len({norm_name(r["an"]) for r in humans})                 # ② 姓名归一化后
raw_mails = len({r["ae"].lower() for r in humans})
people = len({find("n:" + norm_name(r["an"])) for r in humans})    # ③ 再加邮箱并查集

print(f"  ① 原始姓名写法                    {raw_names}")
print(f"  ② 姓名归一化后（去缩写 / 词序无关） {normed}   减少 {raw_names - normed}")
print(f"  ③ 再加邮箱并查集合并              {people}   再减少 {normed - people}")
print(f"     合计 {raw_names} -> {people}，减少 {raw_names - people}"
      f"（{(raw_names - people) / raw_names:.1%}）")
print(f"  （邮箱写法共 {raw_mails} 个）")

by_mail = collections.defaultdict(set)
for r in humans:
    by_mail[r["ae"].lower()].add(r["an"])
dup = {m: ns for m, ns in by_mail.items() if len(ns) > 1}
print(f"\n  同一邮箱多个姓名：{len(dup)} 个邮箱")
for m, ns in list(dup.items())[:10]:
    print(f"    {m:<42} -> {sorted(ns)}")

print(f"\n  归一化后提交数 Top12")
cnt = collections.Counter(find("n:" + norm_name(r["an"])) for r in humans)
label = {}
for r in humans:
    label.setdefault(find("n:" + norm_name(r["an"])), set()).add(r["an"])
for root, c in cnt.most_common(12):
    print(f"    {c:>6}  {sorted(label[root])}")
