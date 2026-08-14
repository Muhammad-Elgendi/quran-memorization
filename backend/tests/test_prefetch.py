from pathlib import Path

from prefetch_model import cached_snapshot_path, hf_home, hub_cache, repo_cache_dir


def test_cached_snapshot_path_finds_complete_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    snap = repo_cache_dir("UsefulSensors/moonshine-tiny-ar") / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    (snap / "model.safetensors").write_bytes(b"weights")

    assert cached_snapshot_path("UsefulSensors/moonshine-tiny-ar") == snap


def test_cached_snapshot_path_ignores_incomplete_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    snap = repo_cache_dir("UsefulSensors/moonshine-tiny-ar") / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")

    assert cached_snapshot_path("UsefulSensors/moonshine-tiny-ar") is None


def test_hub_cache_respects_hf_hub_cache_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "custom-hub"))
    assert hub_cache() == Path(tmp_path / "custom-hub")


def test_hf_home_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "env-home"))
    assert hf_home() == tmp_path / "env-home"
