"""Tests for hardware catalog, Gen4 Hyperdisk rules, and SkyPilot-compatible Task YAML parsing."""

from __future__ import annotations

import pathlib
import pytest

from gcolab.catalog import normalize_gpu_alias, resolve_hardware_spec
from gcolab.task_spec import load_task_yaml


def test_gpu_alias_resolution_and_unknown_rejection() -> None:
    assert normalize_gpu_alias("l4") == "L4"
    assert normalize_gpu_alias("nvidia-tesla-t4") == "T4"
    assert normalize_gpu_alias("a100_80gb") == "A100-80GB"
    assert normalize_gpu_alias("h100-80gb") == "H100"

    with pytest.raises(ValueError, match="Unsupported --gpu"):
        normalize_gpu_alias("RTX9999")


def test_hardware_spec_l4_and_cpu_defaults() -> None:
    l4 = resolve_hardware_spec(gpu="L4", gpu_count=1, spot=True)
    assert l4["machine_type"] == "g2-standard-8"
    assert l4["gcp_accelerator"] == "nvidia-l4"
    assert l4["disk_type"] == "pd-balanced"
    assert l4["spot"] is True

    cpu = resolve_hardware_spec(gpu=None, cpus=8, spot=False)
    assert cpu["machine_type"] == "e2-standard-8"
    assert cpu["gcp_accelerator"] is None
    assert cpu["gpu_count"] == 0


def test_gen4_hyperdisk_and_spot_guardrails() -> None:
    gen4 = resolve_hardware_spec(machine_type="c4-standard-8", spot=True)
    assert gen4["disk_type"] == "hyperdisk-balanced"

    with pytest.raises(ValueError, match="requires Hyperdisk"):
        resolve_hardware_spec(machine_type="c4-standard-8", disk_type="pd-balanced", spot=True)

    with pytest.raises(ValueError, match="does not support GCP Spot"):
        resolve_hardware_spec(machine_type="c3-standard-192-metal", spot=True)


def test_task_yaml_parsing(tmp_path: pathlib.Path) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "a.yaml").write_text("x: 1\n", encoding="utf-8")
    yaml_file = tmp_path / "task.yaml"
    yaml_file.write_text(
        """
name: demo-train
workdir: .
resources:
  accelerators: L4:2
  use_spot: true
  disk_size: 150
  infra: gcp/asia-east1/asia-east1-a
  max_hours: 3.5
envs:
  LR: 0.001
file_mounts:
  /workspace/cfg.yaml: ./configs/a.yaml
outputs:
  /workspace/out: ./out
setup: |
  pip install tqdm
run: |
  python3 train.py
""",
        encoding="utf-8",
    )

    spec = load_task_yaml(yaml_file)
    assert spec.name == "demo-train"
    assert spec.gpu == "L4"
    assert spec.gpu_count == 2
    assert spec.spot is True
    assert spec.disk_gb == 150
    assert spec.region == "asia-east1"
    assert spec.zone == "asia-east1-a"
    assert spec.max_hours == 3.5
    assert spec.envs == {"LR": "0.001"}
    assert spec.file_mounts["/workspace/cfg.yaml"].endswith("a.yaml")
    assert spec.outputs[0][0] == "/workspace/out"
    assert "python3 train.py" in (spec.run or "")
