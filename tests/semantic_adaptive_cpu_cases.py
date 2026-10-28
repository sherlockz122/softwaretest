"""Local tiny-model32-window CPU evidence; never a product-quality evaluation."""

import numpy as np
import torch
from transformers import RobertaConfig, RobertaModel

from packages.semantic import adaptive_encoder, coverage_encoder, encoder
from tests.semantic_encoder_cases import IdentityModel


def test_32_windows_and_short_tail_have_content_token_weighted_pooling():
    chunks, audit = adaptive_encoder.windows([10] * (31 * 126) + [20])
    jobs = [[0, *chunk, 2] for chunk in chunks]
    model = IdentityModel()
    model.eval()
    model.requires_grad_(False)
    values, repeated = coverage_encoder.aggregate(
        jobs, [(0, 1)] * len(jobs), lambda batch: encoder.infer(model, batch, 1), 1, hidden=2
    )
    expected = np.float32((31 * 126 * 10 + 20) / (31 * 126 + 1))
    assert len(chunks) == 32 and audit["omitted_tokens"] == 0
    assert repeated and np.array_equal(values[0, 2:4], np.full(2, expected))
    assert values[0, -4:].tolist() == [0, 1, 0, 0]


def test_tiny_frozen_roberta_32_windows_repeat_and_last_window_content_matters():
    torch.manual_seed(43)
    model = RobertaModel(
        RobertaConfig(
            vocab_size=64,
            hidden_size=8,
            intermediate_size=16,
            num_attention_heads=2,
            num_hidden_layers=1,
        )
    )
    model.eval()
    model.requires_grad_(False)
    chunks, _ = adaptive_encoder.windows([3 + i % 40 for i in range(4032)])
    jobs = [[0, *chunk, 2] for chunk in chunks]

    def execute(values):
        return coverage_encoder.aggregate(
            values,
            [(0, 1)] * len(values),
            lambda batch: encoder.infer(model, batch, 1),
            1,
            hidden=8,
        )[0]

    first = execute(jobs)
    assert np.array_equal(first, execute(jobs))
    changed = [list(j) for j in jobs]
    changed[-1][-2] = 50
    assert not np.array_equal(first, execute(changed))
    assert all(not p.requires_grad for p in model.parameters())
