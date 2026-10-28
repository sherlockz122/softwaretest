"""Tiny frozen models and real local BPE verify pooling; no quality claims/downloads."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import torch
from tokenizers import ByteLevelBPETokenizer
from transformers import RobertaConfig, RobertaModel, RobertaTokenizer

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import coverage, encoder
from packages.semantic import coverage_encoder as e
from tests.semantic_encoder_cases import IdentityModel


def test_short_tail_content_token_weighting_and_empty_role_masks():
    model = IdentityModel()
    model.eval()
    model.requires_grad_(False)
    jobs = [[0, *([10] * 126), 2], [0, 20, 2]]
    values, repeated = e.aggregate(
        jobs, [(0, 1), (0, 1)], lambda batch: encoder.infer(model, batch, 1), 1, hidden=2
    )
    assert repeated and np.array_equal(values[0, 2:4], np.full(2, np.float32(1280 / 127)))
    assert values[0, 2] != 15 and values[0, -4:].tolist() == [0, 1, 0, 0]
    assert not values[0, :2].any() and not values[0, 4:8].any()


def test_tiny_roberta_eight_window_frozen_repeat_and_changed_content():
    torch.manual_seed(37)
    model = RobertaModel(
        RobertaConfig(
            vocab_size=32,
            hidden_size=8,
            intermediate_size=16,
            num_attention_heads=2,
            num_hidden_layers=1,
        )
    )
    model.requires_grad_(False)
    model.eval()
    jobs = [[0, *([3 + i] * 126), 2] for i in range(8)]

    def forward(batch):
        return encoder.infer(model, batch, 1)

    first, _ = e.aggregate(jobs, [(0, 1)] * 8, forward, 1, hidden=8)
    second, _ = e.aggregate(jobs, [(0, 1)] * 8, forward, 1, hidden=8)
    assert np.array_equal(first, second) and first.shape == (1, 36)
    changed = [list(job) for job in jobs]
    changed[0][1] = 20
    third, _ = e.aggregate(changed, [(0, 1)] * 8, forward, 1, hidden=8)
    assert not np.array_equal(first, third) and all(not p.requires_grad for p in model.parameters())


def test_actual_bpe_whole_line_coverage_fits_all_eight_code_windows():
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="coverage-bpe-") as name:
        path = Path(name)
        bpe = ByteLevelBPETokenizer()
        bpe.train_from_iterator(
            ["class A { int 中文 = 123; }\r\n"] * 20,
            vocab_size=300,
            special_tokens=["<s>", "<pad>", "</s>", "<unk>", "<mask>"],
        )
        bpe.save_model(str(path))
        (path / "tokenizer_config.json").write_text(
            json.dumps({"tokenizer_class": "RobertaTokenizer"})
        )
        tokenizer = RobertaTokenizer.from_pretrained(path, local_files_only=True)

        def tokenize(s):
            return tokenizer.encode(s, add_special_tokens=False)

        groups = [
            {
                "hunk": i,
                "eligible_lines": 1,
                "candidates": [(i, 0, f"class A {{ int 中文 = {i}; }}\r\n")],
            }
            for i in range(40)
        ]
        value, audit = coverage.select_lines(groups, tokenize)
        assert 252 < len(tokenize(value)) <= 1008
        row = {
            "member": {"partition": "train"},
            "roles": {"message": "hi", "added": value, "deleted": "", "base": ""},
        }
        row["text_hash"] = content_hash(row["roles"])
        jobs, locations, audits = e.prepare_jobs([row], tokenize)
        assert 2 < sum(role == 1 for _, role in locations) <= 8
        assert audits[0]["roles"]["added"]["omitted_tokens"] == 0
        assert all(len(job) <= 128 and job[0] == 0 and job[-1] == 2 for job in jobs)
        assert audit["selected_tokens"] == audits[0]["roles"]["added"]["covered_tokens"]
