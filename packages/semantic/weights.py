"""Restricted tensor-only conversion of the verified official legacy checkpoint."""

import shutil
from pathlib import Path
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.semantic import artifacts, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError


def convert(raw, output):
    import torch
    from transformers import RobertaConfig, RobertaModel

    raw, output = assets.safe_path(Path(raw)), assets.safe_path(Path(output))
    receipt = assets.read_json(raw / "acquisition.json", checkpoint)
    assets.verify_seal(receipt)
    if receipt["model"] != policy.MODEL or receipt["revision"] != policy.REVISION:
        raise SemanticError("SEMANTIC_CHECKPOINT_CONFLICT")
    if (
        receipt["files"]["pytorch_model.bin"]["sha256"]
        != "28b61fd8fa069f6bc966f4cb9572a4026ab2a784fca8fb224020d91b744e32d6"
    ):
        raise SemanticError("SEMANTIC_CHECKPOINT_CONFLICT")
    for name, record in receipt["files"].items():
        if Path(name).name != name or artifacts.file_digest(raw / name, checkpoint) != record:
            raise SemanticError("SEMANTIC_WEIGHT_DRIFT")
    torch.set_num_threads(policy.THREADS)
    state = torch.load(raw / "pytorch_model.bin", map_location="cpu", weights_only=True)
    if not isinstance(state, dict) or not 1 < len(state) < 1024:
        raise SemanticError("SEMANTIC_STATE_INVALID")
    for key, value in state.items():
        if (
            not isinstance(key, str)
            or type(value) is not torch.Tensor
            or value.layout != torch.strided
            or value.numel() > 100_000_000
        ):
            raise SemanticError("SEMANTIC_STATE_INVALID")
        if value.dtype not in {torch.float32, torch.int64} or not torch.isfinite(value).all():
            raise SemanticError("SEMANTIC_STATE_INVALID")
    config = RobertaConfig.from_pretrained(raw, local_files_only=True)
    if config.hidden_size != 768 or config.num_hidden_layers != 12 or config.vocab_size != 50265:
        raise SemanticError("SEMANTIC_CONFIG_INVALID")
    ignored = []
    for key in ["embeddings.position_ids", "embeddings.token_type_ids"]:
        if key in state:
            expected = (
                torch.arange(config.max_position_embeddings).reshape(1, -1)
                if key.endswith("position_ids")
                else torch.zeros_like(state[key])
            )
            if not torch.equal(state[key], expected):
                raise SemanticError("SEMANTIC_BUFFER_INVALID")
            state.pop(key)
            ignored.append(key)
    checkpoint()
    model = RobertaModel(config)
    model.load_state_dict(state, strict=True)
    model.requires_grad_(False)
    model.eval()
    output.mkdir(parents=True, exist_ok=True)
    attempt = assets.safe_path(output / (".convert-" + uuid4().hex))
    attempt.mkdir()
    model.save_pretrained(attempt, safe_serialization=True, max_shard_size="1GB")
    for name in (
        "vocab.json",
        "merges.txt",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "README.md",
        "upstream-LICENSE",
    ):
        shutil.copyfile(raw / name, attempt / name)
    checkpoint()
    names = sorted(p.name for p in attempt.iterdir() if p.is_file())
    if any(p.endswith(".bin") for p in names):
        raise SemanticError("SEMANTIC_UNSAFE_SERIALIZATION")
    metadata = {
        "version": "codebert-safe-conversion-v1",
        "original": receipt,
        "torch": torch.__version__,
        "weights_only": True,
        "ignored_verified_legacy_buffers": ignored,
        "remote_code_executed": False,
        "safe_format": "safetensors",
        "redistribution_approved": False,
    }
    # Load only the newly verified safetensors and compare all tensor values exactly.
    again = RobertaModel.from_pretrained(attempt, local_files_only=True, use_safetensors=True)
    if any(
        not torch.equal(value, again.state_dict()[key]) for key, value in model.state_dict().items()
    ):
        raise SemanticError("SEMANTIC_CONVERSION_DRIFT")
    metadata["conversion_sources"] = artifacts.sources(
        ["packages/semantic/weights.py", "packages/semantic/artifacts.py", "tools/semantic/uv.lock"]
    )
    checkpoint()
    return artifacts.publish(attempt, output, metadata, names)
