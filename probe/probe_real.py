# -*- coding: utf-8 -*-
"""
探针 ⑨：在真实 Git 仓库上测量 features.compute() 的耗时与内存

为什么不用 scripts/probe_causal_features.py：
    那条路径要导入 packages.repositories.clone -> safety -> dns，
    需要 dnspython 等第三方依赖，而当前环境装不上。
    本脚本只依赖标准库与纯内核 packages.mining.features，因此无需任何 pip 包。

口径（与 scripts/probe_features.py 的合成探针保持一致，才能对比）：
    history_coverage = "full"，即完整计算六项历史特征，包含 file_history 的回溯成本。
    为保证 full 的祖先闭包完整，窗口取「拓扑序最老的 N 个提交」，
    head 设为该窗口中最新的那个提交。

已知偏差（必须随结果一起引用）：
    **不含** Git blob 读取与解析：old_loc 用固定占位 1，LT 的值不准但不影响循环成本；
    真实生产路径里 old_loc 由解析阶段提供，不属内核 compute() 的耗时。

用法：
    python probe/probe_real.py <bare-repo> [--counts 200 500 1000]
"""
import argparse
import hashlib
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.mining.features import CommitInput, FileDelta, Snapshot, compute  # noqa: E402

REC = "@@"


def git(repo, *args, stdin=None):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        sys.exit(f"[X] git 失败：{args}\n{result.stderr[:500]}")
    return result.stdout


def parse_status(text):
    out, current = {}, None
    for line in text.splitlines():
        if line.startswith(REC):
            current = line[2:].strip()
            out[current] = []
        elif current is not None and line.strip():
            parts = line.split("\t")
            code = parts[0][0]
            if code in ("R", "C") and len(parts) == 3:
                out[current].append((code, parts[1], parts[2]))
            elif len(parts) == 2:
                out[current].append((code, None if code == "A" else parts[1],
                                     None if code == "D" else parts[1]))
    return out


def parse_numstat(text):
    out, current = {}, None
    for line in text.splitlines():
        if line.startswith(REC):
            current = line[2:].strip()
            out[current] = []
        elif current is not None and line.strip():
            parts = line.split("\t")
            if len(parts) >= 3:
                out[current].append((parts[0], parts[1]))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--counts", type=int, nargs="+", default=[200, 500, 1000])
    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        sys.exit(f"[X] 找不到仓库：{repo}")
    want = max(args.counts)

    # 拓扑序最老的 N 个提交：前缀天然满足祖先闭包，history_coverage="full" 才能通过校验
    order = git(repo, "rev-list", "--topo-order", "--reverse", "HEAD").split()
    if len(order) < want:
        sys.exit(f"[X] 仓库只有 {len(order)} 个提交，不足 {want}")
    window = order[:want]
    stdin = "\n".join(window) + "\n"

    print(f"[i] 仓库：{repo}")
    print(f"[i] 总提交 {len(order)}，取拓扑序最老的 {want} 个")
    meta_text = git(repo, "log", "--no-walk=unsorted", "--stdin",
                    "--format=@@%H|%P|%ct|%ae", stdin=stdin)
    status_text = git(repo, "log", "--no-walk=unsorted", "--stdin",
                      "--format=@@%H", "--name-status", "-M", stdin=stdin)
    numstat_text = git(repo, "log", "--no-walk=unsorted", "--stdin",
                       "--format=@@%H", "--numstat", "-M", stdin=stdin)

    status, numstat = parse_status(status_text), parse_numstat(numstat_text)
    records, skipped = [], 0
    for line in meta_text.splitlines():
        if not line.startswith(REC):
            continue
        sha, parents, stamp, email = line[2:].split("|", 3)
        entries, counts = status.get(sha, []), numstat.get(sha, [])
        if len(entries) != len(counts):
            sys.exit(f"[X] {sha[:12]} name-status 与 numstat 条数不一致：{len(entries)} vs {len(counts)}")
        files = []
        for (code, old, new), (added, deleted) in zip(entries, counts):
            binary = added == "-" or deleted == "-"
            try:
                files.append(FileDelta(
                    old_path=old, new_path=new, change_type=code,
                    insertions=0 if binary else int(added),
                    deletions=0 if binary else int(deleted),
                    old_loc=1,                      # 占位：blob 读取不计入内核计时
                    is_binary=binary,
                ))
            except Exception:
                skipped += 1
        records.append(CommitInput(
            sha, tuple(parents.split()) if parents else (),
            datetime.fromtimestamp(int(stamp), tz=timezone.utc).replace(tzinfo=None),
            hashlib.sha256(email.encode()).hexdigest(),
            tuple(files),
        ))

    print(f"[i] 取到 {len(records)} 个提交；跳过不合法路径 {skipped} 处")
    print(f"[i] 平均每提交文件数 {sum(len(r.files) for r in records) / len(records):.2f}")
    head = records[-1].sha

    results = []
    for count in args.counts:
        subset = tuple(records[:count])
        snapshot = Snapshot("probe-real", subset[-1].sha, subset, history_coverage="full")
        tracemalloc.start()
        started = time.perf_counter()
        compute(snapshot)
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        results.append((count, elapsed, peak))
        print(f"PASS real {count} commits, {elapsed:.3f}s, {peak / 1024**2:.3f} MiB Python peak")

    print("\n=== 汇总 ===")
    print(f"{'提交数':>8} {'耗时(s)':>10} {'每提交(ms)':>12} {'内存(MiB)':>11}")
    for count, elapsed, peak in results:
        print(f"{count:>8} {elapsed:>10.3f} {elapsed / count * 1000:>12.2f} {peak / 1024**2:>11.3f}")


if __name__ == "__main__":
    main()
