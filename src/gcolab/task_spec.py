"""SkyPilot-compatible Task YAML parser for GCP workloads."""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import yaml


@dataclasses.dataclass
class TaskSpec:
    name: str | None = None
    workdir: str | None = None
    gpu: str | None = None
    gpu_count: int = 1
    cpus: int | None = None
    machine_type: str | None = None
    spot: bool = True
    disk_gb: int | None = None
    disk_type: str | None = None
    project: str | None = None
    region: str | None = None
    zone: str | None = None
    idle_minutes: int = 30
    autodown: bool = True
    max_hours: float | None = None
    envs: dict[str, str] = dataclasses.field(default_factory=dict)
    file_mounts: dict[str, str] = dataclasses.field(default_factory=dict)
    outputs: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    optional_outputs: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    retain_on_download_error_mins: int = 30
    setup: str | None = None
    run: str | None = None


def _parse_accelerators_field(raw: Any) -> tuple[str | None, int]:
    """Parse SkyPilot 'accelerators: L4:1' or {'L4': 2} or ['L4:1']."""
    if raw is None:
        return None, 1
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict) and raw:
        k, v = next(iter(raw.items()))
        return str(k), int(v)
    text = str(raw).strip()
    if ":" in text:
        acc, cnt = text.rsplit(":", 1)
        return acc.strip(), int(cnt.strip())
    return text, 1


def _parse_infra_field(infra: str | None) -> tuple[str | None, str | None]:
    """Parse SkyPilot 'infra: gcp/us-central1/us-central1-a' or 'us-central1'."""
    if not infra:
        return None, None
    parts = [p.strip() for p in str(infra).split("/") if p.strip()]
    if parts and parts[0].lower() == "gcp":
        parts = parts[1:]
    elif parts and parts[0].lower() not in ("gcp",) and len(parts) >= 2 and "-" not in parts[0]:
        raise ValueError(
            f"gcolab targets Google Cloud only; unsupported infra provider in {infra!r}."
        )
    if not parts:
        return None, None
    if len(parts) == 1:
        token = parts[0]
        # Zone looks like us-central1-a (2+ hyphens with trailing letter), region like us-central1
        if token.count("-") >= 2 and len(token.rsplit("-", 1)[-1]) == 1:
            return token.rsplit("-", 1)[0], token
        return token, None
    return parts[0], parts[1]


def load_task_yaml(path: str | pathlib.Path) -> TaskSpec:
    yaml_path = pathlib.Path(path).expanduser().resolve()
    if not yaml_path.exists():
        raise FileNotFoundError(f"Task YAML not found: {yaml_path}")

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Task YAML root must be a mapping: {yaml_path}")

    res = data.get("resources") or {}
    if not isinstance(res, dict):
        raise ValueError("'resources' in Task YAML must be a mapping.")

    acc_raw = res.get("accelerators") or res.get("gpu")
    if acc_raw is None and isinstance(res.get("any_of"), list) and res["any_of"]:
        first_choice = res["any_of"][0]
        if isinstance(first_choice, dict):
            acc_raw = first_choice.get("accelerators") or first_choice.get("gpu")

    gpu, gpu_count = _parse_accelerators_field(acc_raw)
    if "gpu_count" in res and res["gpu_count"] is not None:
        gpu_count = int(res["gpu_count"])

    infra_region, infra_zone = _parse_infra_field(res.get("infra"))
    region = res.get("region") or infra_region
    zone = res.get("zone") or infra_zone

    cpus_raw = res.get("cpus")
    cpus = None
    if cpus_raw is not None:
        cpus_str = str(cpus_raw).rstrip("+").strip()
        if cpus_str.isdigit():
            cpus = int(cpus_str)

    spot = bool(res.get("use_spot", True))
    disk_gb = int(res["disk_size"]) if res.get("disk_size") is not None else None

    envs_raw = data.get("envs") or {}
    envs = {str(k): str(v) for k, v in envs_raw.items()}

    fm_raw = data.get("file_mounts") or {}
    file_mounts: dict[str, str] = {}
    for remote_dst, local_src in fm_raw.items():
        if isinstance(local_src, str):
            src_p = pathlib.Path(local_src).expanduser()
            if not src_p.is_absolute():
                src_p = (yaml_path.parent / src_p).resolve()
            file_mounts[str(remote_dst)] = str(src_p)

    outputs: list[tuple[str, str]] = []
    out_raw = data.get("outputs") or {}
    if isinstance(out_raw, dict):
        for remote_src, local_dst in out_raw.items():
            dst_p = pathlib.Path(str(local_dst)).expanduser()
            if not dst_p.is_absolute():
                dst_p = (yaml_path.parent / dst_p).resolve()
            outputs.append((str(remote_src), str(dst_p)))

    optional_outputs: list[tuple[str, str]] = []
    opt_out_raw = data.get("optional_outputs") or {}
    if isinstance(opt_out_raw, dict):
        for remote_src, local_dst in opt_out_raw.items():
            dst_p = pathlib.Path(str(local_dst)).expanduser()
            if not dst_p.is_absolute():
                dst_p = (yaml_path.parent / dst_p).resolve()
            optional_outputs.append((str(remote_src), str(dst_p)))

    retain_mins = int(
        res.get(
            "retain_on_download_error_mins",
            data.get("retain_on_download_error_mins", 30),
        )
    )

    workdir_val = data.get("workdir")
    resolved_workdir = None
    if workdir_val is not None:
        wp = pathlib.Path(str(workdir_val)).expanduser()
        if not wp.is_absolute():
            wp = (yaml_path.parent / wp).resolve()
        resolved_workdir = str(wp)

    return TaskSpec(
        name=data.get("name"),
        workdir=resolved_workdir,
        gpu=gpu,
        gpu_count=gpu_count,
        cpus=cpus,
        machine_type=res.get("instance_type") or res.get("machine_type"),
        spot=spot,
        disk_gb=disk_gb,
        disk_type=res.get("disk_tier") or res.get("disk_type"),
        project=res.get("project") or data.get("project"),
        region=region,
        zone=zone,
        idle_minutes=int(res.get("idle_minutes", data.get("idle_minutes", 30))),
        autodown=bool(res.get("autodown", data.get("autodown", True))),
        max_hours=float(res["max_hours"]) if res.get("max_hours") is not None else None,
        envs=envs,
        file_mounts=file_mounts,
        outputs=outputs,
        optional_outputs=optional_outputs,
        retain_on_download_error_mins=retain_mins,
        setup=data.get("setup"),
        run=data.get("run"),
    )
