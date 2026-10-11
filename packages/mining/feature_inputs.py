"""Read-only, bounded Git mode/blob verification before freezing parser records."""

from collections import OrderedDict

from packages.mining.features import FeatureInputError, freeze_record
from packages.repositories.parse_policy import INDEX_LIMIT
from packages.repositories.parser import sha_valid


def freeze_native_record(reader, record, fix=None):
    commit = record["commit"]

    def tree(sha):
        sha_valid(sha.encode("ascii"))
        if not hasattr(reader, "feature_trees"):
            reader.feature_trees = OrderedDict()
            reader.feature_tree_bytes = 0
        if sha in reader.feature_trees:
            reader.feature_trees.move_to_end(sha)
            return reader.feature_trees[sha][0]
        raw = reader.command(["ls-tree", "-rz", "--full-tree", sha, "--"], INDEX_LIMIT)
        entries = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            metadata, path = item.split(b"\t", 1)
            mode, _, blob = metadata.split()
            entries[path] = (mode.decode("ascii"), blob.decode("ascii"))
        if len(raw) <= 8 * 1024 * 1024:
            while reader.feature_trees and (
                reader.feature_tree_bytes + len(raw) > 8 * 1024 * 1024
                or len(reader.feature_trees) >= 64
            ):
                _, (_, size) = reader.feature_trees.popitem(last=False)
                reader.feature_tree_bytes -= size
            reader.feature_trees[sha] = entries, len(raw)
            reader.feature_tree_bytes += len(raw)
        return entries

    old = tree(commit["parents"][0]) if len(commit["parents"]) == 1 else {}
    new = tree(commit["sha"])
    files = []
    for file in record["files"]:
        modes = {}
        for prefix, entries in (("old", old), ("new", new)):
            path = file[prefix + "_path"]
            if path is None:
                mode = "000000"
                if set(file[prefix + "_blob"]) != {"0"}:
                    raise FeatureInputError("FEATURE_BLOB_CONFLICT")
            else:
                entry = entries.get(path.encode("utf-8"))
                if entry is None or entry[1] != file[prefix + "_blob"]:
                    raise FeatureInputError("FEATURE_BLOB_CONFLICT")
                mode = entry[0]
            modes[prefix + "_mode"] = mode
        files.append(file | modes)
    return freeze_record(record | {"files": files}, fix)
