import io
import json
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import Mock

from llm.gateway.colab import nimble_setup


def test_nimble_preparation_keeps_adapter_separate_and_never_loads_base_into_cpu(
    tmp_path, monkeypatch
):
    snapshot = tmp_path / "adapter-commit"
    snapshot.mkdir()
    contract = {"model": "Qwen/Qwen3.5-9B", "revision": "pinned-base-commit", "max_length": 8192}
    (snapshot / "schema_config.json").write_text(json.dumps(contract))
    (snapshot / "adapter_config.json").write_text("{}")
    (snapshot / "adapter_model.safetensors").write_bytes(b"adapter")
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr(f"nimble-{nimble_setup.SCORER_COMMIT}/nimble/__init__.py", "")
    monkeypatch.setattr(
        nimble_setup.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(archive_bytes.getvalue()),
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    download = Mock(return_value=str(tmp_path / "base"))
    monkeypatch.setattr(nimble_setup, "download_snapshot", download)
    validate = Mock()
    tokenizer = Mock()
    base_model = Mock()
    merged = Mock()

    def save(path, **kwargs):
        Path(path).mkdir(exist_ok=True)

    merged.save_pretrained.side_effect = save
    adapter = Mock()
    adapter.merge_and_unload.return_value = merged
    peft_model = Mock()
    peft_model.from_pretrained.return_value = adapter
    tokenizer_cls = Mock()
    tokenizer_cls.from_pretrained.return_value = tokenizer
    base_cls = Mock()
    base_cls.from_pretrained.return_value = base_model
    modules = {
        "huggingface_hub": types.SimpleNamespace(snapshot_download=download),
        "torch": types.SimpleNamespace(bfloat16="bf16"),
        "peft": types.SimpleNamespace(PeftModel=peft_model),
        "transformers": types.SimpleNamespace(
            AutoTokenizer=tokenizer_cls, Qwen3_5ForConditionalGeneration=base_cls
        ),
        "nimble.training.candidate_schema": types.SimpleNamespace(validate_contract=validate),
    }
    for key, module in modules.items():
        monkeypatch.setitem(sys.modules, key, module)
    config = {
        "root": str(tmp_path),
        "snapshot": str(snapshot),
        "cache": str(tmp_path / "cache"),
        "model_repo": "bespokelabs/Bespoke-Nimble-9B",
        "hf_token": "private-hf",
        "max_context": 16000,
    }
    prepared = nimble_setup.prepare(config)
    download.assert_called_once_with(
        contract["model"],
        step="base_download",
        stall_timeout=180,
        max_attempts=3,
        retry_backoff=5,
        revision=contract["revision"],
        token="private-hf",
        cache_dir=config["cache"],
        allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt", "*.jinja"],
    )
    validate.assert_called_once_with(contract, tokenizer)
    adapter.merge_and_unload.assert_not_called()
    base_cls.from_pretrained.assert_not_called()
    assert prepared["max_input_tokens"] == 8192
    assert prepared["commit"] == snapshot.name
    assert prepared["snapshot"] == str(tmp_path / "base")
    assert prepared["adapter_snapshot"] == str(snapshot)
    assert prepared["offload_folder"] == str(tmp_path / "offload")
    assert not list(tmp_path.glob("nimble-merged-*"))
    assert "private-hf" not in json.dumps(prepared)
    # Repeated setup only resolves cached paths and never allocates an 18 GiB model.
    nimble_setup.prepare(config)
    base_cls.from_pretrained.assert_not_called()
