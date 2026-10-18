"""Executed in the Colab model environment, never in the gateway process."""

import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

if __package__:
    from .download import download_snapshot, report
else:
    from download import download_snapshot, report

# Scoring code and prompt hashes form part of the published checkpoint contract.
SCORER_COMMIT = "dcfdbd9a64f0d869f658d7a72f1beaee32737773"


def prepare(config):
    root = Path(config["root"])
    source = root / "nimble-source"
    source.mkdir(exist_ok=True)
    url = f"https://codeload.github.com/bespokelabsai/nimble/zip/{SCORER_COMMIT}"
    report("scorer_download", "running")
    with urllib.request.urlopen(url, timeout=120) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    prefix = f"nimble-{SCORER_COMMIT}/"
    # Only the scoring package is needed. Never extract arbitrary archive paths.
    for member in archive.namelist():
        if member.startswith(prefix + "nimble/") and member.endswith(".py"):
            relative = Path(member.removeprefix(prefix))
            if ".." in relative.parts:
                raise ValueError("Invalid scorer archive")
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    report("scorer_download", "done")
    report("contract", "running")
    sys.path.insert(0, str(source))
    snapshot = Path(config["snapshot"])
    contract = json.loads((snapshot / "schema_config.json").read_text())
    from nimble.training.candidate_schema import validate_contract
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        snapshot, local_files_only=True, trust_remote_code=False
    )
    validate_contract(contract, tokenizer)
    report("contract", "done")
    model_path = snapshot
    if (snapshot / "adapter_config.json").exists():
        # Download the exact base named by the release contract, including gated access.
        base_path = download_snapshot(
            contract["model"],
            step="base_download",
            stall_timeout=config.get("download_stall_timeout", 180),
            max_attempts=config.get("download_max_attempts", 3),
            retry_backoff=config.get("download_retry_backoff", 5),
            revision=contract["revision"],
            token=config.get("hf_token") or False,
            cache_dir=config["cache"],
            allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt", "*.jinja"],
        )
        # Keep the adapter separate: a CPU merge requires the complete 18 GiB
        # model plus temporary tensors, exceeding standard Colab RAM.
        report("merge", "skipped")
        return {
            "snapshot": base_path,
            "adapter_snapshot": str(snapshot),
            "commit": snapshot.name,
            "scorer_source": str(source),
            "offload_folder": str(root / "offload"),
            "max_input_tokens": min(config["max_context"], contract.get("max_length", 2048)),
        }
    else:
        report("base_download", "skipped")
        report("merge", "skipped")
    return {
        "snapshot": str(model_path),
        "commit": snapshot.name,
        "scorer_source": str(source),
        "max_input_tokens": min(config["max_context"], contract.get("max_length", 2048)),
    }


if __name__ == "__main__":
    print(json.dumps(prepare(json.load(sys.stdin))))
