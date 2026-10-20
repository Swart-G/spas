import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from llm.gateway.colab import nimble_runtime


@pytest.fixture
def loading(monkeypatch):
    monkeypatch.setattr(nimble_runtime, "host_memory_budget", lambda: 4096)
    head = NS(weight="head")
    core = NS(
        model=object(),
        config=NS(text_config=NS(max_position_embeddings=4096)),
        get_output_embeddings=lambda: head,
        get_input_embeddings=lambda: "embedding",
    )
    wrapper = NS(
        model=object(),
        get_base_model=lambda: core,
        parameters=lambda: iter([NS(device=NS(type="cuda"))]),
    )
    wrapper.eval = lambda: wrapper
    base_loader = Mock(return_value=(core, {}))
    adapter_loader = Mock(return_value=wrapper)
    tokenizer = Mock()
    builder = Mock(return_value="verified-prompt-builder")
    modules = {
        "peft.peft_model": NS(safe_open=lambda *a, **k: None),
        "peft.tuners.lora": NS(torchao=NS(is_torchao_available=lambda: True)),
        "torch": NS(
            cuda=NS(
                is_available=lambda: True,
                device_count=lambda: 1,
                mem_get_info=lambda i: (10000, 20000),
            ),
            float16="fp16",
            backends=NS(cuda=NS(matmul=NS(allow_tf32=True))),
        ),
        "peft": NS(PeftModel=NS(from_pretrained=adapter_loader)),
        "transformers": NS(
            AutoTokenizer=NS(from_pretrained=tokenizer),
            Qwen3_5ForConditionalGeneration=NS(from_pretrained=base_loader),
        ),
        "nimble.scoring.calibration": NS(
            fitted_temperature=lambda *a: 1, resolve_temperature=lambda *a, **k: 1
        ),
        "nimble.scoring.cuda_scorer": NS(
            CudaCandidateScorer=type("OfficialScorer", (), {}),
            physical_weight=lambda h: h.weight,
            execution_device=lambda e: "cuda:0",
        ),
        "nimble.scoring.release_contract": NS(prompt_builder=builder),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    config = {
        "snapshot": "cached-base",
        "adapter_snapshot": "cached-adapter",
        "offload_folder": "runtime/offload",
        "model_repo": "bespokelabs/Bespoke-Nimble-9B",
        "commit": "adapter-commit",
        "max_input_tokens": 8192,
    }
    return config, core, base_loader, adapter_loader, builder


def test_adapter_loader_bounds_memory_and_scores_correct_peft_backbone(loading):
    config, core, base, adapter, builder = loading
    scorer = nimble_runtime.load_adapter_scorer(config, "float16")
    assert base.call_args.kwargs["max_memory"] == {0: 8000, "cpu": 4096}
    assert base.call_args.kwargs["low_cpu_mem_usage"] is True
    assert adapter.call_args.args[1] == "cached-adapter"
    assert adapter.call_args.kwargs["offload_folder"] == "runtime/offload"
    assert scorer.backbone is core.model
    assert scorer.revision == "adapter-commit" and scorer.max_input_tokens == 4095
    builder.assert_called_once_with("cached-adapter", scorer.tokenizer)


def test_incomplete_base_checkpoint_is_rejected_before_loading_adapter(loading):
    config, core, base, adapter, _ = loading
    base.return_value = (core, {"missing_keys": ["lm_head.weight"]})
    with pytest.raises(ValueError, match="exactly"):
        nimble_runtime.load_adapter_scorer(config, "float16")
    adapter.assert_not_called()


def test_candidate_head_adapter_cannot_be_silently_ignored(loading):
    config, core, _, _, _ = loading
    core.get_output_embeddings = lambda: NS(lora_A={"default": "trained-head"})
    with pytest.raises(ValueError, match="output head"):
        nimble_runtime.load_adapter_scorer(config, "float16")


def test_plain_loading_bypasses_incompatible_torchao_and_restores_on_error(monkeypatch):
    def incompatible():
        raise ImportError("Found an incompatible version of torchao")

    backend = NS(is_torchao_available=incompatible)
    monkeypatch.setitem(sys.modules, "peft.tuners.lora", NS(torchao=backend))
    monkeypatch.setitem(sys.modules, "peft.peft_model", NS(safe_open=lambda *a, **k: None))
    base = NS(is_quantized=False, config=NS())
    with pytest.raises(RuntimeError):
        with nimble_runtime.plain_lora_loading(base):
            assert backend.is_torchao_available() is False
            raise RuntimeError("adapter failed")
    assert backend.is_torchao_available is incompatible
    with pytest.raises(ValueError, match="Quantized"):
        with nimble_runtime.plain_lora_loading(NS(is_quantized=True)):
            pass


def test_disk_shard_filter_only_omits_framework_declared_training_weights(monkeypatch):
    from contextlib import contextmanager

    backend = NS(is_torchao_available=lambda: True)
    monkeypatch.setitem(sys.modules, "peft.tuners.lora", NS(torchao=backend))
    handle = NS(
        keys=lambda: [
            "mtp.layers.0.input_layernorm.weight",
            "model.language_model.layers.0.weight",
        ],
        get_tensor=lambda key: key,
        metadata=lambda: {"format": "pt"},
    )

    @contextmanager
    def original(*args, **kwargs):
        yield handle

    module = NS(safe_open=original)
    monkeypatch.setitem(sys.modules, "peft.peft_model", module)
    base = NS(config=NS(), _keys_to_ignore_on_load_unexpected=[r"^mtp.*"])
    with nimble_runtime.plain_lora_loading(base):
        with module.safe_open("shard", framework="pt") as weights:
            assert weights.keys() == ["model.language_model.layers.0.weight"]
            assert weights.get_tensor(weights.keys()[0]) == weights.keys()[0]
            assert weights.metadata() == {"format": "pt"}
    assert module.safe_open is original
