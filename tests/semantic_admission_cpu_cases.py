"""Actual local BPE and context budgets; no model download or accuracy claim."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from tokenizers import ByteLevelBPETokenizer
from transformers import RobertaTokenizer

from packages.mining import public_assets as assets
from packages.semantic.admission import hunk_groups, select_lines
from packages.semantic.encoder import windows


def test_actual_local_bpe_budget_and_unicode_coordinates():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent) as name:
        path = Path(name)
        bpe = ByteLevelBPETokenizer()
        bpe.train_from_iterator(
            ["class Example { int 中文 = 123; }\r\n"] * 20,
            vocab_size=300,
            special_tokens=["<s>", "<pad>", "</s>", "<unk>", "<mask>"],
        )
        bpe.save_model(str(path))
        (path / "tokenizer_config.json").write_text(
            json.dumps({"tokenizer_class": "RobertaTokenizer"})
        )
        tokenizer = RobertaTokenizer.from_pretrained(path, local_files_only=True)
        before = "".join(f"class Example {{ int 中文 = {i}; }}\r\n" for i in range(100))
        after = before.replace("= 5;", "= 505;").replace("= 95;", "= 995;")
        groups, _ = hunk_groups([(before, after)])
        value, audit = select_lines(
            groups["base"], lambda s: tokenizer.encode(s, add_special_tokens=False)
        )
        ids = tokenizer.encode(value, add_special_tokens=False)
        assert 0 < len(ids) <= 252 and audit["represented_hunks"] == 2
        assert all(
            c["line"] in list(range(3, 10)) + list(range(93, 100))
            for c in audit["selected_coordinates"]
        )
        chunks, coverage = windows(ids)
        assert sum(map(len, chunks)) == len(ids) and coverage["omitted_tokens"] == 0
        assert "\r\n" in value and all(f"= {i};" not in value for i in [505, 995])
