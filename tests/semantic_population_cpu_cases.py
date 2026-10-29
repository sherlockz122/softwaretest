"""Tiny actual CPU84-tail pooling; independent of pretrained scientific results."""

import numpy as np
import torch
from transformers import RobertaConfig, RobertaModel

from packages.semantic import coverage_encoder, encoder
from tests.semantic_encoder_cases import IdentityModel


def test_actual84_tail_has_no_padding_and_independent_weighted_expectations():
    model = IdentityModel()
    model.eval()
    model.requires_grad_(False)
    jobs, locations = [], []
    for i in range(84):
        token = i % 14
        jobs.extend([[0, token + 3, token + 3, 2], [0, 2 * token + 4, 2]])
        locations.extend([(i, 1), (i, 1)])
    values, repeated = coverage_encoder.aggregate(
        jobs, locations, lambda batch: encoder.infer(model, batch, 1), 84, hidden=2
    )
    assert values.shape == (84, 12) and repeated
    expected = np.asarray(
        [(2 * (i % 14 + 3) + 2 * (i % 14) + 4) / 3 for i in range(84)], np.float32
    )
    np.testing.assert_array_equal(values[:, 2], expected)
    np.testing.assert_array_equal(values[:, 3], expected)
    np.testing.assert_array_equal(values[:, -4:], np.tile([0, 1, 0, 0], (84, 1)))


def test_tiny_frozen84_tail_repeat_and_last_real_member_changes():
    torch.manual_seed(97)
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
    jobs = [[0, 3 + i % 40, 10, 2] for i in range(84)]

    def run(batch_jobs):
        return coverage_encoder.aggregate(
            batch_jobs,
            [(i, 1) for i in range(84)],
            lambda batch: encoder.infer(model, batch, 1),
            84,
            hidden=8,
        )[0]

    first = run(jobs)
    assert first.shape == (84, 36) and np.array_equal(first, run(jobs))
    changed = [list(j) for j in jobs]
    changed[-1][1] = 55
    assert not np.array_equal(first[-1], run(changed)[-1])
    assert all(not parameter.requires_grad for parameter in model.parameters())
