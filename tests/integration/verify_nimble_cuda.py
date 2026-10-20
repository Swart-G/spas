"""Optional real-CUDA equivalence check using tiny Qwen3.5 and nonzero LoRA weights."""

import io
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import torch
from peft import LoraConfig, PeftModel, get_peft_model
from safetensors.torch import load_file, save_file
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from transformers import PreTrainedTokenizerFast, Qwen3_5Config, Qwen3_5ForConditionalGeneration

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from llm.gateway.colab.nimble_runtime import load_adapter_scorer, plain_lora_loading
from llm.gateway.colab.nimble_setup import SCORER_COMMIT

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    source = root / "source"
    source.mkdir()
    with urllib.request.urlopen(
        f"https://codeload.github.com/bespokelabsai/nimble/zip/{SCORER_COMMIT}"
    ) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    prefix = f"nimble-{SCORER_COMMIT}/"
    for name in archive.namelist():
        if name.startswith(prefix + "nimble/") and name.endswith(".py"):
            path = source / name.removeprefix(prefix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(name))
    sys.path.insert(0, str(source))
    c = Qwen3_5Config(
        text_config={
            "vocab_size": 32,
            "hidden_size": 32,
            "intermediate_size": 64,
            "num_hidden_layers": 2,
            "num_attention_heads": 2,
            "num_key_value_heads": 1,
            "head_dim": 16,
            "layer_types": ["linear_attention", "full_attention"],
            "linear_key_head_dim": 16,
            "linear_value_head_dim": 16,
            "linear_num_key_heads": 2,
            "linear_num_value_heads": 2,
            "max_position_embeddings": 2048,
            "rope_parameters": {
                "rope_type": "default",
                "rope_theta": 10000,
                "mrope_section": [1, 1, 2],
                "partial_rotary_factor": 0.5,
            },
        },
        vision_config={
            "hidden_size": 16,
            "intermediate_size": 32,
            "num_heads": 2,
            "depth": 1,
            "out_hidden_size": 32,
            "num_position_embeddings": 16,
        },
    )
    model = Qwen3_5ForConditionalGeneration(c).eval()
    base = root / "base"
    model.save_pretrained(base)
    weights_path = base / "model.safetensors"
    weights = load_file(weights_path)
    weights["mtp.layers.0.input_layernorm.weight"] = torch.ones(32)
    save_file(weights, weights_path, metadata={"format": "pt"})
    adapter = get_peft_model(
        model,
        LoraConfig(
            r=2,
            lora_alpha=4,
            target_modules=[
                "q_proj",
                "v_proj",
                "in_proj_qkv",
                "in_proj_z",
                "in_proj_a",
                "in_proj_b",
            ],
            task_type="CAUSAL_LM",
        ),
    ).eval()
    with torch.no_grad():
        for name, parameter in adapter.named_parameters():
            if "lora_B" in name:
                parameter.normal_(0, 0.1)
    adapter_path = root / "adapter"
    adapter.save_pretrained(adapter_path)
    vocab = {"[UNK]": 0, "[PAD]": 1, **{chr(65 + i): i + 2 for i in range(26)}}
    t = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
    t.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=t, unk_token="[UNK]", pad_token="[PAD]")
    tokenizer.chat_template = (
        "{% for message in messages %}{{ message['role'] }} : {{ message['content'] }}\n"
        "{% endfor %}{{ 'assistant :\n' }}"
    )
    tokenizer.save_pretrained(adapter_path)
    config = {
        "snapshot": str(base),
        "adapter_snapshot": str(adapter_path),
        "offload_folder": str(root / "offload"),
        "model_repo": "test/tiny",
        "commit": "test",
        "max_input_tokens": 2048,
    }
    scorer = load_adapter_scorer(config, "float16")
    schema = {"incident": {"type": "boolean", "description": "Is there an outage?"}}
    result = scorer.score("The service is unavailable.", schema)
    print("Native CUDA scoring:", result["output"], result["fields"]["incident"]["scores"])
    assert sum(result["fields"]["incident"]["scores"].values()) > 0.999
    from nimble.scoring.cuda_scorer import CudaCandidateScorer

    merged = scorer.model.merge_and_unload(safe_merge=True)
    merged_path = root / "merged"
    merged.save_pretrained(merged_path)
    tokenizer.save_pretrained(merged_path)
    reference = CudaCandidateScorer(
        str(merged_path),
        "test/tiny",
        "test",
        max_input_tokens=2048,
        dtype="float16",
        device_map="auto",
    )
    expected = reference.score("The service is unavailable.", schema)
    a = torch.tensor(list(result["fields"]["incident"]["logits"].values()))
    b = torch.tensor(list(expected["fields"]["incident"]["logits"].values()))
    print("Unmerged vs merged logits:", a.tolist(), b.tolist())
    assert torch.allclose(a, b, atol=0.01, rtol=0.01)
    # Exercise the real PEFT disk-shard rewrite that failed on the T4 runtime.
    disk_map = {
        "model.visual": 0,
        "model.language_model.embed_tokens": 0,
        "model.language_model.layers.0": 0,
        "model.language_model.layers.1": "disk",
        "model.language_model.norm": 0,
        "model.language_model.rotary_emb": 0,
        "lm_head": 0,
    }
    disk_base = Qwen3_5ForConditionalGeneration.from_pretrained(
        base, dtype=torch.float32, device_map=disk_map, offload_folder=str(root / "disk")
    )
    with plain_lora_loading(disk_base):
        disk_model = PeftModel.from_pretrained(
            disk_base,
            adapter_path,
            low_cpu_mem_usage=True,
            device_map={"base_model.model." + name: device for name, device in disk_map.items()},
            offload_folder=str(root / "disk"),
        ).eval()
    with torch.no_grad():
        output = disk_model(input_ids=torch.tensor([[2, 3, 4]], device="cuda"))
        merged_output = reference.model(input_ids=torch.tensor([[2, 3, 4]], device="cuda"))
    assert torch.isfinite(output.logits).all()
    assert torch.allclose(output.logits.float(), merged_output.logits.float(), atol=0.01, rtol=0.01)
    print("PASS: real CUDA Qwen3.5, nonzero LoRA, official scorer, bounded loader.")
