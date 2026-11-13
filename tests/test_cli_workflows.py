"""CLI end-to-end tests with mocked GCP & SSH layers."""

from __future__ import annotations

import json
import pathlib
import pytest

from gcolab.cli import main
from gcolab.config import SessionRecord


def test_cli_gpus_and_config_isolation(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_file = tmp_path / "isolated" / "sessions.json"
    rc = main(["--config", str(cfg_file), "-o", "json", "config", "set", "project", "demo-gcp-123"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["default_project"] == "demo-gcp-123"
    assert (cfg_file.parent / "settings.json").exists()

    rc_show = main(["config", "show", "--config", str(cfg_file), "-o", "json"])
    assert rc_show == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["default_project"] == "demo-gcp-123"

    rc2 = main(["--config", str(cfg_file), "-o", "json", "gpus"])
    assert rc2 == 0
    gpus = json.loads(capsys.readouterr().out)["accelerators"]
    aliases = [g["gpu"] for g in gpus]
    assert "L4" in aliases and "T4" in aliases and "A100" in aliases and "H100" in aliases


def test_cli_run_aborts_before_provisioning_when_file_missing(tmp_path: pathlib.Path) -> None:
    cfg_file = tmp_path / "sessions.json"
    rc = main(["--config", str(cfg_file), "run", "--gpu", "L4", str(tmp_path / "missing.py")])
    assert rc == 2
    assert not cfg_file.exists()


def test_cli_run_destroys_vm_even_when_script_exits_nonzero(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "sessions.json"
    script = tmp_path / "boom.py"
    script.write_text("import sys; sys.exit(42)\n", encoding="utf-8")

    events: list[str] = []

    def fake_provision(store, **kwargs):
        events.append("provision")
        rec = SessionRecord(
            name="run-test",
            instance_name="gcolab-run-test-abcd",
            project="demo-proj",
            region="us-central1",
            zone="us-central1-a",
            machine_type="g2-standard-8",
            gpu="L4",
            gpu_count=1,
            spot=True,
            external_ip="203.0.113.10",
            status="RUNNING",
        )
        store.upsert_session(rec)
        return rec

    monkeypatch.setattr("gcolab.cli._provision_session", fake_provision)
    monkeypatch.setattr(
        "gcolab.cli.upload_path",
        lambda store, rec, local_path, remote_path="/workspace": {
            "files_synced": 1,
            "bytes_sent": 20,
            "remote_path": "/workspace/boom.py",
        },
    )
    monkeypatch.setattr("gcolab.cli.ssh_run_stream", lambda store, rec, cmd, stdin_bytes=None: 42)

    class FakeComputeClient:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def delete_instance(self, zone: str, instance_name: str) -> bool:
            events.append(f"deleted:{zone}/{instance_name}")
            return True

    monkeypatch.setattr("gcolab.cli.ComputeClient", FakeComputeClient)

    rc = main(["--config", str(cfg_file), "run", "--gpu", "L4", str(script)])
    assert rc == 42
    assert events == ["provision", "deleted:us-central1-a/gcolab-run-test-abcd"]
