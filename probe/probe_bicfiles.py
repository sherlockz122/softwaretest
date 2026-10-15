# -*- coding: utf-8 -*-
"""
探针 ⑦：BIC 的改动文件构成 —— 有多少"缺陷引入提交"只动了测试代码

用法：
    python probe_bicfiles.py                    # activemq / 全部分析
    python probe_bicfiles.py <仓库路径> <上限条数>

读 probe/out/gaps.csv，取「提交级」的记录（= 全部 BIC），
对每个引入提交取改动文件清单与增删行数，分类统计：

    只改主代码     全部路径都不在测试目录
    只改测试       全部路径都在测试目录 / 类名以 Test|IT 结尾
    混合           两者都有
    只改非 Java    全部不是 .java
    取不到         git show 无输出

为什么要这个：
    一条"测试代码的 BIC"不是产品缺陷。如果占比高，模型会学到
    "测试文件的改动模式"，而不是产品缺陷模式 —— 需要决定要不要切掉。
"""
import csv
import re
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent

REPO = (Path(sys.argv[1]).expanduser().resolve()
        if len(sys.argv) > 1 else ROOT / "repos" / "activemq.git")
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 0     # 0 = 全部

CSV_PATH = ROOT / "out" / "gaps.csv"

if not REPO.exists():
    sys.exit(f"[X] 找不到仓库：{REPO}")
if not CSV_PATH.exists():
    sys.exit(f"[X] 找不到 {CSV_PATH}，请先跑 probe_szz.py")

TEST_DIR = re.compile(r"(^|/)src/test/|(^|/)test/", re.I)
TEST_NAME = re.compile(r"(Test|Tests|TestCase|IT|ITCase|IntegrationTest)\.java$")


def is_test(p):
    return bool(TEST_DIR.search(p) or TEST_NAME.search(p))


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


# ---------- 读 gaps.csv ----------
rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8-sig")))
commits = [r for r in rows if r.get("level") == "commit" and r.get("intro_sha")]
code_bics = [r["intro_sha"] for r in commits if r.get("is_noise") == "0"]
noise_bics = [r["intro_sha"] for r in commits if r.get("is_noise") == "1"]

if LIMIT:
    code_bics = code_bics[:LIMIT]

print(f"仓库   {REPO}")
print(f"数据   {CSV_PATH}")
print(f"BIC 总数（提交级去重）  {len(commits)}"
      f"   = 代码行命中 {len(code_bics)} + 仅非语义行命中 {len(noise_bics)}")
print(f"本次分析  {len(code_bics)} 个（代码行命中的）\n")

# ---------- 逐个取改动文件 ----------
cat = {"main": 0, "test": 0, "mixed": 0, "nonjava": 0, "empty": 0}
lines_main = lines_test = lines_other = 0
test_prefix = {}
other_prefix = {}
examples = {"test": [], "mixed": []}
done = 0

for sha in code_bics:
    done += 1
    if done % 200 == 0:
        print(f"  ...{done}/{len(code_bics)}")

    out = git("show", "--numstat", "--format=", sha)
    files, nfile = [], 0
    n_main = n_test = 0
    n_nonjava = 0

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        try:
            a, d = int(a), int(d)
        except ValueError:
            a = d = 0                      # 二进制文件显示为 "-"
        n = a + d
        nfile += 1
        files.append(path)
        if not path.endswith(".java"):
            n_nonjava += 1
            lines_other += n
            key = "/".join(path.split("/")[:4]) or path
            other_prefix[key] = other_prefix.get(key, 0) + n
        elif is_test(path):
            n_test += 1
            lines_test += n
            key = "/".join(path.split("/")[:4])
            test_prefix[key] = test_prefix.get(key, 0) + 1
        else:
            n_main += 1
            lines_main += n

    if nfile == 0:
        cat["empty"] += 1
        continue
    if n_main and n_test:
        cat["mixed"] += 1
        if len(examples["mixed"]) < 3:
            examples["mixed"].append((sha, files))
    elif n_test and not n_main:
        cat["test"] += 1
        if len(examples["test"]) < 5:
            examples["test"].append((sha, files))
    elif n_main:
        cat["main"] += 1
    else:
        cat["nonjava"] += 1

total_files = cat["main"] + cat["test"] + cat["mixed"] + cat["nonjava"] + cat["empty"]
total_lines = lines_main + lines_test + lines_other


def pct(a, b):
    return f"{a / b:6.1%}" if b else "   n/a"


print(f"\n{'=' * 72}")
print(f"BIC 改动文件构成（{total_files} 个引入提交）")
print(f"{'=' * 72}")
print(f"  只改主代码   {cat['main']:5d}  {pct(cat['main'], total_files)}")
print(f"  只改测试     {cat['test']:5d}  {pct(cat['test'], total_files)}   <-- 这些不是产品缺陷")
print(f"  混合         {cat['mixed']:5d}  {pct(cat['mixed'], total_files)}")
print(f"  只改非 Java  {cat['nonjava']:5d}  {pct(cat['nonjava'], total_files)}")
print(f"  取不到        {cat['empty']:5d}  {pct(cat['empty'], total_files)}")

print(f"\n{'=' * 72}")
print("改动行数构成")
print(f"{'=' * 72}")
print(f"  主代码   {lines_main:8,d} 行  {pct(lines_main, total_lines)}")
print(f"  测试     {lines_test:8,d} 行  {pct(lines_test, total_lines)}")
print(f"  非 Java  {lines_other:8,d} 行  {pct(lines_other, total_lines)}")

if test_prefix:
    print(f"\n{'=' * 72}")
    print("测试文件路径 Top10（按出现次数）")
    print(f"{'=' * 72}")
    for k, v in sorted(test_prefix.items(), key=lambda x: -x[1])[:10]:
        print(f"  {v:5d}  {k}")

if other_prefix:
    print(f"\n{'=' * 72}")
    print("非 Java 路径 Top10（按改动行数）—— 用来解释那 41.9% 是什么")
    print(f"{'=' * 72}")
    for k, v in sorted(other_prefix.items(), key=lambda x: -x[1])[:10]:
        print(f"  {v:8,d} 行  {k}")

for tag, title in (("test", "只改测试的 BIC 示例"), ("mixed", "混合改动的 BIC 示例")):
    if examples[tag]:
        print(f"\n{'=' * 72}")
        print(title)
        print(f"{'=' * 72}")
        for sha, files in examples[tag]:
            print(f"  {sha[:10]}   {len(files)} 个文件")
            for p in files[:4]:
                print(f"      {p}")
