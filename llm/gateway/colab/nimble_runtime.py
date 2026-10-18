"""Memory-bounded PEFT loading with the pinned official Nimble scoring implementation."""

import importlib
import re
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def plain_lora_loading(base):
    """Attach floating-point adapters despite Colab's optional package conflicts.

    PEFT probes an incompatible system torchao even for ordinary Linear layers,
    and scans training-only tensors in disk shards that Transformers ignores.
    Scope both workarounds to adapter loading and restore the original functions.
    """
    if getattr(base, "is_quantized", False) or getattr(base.config, "quantization_config", None):
        raise ValueError("Quantized Nimble base models are unsupported")
    from peft.tuners.lora import torchao

    original = torchao.is_torchao_available
    peft_model = importlib.import_module("peft.peft_model")
    original_open = peft_model.safe_open
    ignored = [
        re.compile(pattern)
        for pattern in (getattr(base, "_keys_to_ignore_on_load_unexpected", None) or [])
    ]

    class InferenceWeights:
        def __init__(self, handle):
            self.handle = handle

        def keys(self):
            # Qwen checkpoints include training-only MTP tensors. Transformers
            # already ignores them, but PEFT's disk-offload shard rewrite does
            # not, and otherwise looks up modules that do not exist.
            return [
                key
                for key in self.handle.keys()
                if not any(pattern.match(key) for pattern in ignored)
            ]

        def get_tensor(self, key):
            return self.handle.get_tensor(key)

        def metadata(self):
            return self.handle.metadata()

    @contextmanager
    def inference_open(*args, **kwargs):
        with original_open(*args, **kwargs) as handle:
            yield InferenceWeights(handle)

    torchao.is_torchao_available = lambda: False
    peft_model.safe_open = inference_open
    try:
        yield
    finally:
        torchao.is_torchao_available = original
        peft_model.safe_open = original_open


def host_memory_budget():
    lines = Path("/proc/meminfo").read_text().splitlines()
    available = next(
        int(line.split()[1]) * 1024 for line in lines if line.startswith("MemAvailable:")
    )
    return max(256 * 1024**2, int(available * 0.65))


def load_adapter_scorer(config, precision):
    import torch
    from nimble.scoring.calibration import fitted_temperature, resolve_temperature
    from nimble.scoring.cuda_scorer import (
        CudaCandidateScorer,
        execution_device,
        physical_weight,
    )
    from nimble.scoring.release_contract import prompt_builder
    from peft import PeftModel
    from transformers import AutoTokenizer, Qwen3_5ForConditionalGeneration

    class AdapterScorer(CudaCandidateScorer):
        def __init__(self):
            if not torch.cuda.is_available():
                raise ValueError("Nimble requires CUDA")
            budgets = {
                index: int(torch.cuda.mem_get_info(index)[0] * 0.8)
                for index in range(torch.cuda.device_count())
            }
            budgets["cpu"] = host_memory_budget()
            torch.backends.cuda.matmul.allow_tf32 = False
            loading = dict(
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
                dtype=getattr(torch, precision),
                device_map="auto",
                max_memory=budgets,
                offload_folder=config["offload_folder"],
                offload_state_dict=True,
                low_cpu_mem_usage=True,
                attn_implementation="sdpa",
                output_loading_info=True,
            )
            base, info = Qwen3_5ForConditionalGeneration.from_pretrained(
                config["snapshot"], **loading
            )
            if any(
                info.get(key)
                for key in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")
            ):
                raise ValueError("Base checkpoint did not load exactly")
            with plain_lora_loading(base):
                self.model = PeftModel.from_pretrained(
                    base,
                    config["adapter_snapshot"],
                    local_files_only=True,
                    low_cpu_mem_usage=True,
                    device_map="auto",
                    max_memory=budgets,
                    offload_folder=config["offload_folder"],
                ).eval()
            # PEFT's .model is the entire conditional-generation model. Scoring
            # needs its backbone, with LoRA modules still installed and active.
            underlying = self.model.get_base_model()
            head = underlying.get_output_embeddings()
            if getattr(head, "lora_A", None):
                raise ValueError("Candidate output head adapters are unsupported")
            self.backbone = underlying.model
            self.head_weight = physical_weight(head)
            self.device = execution_device(underlying.get_input_embeddings())
            if not any(p.device.type == "cuda" for p in self.model.parameters()):
                raise ValueError("No model weights were placed on CUDA")
            self.tokenizer = AutoTokenizer.from_pretrained(
                config["adapter_snapshot"],
                local_files_only=True,
                trust_remote_code=False,
            )
            self.prepare_prompts = prompt_builder(config["adapter_snapshot"], self.tokenizer)
            self.system_role = True
            self.model_id, self.revision = config["model_repo"], config["commit"]
            self.max_input_tokens = min(
                config["max_input_tokens"],
                underlying.config.text_config.max_position_embeddings - 1,
            )
            self.temperature = resolve_temperature(
                self.model_id, self.revision, None, model_path=config["adapter_snapshot"]
            )
            self.runtime = {
                "dtype": precision,
                "adapter": "peft",
                "device_map": "auto",
                "max_memory": {str(k): v for k, v in budgets.items()},
            }
            self.temperature_fitted = self.temperature == fitted_temperature(
                self.model_id, self.revision
            )

    return AdapterScorer()
