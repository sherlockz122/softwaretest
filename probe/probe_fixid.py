# -*- coding: utf-8 -*-
"""
探针 ⑦：FIX 是否该加入 Kamei 的「缺陷编号」条款（只读提交信息，秒级完成）

背景：
    Kamei 2013 的 FIX 维度是「关键词 bug/fix/defect/patch 或者缺陷编号」。
    本项目只用了关键词。本脚本量化「补上编号」的影响面，判断值不值得改规则。

口径（与 probe_szz.py 一致，这样结论可直接对接 SZZ 实测）：
    默认分支 / SINCE 起 / 剔合并 / 剔机器人 / 搜完整消息而非仅标题。

注意两个坑：
    1. `#1234` 在 GitHub 仓库上多为 PR 编号（squash-merge 的 `(#1234)` 后缀），不是缺陷编号。
    2. `AMQ-1234` 是 JIRA 编号，但 JIRA 混了 Bug / New Feature / Improvement，
       不能只凭「有编号」判定为缺陷修复。

用法：
    python probe_fixid.py
    python probe_fixid.py <仓库路径> [起始年份]
"""
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if len(sys.argv) > 1:
    REPO = Path(sys.argv[1]).expanduser().resolve()
else:
    cand = ROOT / "repos" / "activemq.git"
    REPO = cand if cand.exists() else ROOT / "repos" / "commons-cli.git"
if not REPO.exists():
    sys.exit(f"[X] 找不到仓库：{REPO}\n    用法：python probe_fixid.py <仓库路径> [起始年份]")

YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2013

print(f"[i] 仓库：{REPO}")
print(f"[i] 口径：默认分支 / {YEAR} 起 / 剔合并 / 剔机器人 / 完整消息\n")

# 与 probe_szz.py 保持一致的规则 C 与机器人正则
KEY = re.compile(r"\b(fix(e[sd])?|bug|patch|defect)\b", re.I)
BOT = re.compile(r"\[bot\]|dependabot|renovate|github-actions|greenkeeper", re.I)
JIRA = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
HASH = re.compile(r"(?<![\w&-])#\d{2,}\b")
PRTAIL = re.compile(r"\(#\d+\)\s*$")
FEATISH = re.compile(
    r"\b(add|support|implement|introduc|feature|improve|enhance|refactor|"
    r"update|upgrade|bump|allow|enable|remove|cleanup|clean up|deprecat|"
    r"migrat|rename|document|readme|test|build)\b", re.I)
BUGISH = re.compile(r"\b(fix|bug|issue|error|exception|incorrect|wrong|broken|fail|regress|hang|leak)\b", re.I)

SEP, REC = "\x1f", "\x1e"
out = subprocess.run(
    ["git", "-C", str(REPO), "log", f"--format=%H{SEP}%an{SEP}%at{SEP}%P{SEP}%s{SEP}%B{REC}"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
).stdout

cut = int(datetime(YEAR, 1, 1, tzinfo=timezone.utc).timestamp())
rows, dropped = [], {"merge": 0, "bot": 0, "before": 0}
for rec in out.split(REC):
    rec = rec.strip("\r\n")
    if not rec.strip():
        continue
    p = rec.split(SEP)
    if len(p) < 6:
        continue
    an, at, parents, subj = p[1], p[2], p[3], p[4]
    body = SEP.join(p[5:])
    if len(parents.split()) > 1:
        dropped["merge"] += 1
        continue
    if BOT.search(an):
        dropped["bot"] += 1
        continue
    if int(at or 0) < cut:
        dropped["before"] += 1
        continue
    rows.append({"subj": subj, "key": bool(KEY.search(body)),
                 "jira": bool(JIRA.search(body)), "hash": bool(HASH.search(body)),
                 "prtail": bool(PRTAIL.search(subj))})

n = len(rows)
key = [r for r in rows if r["key"]]
jira_only = [r for r in rows if r["jira"] and not r["key"]]
hash_only = [r for r in rows if r["hash"] and not r["jira"] and not r["key"]]
feat = [r for r in jira_only if FEATISH.search(r["subj"]) and not BUGISH.search(r["subj"])]
bugg = [r for r in jira_only if BUGISH.search(r["subj"])]

print(f"剔除：合并 {dropped['merge']} / 机器人 {dropped['bot']} / {YEAR} 前 {dropped['before']}")
print(f"参与统计：{n} 条\n")
print(f"规则 C（关键词，完整消息）命中   {len(key):6d}  {len(key) / n * 100:5.2f}%")
print(f"  仅 JIRA 编号、关键词漏掉       {len(jira_only):6d}  相对增量 {len(jira_only) / max(len(key), 1) * 100:6.2f}%")
print(f"  仅 # 编号、无 JIRA             {len(hash_only):6d}  相对增量 {len(hash_only) / max(len(key), 1) * 100:6.2f}%")
print(f"    其中行尾 (#NNNN) = PR 特征   {sum(r['prtail'] for r in hash_only):6d}")

print("\nJIRA-only 的语义构成（按标题粗判，仅作趋势参考）")
print(f"  像功能/改进   {len(feat):6d}  {len(feat) / max(len(jira_only), 1) * 100:5.2f}%")
print(f"  像缺陷        {len(bugg):6d}  {len(bugg) / max(len(jira_only), 1) * 100:5.2f}%")
print(f"  无法判断      {len(jira_only) - len(feat) - len(bugg):6d}")

total = len(key) + len(jira_only) + len(hash_only)
print(f"\n若全量采纳缺陷编号：fix {len(key)} → {total}，占比 {len(key) / n * 100:.1f}% → {total / n * 100:.1f}%")

print("\n--- 像功能的样例（前 10）---")
for r in feat[:10]:
    print(f"  {r['subj'][:92]}")
