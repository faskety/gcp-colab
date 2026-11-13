"""Tests for ComputeClient safety isolation, Spot scheduling schema, capacity ranking, and failover."""

from __future__ import annotations

from typing import Any
import pytest

from gcolab import MANAGED_LABEL_KEY, MANAGED_LABEL_VALUE
from gcolab.gcp_api import (
    ComputeClient,
    GCPApiError,
    SafetyIsolationError,
    StockoutError,
    build_scheduling_config,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "{}" if payload is not None else ""

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, headers=None, params=None, json=None, timeout=45):
        self.calls.append((method, url, json))
        return self.handler(method, url, params, json)


def test_spot_scheduling_never_mixes_legacy_preemptible() -> None:
    sched = build_scheduling_config(spot=True, has_gpu=True, spot_action="DELETE", max_run_seconds=7200)
    assert sched["provisioningModel"] == "SPOT"
    assert "preemptible" not in sched
    assert sched["onHostMaintenance"] == "TERMINATE"
    assert sched["automaticRestart"] is False
    assert sched["instanceTerminationAction"] == "DELETE"
    assert sched["maxRunDuration"] == {"seconds": "7200"}

    gpu_std = build_scheduling_config(spot=False, has_gpu=True)
    assert gpu_std["provisioningModel"] == "STANDARD"
    assert gpu_std["onHostMaintenance"] == "TERMINATE"


def test_refuses_to_delete_or_stop_unmanaged_existing_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gcolab.gcp_api.get_access_token_and_adc_project", lambda: ("fake-token", "proj-1", "adc"))

    # Simulate an existing production VM that does NOT have managed-by=gcolab
    def handler(method: str, url: str, params, body):
        if method == "GET" and url.endswith("/instances/prod-web-01"):
            return FakeResponse(200, {"name": "prod-web-01", "status": "RUNNING", "labels": {"env": "prod"}})
        pytest.fail(f"Unexpected mutation request executed: {method} {url}")

    session = FakeSession(handler)
    client = ComputeClient("proj-1", session=session)  # type: ignore[arg-type]

    with pytest.raises(SafetyIsolationError, match="SAFETY BLOCK"):
        client.delete_instance("us-central1-a", "prod-web-01")

    with pytest.raises(SafetyIsolationError, match="SAFETY BLOCK"):
        client.stop_instance("us-central1-a", "prod-web-01")


def test_allows_deleting_managed_gcolab_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gcolab.gcp_api.get_access_token_and_adc_project", lambda: ("fake-token", "proj-1", "adc"))

    def handler(method: str, url: str, params, body):
        if method == "GET" and url.endswith("/instances/gcolab-exp1-abcd"):
            return FakeResponse(
                200,
                {
                    "name": "gcolab-exp1-abcd",
                    "status": "RUNNING",
                    "labels": {MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE},
                },
            )
        if method == "DELETE" and url.endswith("/instances/gcolab-exp1-abcd"):
            return FakeResponse(200, {"name": "op-del-1", "status": "DONE"})
        if method == "GET" and url.endswith("/operations/op-del-1"):
            return FakeResponse(200, {"name": "op-del-1", "status": "DONE"})
        raise AssertionError(f"Unexpected call {method} {url}")

    client = ComputeClient("proj-1", session=FakeSession(handler))  # type: ignore[arg-type]
    assert client.delete_instance("us-central1-a", "gcolab-exp1-abcd") is True


def test_spot_capacity_advice_sorting_and_stockout_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gcolab.gcp_api.get_access_token_and_adc_project", lambda: ("fake-token", "proj-1", "adc"))

    def handler(method: str, url: str, params, body):
        if method == "POST" and url.endswith("/advice/capacity"):
            return FakeResponse(
                200,
                {
                    "recommendations": [
                        {
                            "scores": {"obtainability": 0.4, "estimatedUptime": "600s"},
                            "shards": [{"zone": "zones/us-central1-a", "machineType": "g2-standard-8"}],
                        },
                        {
                            "scores": {"obtainability": 0.9, "estimatedUptime": "3600s"},
                            "shards": [{"zone": "zones/us-central1-c", "machineType": "g2-standard-8"}],
                        },
                    ]
                },
            )
        if method == "POST" and "/zones/us-central1-c/instances" in url:
            return FakeResponse(200, {"name": "op-ins-stockout"})
        if method == "GET" and url.endswith("/operations/op-ins-stockout"):
            return FakeResponse(
                200,
                {
                    "name": "op-ins-stockout",
                    "status": "DONE",
                    "error": {
                        "errors": [
                            {
                                "code": "ZONE_RESOURCE_POOL_EXHAUSTED",
                                "message": "Zone exhausted",
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"Unexpected {method} {url}")

    client = ComputeClient("proj-1", session=FakeSession(handler))  # type: ignore[arg-type]
    ranked = client.query_spot_capacity_advice("us-central1", "g2-standard-8")
    assert [r["zone"] for r in ranked] == ["us-central1-c", "us-central1-a"]

    body = client.build_instance_insert_body(
        instance_name="gcolab-job-1122",
        session_name="job",
        zone="us-central1-c",
        machine_type="g2-standard-8",
        source_image="projects/deeplearning-platform-release/global/images/dlvm",
        disk_gb=100,
        disk_type="pd-balanced",
        gcp_accelerator="nvidia-l4",
        gpu_count=1,
        spot=True,
        spot_action="DELETE",
        max_run_seconds=3600,
        ssh_user="gcolab",
        ssh_pubkey="ssh-ed25519 AAAAC3Nza... gcolab",
        startup_script="#!/bin/bash\necho ok\n",
    )
    assert body["disks"][0]["autoDelete"] is True
    assert body["labels"][MANAGED_LABEL_KEY] == MANAGED_LABEL_VALUE

    with pytest.raises(StockoutError):
        client.create_instance_in_zone("us-central1-c", body)
