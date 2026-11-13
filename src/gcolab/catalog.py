"""GCP GPU/CPU SKU catalog, disk tier rules, candidate regions, and pricing estimates.

Modeled on SkyPilot's sky/clouds/gcp.py and Google Cloud's compute-spot-capacity-advice skill.
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class AcceleratorSpec:
    alias: str
    gcp_accelerator: str
    vram_gb: int
    machine_by_count: dict[int, str]
    approx_ondemand_usd_hr: float
    approx_spot_usd_hr: float
    preferred_regions: tuple[str, ...]
    notes: str = ""


ACCELERATOR_CATALOG: dict[str, AcceleratorSpec] = {
    "T4": AcceleratorSpec(
        alias="T4",
        gcp_accelerator="nvidia-tesla-t4",
        vram_gb=16,
        machine_by_count={
            1: "n1-standard-4",
            2: "n1-standard-8",
            4: "n1-standard-16",
        },
        approx_ondemand_usd_hr=0.54,
        approx_spot_usd_hr=0.16,
        preferred_regions=(
            "us-central1",
            "us-east4",
            "us-west1",
            "us-east1",
            "asia-east1",
            "asia-southeast1",
            "europe-west4",
        ),
        notes="Entry 16GB Turing GPU; wide regional availability and lowest hourly cost.",
    ),
    "L4": AcceleratorSpec(
        alias="L4",
        gcp_accelerator="nvidia-l4",
        vram_gb=24,
        machine_by_count={
            1: "g2-standard-8",
            2: "g2-standard-24",
            4: "g2-standard-48",
            8: "g2-standard-96",
        },
        approx_ondemand_usd_hr=0.98,
        approx_spot_usd_hr=0.31,
        preferred_regions=(
            "us-central1",
            "us-east4",
            "us-west1",
            "us-west4",
            "asia-east1",
            "asia-northeast1",
            "asia-southeast1",
            "europe-west4",
        ),
        notes="24GB Ada Lovelace GPU (Colab L4 equivalent); defaults to g2-standard-8 (32GB RAM).",
    ),
    "V100": AcceleratorSpec(
        alias="V100",
        gcp_accelerator="nvidia-tesla-v100",
        vram_gb=16,
        machine_by_count={
            1: "n1-standard-8",
            2: "n1-standard-16",
            4: "n1-standard-32",
            8: "n1-standard-64",
        },
        approx_ondemand_usd_hr=2.86,
        approx_spot_usd_hr=0.86,
        preferred_regions=("us-central1", "us-west1", "asia-east1", "europe-west4"),
        notes="16GB Volta GPU; legacy CUDA 12 image recommended if using older stacks.",
    ),
    "A100": AcceleratorSpec(
        alias="A100",
        gcp_accelerator="nvidia-tesla-a100",
        vram_gb=40,
        machine_by_count={
            1: "a2-highgpu-1g",
            2: "a2-highgpu-2g",
            4: "a2-highgpu-4g",
            8: "a2-highgpu-8g",
        },
        approx_ondemand_usd_hr=3.67,
        approx_spot_usd_hr=1.10,
        preferred_regions=(
            "us-central1",
            "us-east1",
            "us-west1",
            "asia-northeast1",
            "asia-southeast1",
            "europe-west4",
        ),
        notes="40GB Ampere A100 on a2-highgpu series (85GB system RAM per GPU).",
    ),
    "A100-80GB": AcceleratorSpec(
        alias="A100-80GB",
        gcp_accelerator="nvidia-a100-80gb",
        vram_gb=80,
        machine_by_count={
            1: "a2-ultragpu-1g",
            2: "a2-ultragpu-2g",
            4: "a2-ultragpu-4g",
            8: "a2-ultragpu-8g",
        },
        approx_ondemand_usd_hr=5.07,
        approx_spot_usd_hr=1.52,
        preferred_regions=("us-central1", "us-east4", "us-west4", "europe-west4"),
        notes="80GB Ampere A100 on a2-ultragpu series (170GB system RAM per GPU).",
    ),
    "H100": AcceleratorSpec(
        alias="H100",
        gcp_accelerator="nvidia-h100-80gb",
        vram_gb=80,
        machine_by_count={
            1: "a3-highgpu-1g",
            2: "a3-highgpu-2g",
            4: "a3-highgpu-4g",
            8: "a3-highgpu-8g",
        },
        approx_ondemand_usd_hr=11.06,
        approx_spot_usd_hr=3.32,
        preferred_regions=("us-central1", "us-east4", "us-west1", "europe-west4", "asia-southeast1"),
        notes="80GB Hopper H100 on a3-highgpu series.",
    ),
}

DEFAULT_CPU_REGIONS: tuple[str, ...] = (
    "us-central1",
    "us-east4",
    "us-west1",
    "asia-east1",
    "asia-northeast1",
    "europe-west4",
)

# Gen 4+ machine prefixes that reject pd-* boot disks and require Hyperdisk
HYPERDISK_REQUIRED_PREFIXES: tuple[str, ...] = (
    "c4-",
    "c4a-",
    "c4d-",
    "n4-",
    "n4a-",
    "m4-",
    "h4d-",
    "a4-",
    "a4x-",
    "a3-ultragpu-",
)

# Shapes that GCP does not permit under SPOT provisioningModel
SPOT_UNSUPPORTED_PREFIXES: tuple[str, ...] = (
    "a4x-",
)

# Official Google Cloud image families (probed in order)
GPU_IMAGE_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("deeplearning-platform-release", "common-cu128-ubuntu-2204-nvidia-570"),
    ("deeplearning-platform-release", "common-cu124-ubuntu-2204"),
    ("deeplearning-platform-release", "pytorch-latest-gpu"),
    ("deeplearning-platform-release", "common-cu123-debian-11"),
    ("ubuntu-os-cloud", "ubuntu-2204-lts"),
)

CPU_IMAGE_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("ubuntu-os-cloud", "ubuntu-2404-lts-amd64"),
    ("ubuntu-os-cloud", "ubuntu-2204-lts"),
    ("debian-cloud", "debian-12"),
)


def normalize_gpu_alias(gpu: str | None) -> str | None:
    if gpu is None:
        return None
    cleaned = gpu.strip().upper().replace("_", "-")
    aliases = {
        "A100-40GB": "A100",
        "TESLA-T4": "T4",
        "NVIDIA-L4": "L4",
        "NVIDIA-TESLA-T4": "T4",
        "NVIDIA-TESLA-A100": "A100",
        "NVIDIA-A100-80GB": "A100-80GB",
        "NVIDIA-H100-80GB": "H100",
        "H100-80GB": "H100",
    }
    cleaned = aliases.get(cleaned, cleaned)
    if cleaned not in ACCELERATOR_CATALOG:
        supported = ", ".join(ACCELERATOR_CATALOG)
        raise ValueError(
            f"Unsupported --gpu {gpu!r}. Supported values: {supported} "
            "(unlike consumer Colab CLI, unknown GPUs never silently fall back to A100)."
        )
    return cleaned


def default_disk_type_for_machine(machine_type: str) -> str:
    mt = machine_type.strip().lower()
    if any(mt.startswith(prefix) for prefix in HYPERDISK_REQUIRED_PREFIXES):
        return "hyperdisk-balanced"
    return "pd-balanced"


def validate_spot_compatibility(machine_type: str, spot: bool) -> None:
    if not spot:
        return
    mt = machine_type.strip().lower()
    if mt.endswith("-metal") or any(mt.startswith(p) for p in SPOT_UNSUPPORTED_PREFIXES):
        raise ValueError(
            f"Machine type {machine_type!r} does not support GCP Spot provisioning. "
            "Pass --no-spot or select another machine family."
        )


def resolve_hardware_spec(
    gpu: str | None = None,
    gpu_count: int = 1,
    cpus: int | None = None,
    machine_type: str | None = None,
    disk_gb: int | None = None,
    disk_type: str | None = None,
    spot: bool = True,
) -> dict[str, Any]:
    gpu_key = normalize_gpu_alias(gpu)
    if gpu_key is not None:
        spec = ACCELERATOR_CATALOG[gpu_key]
        if gpu_count not in spec.machine_by_count and machine_type is None:
            valid_counts = ", ".join(str(c) for c in sorted(spec.machine_by_count))
            raise ValueError(
                f"GPU {gpu_key} supports counts [{valid_counts}] by default; got {gpu_count}. "
                "Specify --machine-type explicitly if using a custom shape."
            )
        chosen_machine = machine_type or spec.machine_by_count[gpu_count]
        gcp_acc: str | None = spec.gcp_accelerator
        eff_gpu_count = gpu_count
        regions = list(spec.preferred_regions)
        default_gb = 100
        est_cost = (
            spec.approx_spot_usd_hr if spot else spec.approx_ondemand_usd_hr
        ) * gpu_count
    else:
        gcp_acc = None
        eff_gpu_count = 0
        if machine_type:
            chosen_machine = machine_type
        else:
            cpu_n = cpus or 4
            if cpu_n not in (2, 4, 8, 16, 32):
                raise ValueError(
                    f"Default e2-standard CPU count must be one of 2, 4, 8, 16, 32 (got {cpu_n}), "
                    "or pass --machine-type explicitly."
                )
            chosen_machine = f"e2-standard-{cpu_n}"
        regions = list(DEFAULT_CPU_REGIONS)
        default_gb = 60
        cpu_factor = int(chosen_machine.rsplit("-", 1)[-1]) if chosen_machine.rsplit("-", 1)[-1].isdigit() else 4
        base_hr = 0.0335 * cpu_factor
        est_cost = round(base_hr * (0.33 if spot else 1.0), 3)

    validate_spot_compatibility(chosen_machine, spot)
    chosen_disk_type = disk_type or default_disk_type_for_machine(chosen_machine)
    # Enforce Hyperdisk if user didn't realize Gen4 requires it
    if default_disk_type_for_machine(chosen_machine) == "hyperdisk-balanced" and chosen_disk_type.startswith("pd-"):
        raise ValueError(
            f"Gen 4+ machine type {chosen_machine!r} requires Hyperdisk (e.g. hyperdisk-balanced), "
            f"not {chosen_disk_type!r}."
        )

    return {
        "gpu": gpu_key,
        "gcp_accelerator": gcp_acc,
        "gpu_count": eff_gpu_count,
        "machine_type": chosen_machine,
        "disk_gb": disk_gb or default_gb,
        "disk_type": chosen_disk_type,
        "spot": spot,
        "candidate_regions": regions,
        "estimated_hourly_usd": round(est_cost, 3),
    }


DISK_GB_MONTH_RATES_USD: dict[str, float] = {
    "pd-standard": 0.04,
    "pd-balanced": 0.10,
    "pd-ssd": 0.17,
    "pd-extreme": 0.125,
    "hyperdisk-balanced": 0.08,
}

SMALL_CPU_STANDARD_USD_HR: dict[str, float] = {
    "e2-micro": 0.0084,
    "e2-small": 0.0168,
    "e2-medium": 0.0335,
    "f1-micro": 0.0076,
    "g1-small": 0.0257,
    "n1-standard-1": 0.0475,
}


def estimate_session_cost(
    *,
    machine_type: str,
    gpu: str | None = None,
    gpu_count: int = 0,
    spot: bool = True,
    disk_gb: int = 60,
    disk_type: str = "pd-balanced",
    runtime_seconds: float = 0.0,
) -> dict[str, Any]:
    """Compute a reference runtime + boot-disk cost estimate (with GCE 60s billing minimum)."""
    mt = (machine_type or "e2-standard-4").strip().lower()
    gpu_key = normalize_gpu_alias(gpu) if gpu else None
    if gpu_key is not None and gpu_count > 0:
        spec = ACCELERATOR_CATALOG[gpu_key]
        compute_hr = (spec.approx_spot_usd_hr if spot else spec.approx_ondemand_usd_hr) * gpu_count
    elif mt in SMALL_CPU_STANDARD_USD_HR:
        base_hr = SMALL_CPU_STANDARD_USD_HR[mt]
        compute_hr = base_hr * (0.30 if spot else 1.0)
    else:
        tail = mt.rsplit("-", 1)[-1]
        vcpus = int(tail) if tail.isdigit() else 4
        per_vcpu = 0.0475 if mt.startswith("n1-") else (0.0485 if mt.startswith(("n2-", "c2-", "c3-", "c4-", "n4-")) else 0.0335)
        compute_hr = per_vcpu * vcpus * (0.33 if spot else 1.0)

    gb_month = DISK_GB_MONTH_RATES_USD.get(disk_type.strip().lower(), 0.10)
    disk_hr = (max(int(disk_gb), 1) * gb_month) / 730.0

    raw_sec = max(0.0, float(runtime_seconds))
    billed_sec = max(60.0, raw_sec) if raw_sec > 0 else 0.0
    hours = billed_sec / 3600.0
    compute_usd = compute_hr * hours
    disk_usd = disk_hr * hours
    return {
        "runtime_seconds": round(raw_sec, 1),
        "billed_seconds": round(billed_sec, 1),
        "compute_hourly_usd": round(compute_hr, 4),
        "disk_hourly_usd": round(disk_hr, 4),
        "estimated_total_usd": round(compute_usd + disk_usd, 4),
        "basis": "reference_catalog_estimate",
    }


def catalog_summary_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, spec in ACCELERATOR_CATALOG.items():
        rows.append(
            {
                "gpu": key,
                "vram_gb": spec.vram_gb,
                "default_machine": spec.machine_by_count[1],
                "supported_counts": sorted(spec.machine_by_count.keys()),
                "gcp_accelerator": spec.gcp_accelerator,
                "approx_spot_usd_hr": spec.approx_spot_usd_hr,
                "approx_ondemand_usd_hr": spec.approx_ondemand_usd_hr,
                "preferred_regions": list(spec.preferred_regions),
                "notes": spec.notes,
            }
        )
    return rows
