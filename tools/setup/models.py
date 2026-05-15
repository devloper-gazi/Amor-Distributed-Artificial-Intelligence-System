"""
Model bootstrap helpers — judge GGUFs + Ollama tags.

These are SLOW (multi-GB downloads).  Every helper here:
  * Is idempotent (skip if already present).
  * Prints progress to stdout.
  * Returns a structured result so `install` can summarise.

Judge GGUFs go into the `amor_custom-models-data` named volume under
`/data/custom_models/judge/`.  Ollama tags go into the `amor-ollama`
container via `ollama pull`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from tools.setup import compose, constants, util


@dataclass
class ModelResult:
    name: str
    action: str          # "present" | "pulled" | "failed" | "skipped"
    detail: str = ""


# ─── Judge profiles JSON ────────────────────────────────────────────


def _judge_profiles_path() -> Path:
    return constants.REPO_ROOT / "tools" / "judge" / "judge_profiles.json"


def load_judge_profiles() -> dict:
    path = _judge_profiles_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _judge_volume() -> str:
    return "amor_custom-models-data"


def _judge_gguf_present(gguf_filename: str) -> bool:
    """`docker run --rm busybox` to check if the GGUF is in the volume."""

    # MSYS_NO_PATHCONV=1 is Git-Bash-on-Windows specific; harmless on
    # other shells.  We pass it as an env override only on this call.
    env = {"MSYS_NO_PATHCONV": "1"} if util.detect_os() == "windows" else None
    res = util.run(
        [
            "docker", "run", "--rm",
            "-v", f"{_judge_volume()}:/v:ro",
            "busybox",
            "test", "-f", f"/v/judge/{gguf_filename}",
        ],
        env=env,
        timeout=30,
    )
    return res.ok


def _judge_download(profile_name: str, profile: dict) -> ModelResult:
    """Download a judge GGUF via `huggingface_hub.snapshot_download`.

    Runs inside the amor-app-1 container so we don't rely on host-side
    HF tooling.  If the container isn't up, falls back to a host pip
    invocation if `huggingface_hub` is importable.
    """

    gguf = profile["gguf_filename"]
    repo = profile["huggingface_repo"]
    pattern = profile["huggingface_pattern"]
    label = profile.get("label", profile_name)

    if _judge_gguf_present(gguf):
        return ModelResult(
            name=label,
            action="present",
            detail=f"{gguf} already in volume {_judge_volume()}",
        )

    util.info(f"Downloading {label} (~{profile.get('approx_disk_gb', '?')} GiB)...")

    # Try the app container first (huggingface_hub is in requirements).
    pyscript = (
        "from huggingface_hub import snapshot_download;"
        "import os, glob;"
        "os.makedirs('/data/custom_models/judge', exist_ok=True);"
        f"snapshot_download(repo_id='{repo}', "
        f"allow_patterns=['{pattern}'], "
        "local_dir='/data/custom_models/judge', "
        "local_dir_use_symlinks=False);"
        "print('done')"
    )
    res = util.run(
        ["docker", "exec", "amor-app-1", "python3", "-c", pyscript],
        timeout=3600,
        stream=True,
    )
    if res.ok and _judge_gguf_present(gguf):
        return ModelResult(name=label, action="pulled", detail=gguf)

    return ModelResult(
        name=label,
        action="failed",
        detail=(
            f"exit {res.code} — check that amor-app-1 is running and has "
            "network access.  Falling back to host download (requires "
            "`pip install huggingface_hub` locally)."
        ),
    )


def pull_judge(profile_name: str) -> ModelResult:
    """Public entry — pull a judge profile by name."""

    profiles = load_judge_profiles()
    if not profiles:
        return ModelResult(
            name=profile_name,
            action="failed",
            detail="judge_profiles.json missing or invalid",
        )
    profile = profiles.get("profiles", {}).get(profile_name)
    if profile is None:
        return ModelResult(
            name=profile_name,
            action="failed",
            detail=f"unknown profile (known: {list(profiles['profiles'])})",
        )
    return _judge_download(profile_name, profile)


# ─── Ollama tag bootstrap ───────────────────────────────────────────


def list_ollama_tags() -> list[str]:
    """Return tags currently installed in the amor-ollama container."""

    res = util.run(
        ["docker", "exec", "amor-ollama", "ollama", "list"],
        timeout=15,
    )
    if not res.ok:
        return []
    tags: list[str] = []
    for line in res.stdout.splitlines()[1:]:  # skip header
        line = line.strip()
        if not line:
            continue
        # Columns: NAME SIZE MODIFIED — first token is name.
        tags.append(line.split()[0])
    return tags


def pull_ollama_tag(tag: str) -> ModelResult:
    if tag in list_ollama_tags():
        return ModelResult(name=tag, action="present", detail="already installed")
    util.info(f"Pulling Ollama tag {tag} (5–10 min on first run)...")
    res = util.run(
        ["docker", "exec", "amor-ollama", "ollama", "pull", tag],
        stream=True,
        timeout=1800,
    )
    if res.ok:
        return ModelResult(name=tag, action="pulled")
    return ModelResult(
        name=tag,
        action="failed",
        detail=f"exit {res.code}; check amor-ollama logs",
    )


# ─── High-level: apply a profile ────────────────────────────────────


def apply_profile(profile: constants.Profile) -> list[ModelResult]:
    results: list[ModelResult] = []
    if profile.pull_judge_mistral:
        results.append(pull_judge("mistral"))
    if profile.pull_judge_phi4:
        results.append(pull_judge("phi4"))
    if profile.pull_ollama_default:
        results.append(pull_ollama_tag("qwen2.5-coder:7b"))
    # llama-swap model bootstrap (Sprint 1 v18) — placeholder; the
    # tools/pull_models.py script is the production path.  We surface
    # a hint here rather than running 30+ GB downloads inline.
    if profile.pull_llamaswap_models:
        results.append(
            ModelResult(
                name="llama-swap models",
                action="skipped",
                detail=(
                    "Run `python tools/pull_models.py` after install to "
                    "fetch DeepSeek-R1-0528-Qwen3-8B + Qwen2.5-Coder-7B "
                    "+ Qwen3-8B (~25 GB)."
                ),
            )
        )
    return results
