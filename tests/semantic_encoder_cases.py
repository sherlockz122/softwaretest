"""Explicit isolated CPU tests; tiny/random fixtures are engineering evidence only."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import pytest
import torch
from transformers import RobertaConfig, RobertaModel

from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical, content_hash
from packages.semantic import artifacts, encoder, policy
from packages.semantic.policy import SemanticError

torch.set_num_threads(2)
if torch.get_num_interop_threads() != 1:
    torch.set_num_interop_threads(1)


@pytest.fixture
def work():
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="semantic-cpu-test-", dir=root) as name:
        yield Path(name)


class IdentityModel(torch.nn.Module):
    def __init__(self, hidden=2):
        super().__init__()
        self.config = SimpleNamespace(hidden_size=hidden)
        self.embeddings = torch.nn.Embedding(32, hidden)
        with torch.no_grad():
            self.embeddings.weight.copy_(torch.arange(32).reshape(-1, 1).expand(32, hidden))

    def forward(self, input_ids, attention_mask):
        return SimpleNamespace(last_hidden_state=self.embeddings(input_ids))


def test_masked_mean_excludes_only_structure_and_padding():
    model = IdentityModel()
    model.eval()
    model.requires_grad_(False)
    result = encoder.infer(model, [[0, 7, 2], [0, 3, 4, 2]], 1)
    assert np.array_equal(result, np.array([[7, 7], [3.5, 3.5]], dtype=np.float32))


def test_tiny_roberta_frozen_forward_repeat_and_content_change():
    torch.manual_seed(42)
    model = RobertaModel(
        RobertaConfig(
            vocab_size=32,
            hidden_size=8,
            intermediate_size=16,
            num_attention_heads=2,
            num_hidden_layers=1,
        )
    )
    model.eval()
    model.requires_grad_(False)
    first = encoder.infer(model, [[0, 7, 8, 2]], 1)
    assert np.array_equal(first, encoder.infer(model, [[0, 7, 8, 2]], 1))
    assert not np.array_equal(first, encoder.infer(model, [[0, 9, 8, 2]], 1))
    assert all(not p.requires_grad for p in model.parameters())


def fixtures(work, safe=True):
    root = work / "inputs"
    root.mkdir()
    stage = root / ".stage"
    stage.mkdir()
    rows = []
    for i in range(2):
        roles = {"message": "short", "added": "a" * 127 + "b" * 173, "deleted": "", "base": ""}
        rows.append(
            {
                "roles": roles,
                "text_hash": content_hash(roles),
                "member": {"commit_id": f"{i:040x}", "partition": "train", "label": str(i)},
            }
        )
    (stage / "records.jsonl").write_bytes(b"".join(canonical(r) + b"\n" for r in rows))
    inputs = artifacts.publish(
        stage, root, {"accepted": 2, "fixture_not_real_effect": True}, ["records.jsonl"]
    )
    root = work / "checkpoint"
    root.mkdir()
    stage = root / ".stage"
    stage.mkdir()
    (stage / "fixture.txt").write_bytes(b"no real pretrained weights in this inert test")
    checkpoint = artifacts.publish(
        stage,
        root,
        {
            "safe_format": "safetensors" if safe else "pickle",
            "weights_only": True,
            "original": {"revision": policy.REVISION},
        },
        ["fixture.txt"],
    )
    return inputs, checkpoint


def test_role_token_weighting_empty_masks_cache_reuse_and_damage(work, monkeypatch):
    inputs, checkpoint = fixtures(work)

    class Tokenizer:
        pad_token_id = 1
        bos_token_id = 0
        eos_token_id = 2

        def encode(self, text, add_special_tokens=False):
            return [5 + ord(c) % 10 for c in text]

    monkeypatch.setattr(
        "transformers.RobertaTokenizer.from_pretrained", lambda *a, **k: Tokenizer()
    )
    monkeypatch.setattr(
        "transformers.RobertaModel.from_pretrained", lambda *a, **k: IdentityModel(768)
    )
    target = encoder.encode(inputs, checkpoint, work / "outputs")
    values = np.load(target / "embeddings.npy", allow_pickle=False)
    assert values.shape == (2, 3076) and values.dtype == np.float32
    assert values[0, 768] == pytest.approx((126 * 12 + 48 * 13) / 174, abs=1e-6)
    assert np.array_equal(values[0, 3072:], np.array([1, 1, 0, 0]))
    assert np.count_nonzero(values[:, 1536:3072]) == 0

    def forbidden(*args, **kwargs):
        raise AssertionError("verified cache must skip model loading")

    monkeypatch.setattr("transformers.RobertaModel.from_pretrained", forbidden)
    assert encoder.encode(inputs, checkpoint, work / "outputs") == target
    coverage = json.loads((target / "coverage.json").read_text())
    assert coverage["records"][0]["roles"]["added"]["omitted_tokens"] == 126
    with (target / "embeddings.npy").open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(SemanticError, match="ARTIFACT_DRIFT"):
        encoder.encode(inputs, checkpoint, work / "outputs")


def test_unsafe_checkpoint_rejected_before_loading(work):
    inputs, checkpoint = fixtures(work, safe=False)
    with pytest.raises(SemanticError, match="UNSAFE_CHECKPOINT"):
        encoder.encode(inputs, checkpoint, work / "outputs")
