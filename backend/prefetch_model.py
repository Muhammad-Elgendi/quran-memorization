"""Download Moonshine weights into HF_HOME (Compose volume / K8s PVC).

Hugging Face Hub stores snapshots at ``$HF_HOME/hub/models--...``.
Do not set TRANSFORMERS_CACHE to HF_HOME — that makes ``from_pretrained``
look beside ``hub/`` and re-download on first assess.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_MODEL = "UsefulSensors/moonshine-tiny-ar"
CONTAINER_HF_HOME = "/models/huggingface"
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".ckpt")


def hf_home() -> Path:
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"])
    container = Path(CONTAINER_HF_HOME)
    if container.parent.is_dir():
        return container
    return Path.home() / ".cache" / "huggingface"


def hub_cache() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit)
    return hf_home() / "hub"


def repo_cache_dir(repo_id: str) -> Path:
    return hub_cache() / ("models--" + repo_id.replace("/", "--"))


def cached_snapshot_path(repo_id: str) -> Path | None:
    snapshots = repo_cache_dir(repo_id) / "snapshots"
    if not snapshots.is_dir():
        return None
    for snap in sorted(snapshots.iterdir()):
        if not snap.is_dir():
            continue
        if not (snap / "config.json").is_file():
            continue
        if any(
            path.is_file() and path.name.endswith(_WEIGHT_SUFFIXES)
            for path in snap.rglob("*")
        ):
            return snap
    return None


def prefetch_model(repo_id: str | None = None) -> str:
    repo_id = repo_id or os.environ.get("MOONSHINE_MODEL") or DEFAULT_MODEL
    home = hf_home()
    home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(home))
    os.environ.setdefault("HF_HUB_CACHE", str(hub_cache()))

    cached = cached_snapshot_path(repo_id)
    if cached is not None:
        print(f"Using cached {repo_id} at {cached}")
        return str(cached)

    print(f"Downloading {repo_id} into {home} ...")
    from huggingface_hub import snapshot_download

    path = snapshot_download(repo_id=repo_id)
    print(f"Model ready at {path}")
    return path


def main() -> int:
    try:
        prefetch_model()
    except Exception as exc:  # noqa: BLE001 — CLI; surface any hub/network error
        print(f"Model prefetch failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
