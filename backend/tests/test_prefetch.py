from pathlib import Path

import pytest

from prefetch_model import (
    DEFAULT_MODEL,
    cached_snapshot_path,
    hf_home,
    hub_cache,
    repo_cache_dir,
)

TARTEEL_REPO = "tarteel-ai/whisper-tiny-ar-quran"
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cached_snapshot_path_finds_complete_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    snap = repo_cache_dir(TARTEEL_REPO) / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    (snap / "model.safetensors").write_bytes(b"weights")

    assert cached_snapshot_path(TARTEEL_REPO) == snap


def test_cached_snapshot_path_ignores_incomplete_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    snap = repo_cache_dir(TARTEEL_REPO) / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")

    assert cached_snapshot_path(TARTEEL_REPO) is None


def test_hub_cache_respects_hf_hub_cache_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "custom-hub"))
    assert hub_cache() == Path(tmp_path / "custom-hub")


def test_hf_home_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "env-home"))
    assert hf_home() == tmp_path / "env-home"


def test_default_model_is_tarteel():
    assert DEFAULT_MODEL == TARTEEL_REPO


@pytest.mark.parametrize(
    "rel",
    [
        ".env.example",
        "install.py",
        "docker-compose.yml",
        "docker-compose.dev.yml",
        "k8s/deploy.yaml",
        "backend/prefetch_model.py",
        "backend/app/config.py",
    ],
)
def test_tp4_runtime_files_have_no_moonshine_assignment(rel):
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert "UsefulSensors/moonshine" not in text
    assert "MOONSHINE_MODEL" not in text
