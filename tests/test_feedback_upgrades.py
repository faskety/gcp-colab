"""Tests for transfer resilience, deliverable gates, workspace bindings, job tags/wait, and teardown receipts."""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import pathlib
import subprocess
import tarfile
import time
import types
from typing import Any

import pytest

from gcolab import MANAGED_LABEL_KEY, MANAGED_LABEL_VALUE
from gcolab.cli import EXIT_ARTIFACT_DELIVERY_FAILED, EXIT_TEARDOWN_FAILED, main
from gcolab.config import (
    SessionRecord,
    StateStore,
    find_workspace_binding,
    write_workspace_binding,
)
from gcolab.gcp_api import ComputeClient, GCPApiError, build_scheduling_config
from gcolab.remote_agent import REMOTE_JOB_RUNNER_PY
from gcolab.transport import (
    TransferOptions,
    TransportError,
    build_tar_gz_file,
    session_host_key_alias,
    upload_path,
)


def test_e2_standard_deadline_uses_migrate_and_avoids_400() -> None:
    e2_sched = build_scheduling_config(
        spot=False,
        has_gpu=False,
        max_run_seconds=900,
        machine_type="e2-micro",
    )
    assert e2_sched["provisioningModel"] == "STANDARD"
    assert e2_sched["onHostMaintenance"] == "MIGRATE"
    assert "maxRunDuration" not in e2_sched

    n1_sched = build_scheduling_config(
        spot=False,
        has_gpu=False,
        max_run_seconds=900,
        machine_type="n1-standard-1",
    )
    assert n1_sched["onHostMaintenance"] == "TERMINATE"
    assert n1_sched["maxRunDuration"] == {"seconds": "900"}


def test_per_instance_host_key_alias_prevents_ephemeral_ip_collision() -> None:
    r1 = SessionRecord(
        name="exp1",
        instance_name="gcolab-exp1-1111",
        project="proj-a",
        region="us-central1",
        zone="us-central1-a",
        machine_type="e2-standard-4",
        external_ip="203.0.113.42",
    )
    r2 = SessionRecord(
        name="exp2",
        instance_name="gcolab-exp2-2222",
        project="proj-a",
        region="us-central1",
        zone="us-central1-a",
        machine_type="e2-standard-4",
        external_ip="203.0.113.42",
    )
    assert session_host_key_alias(r1, "203.0.113.42", 22) != session_host_key_alias(r2, "203.0.113.42", 22)


def test_workspace_binding_excluded_from_sync_archive(tmp_path: pathlib.Path) -> None:
    proj = tmp_path / "sim_solver"
    proj.mkdir()
    write_workspace_binding(proj, session_name="cloud-build", project_id="demo-proj")
    (proj / "solver.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

    tar_out = tmp_path / "out.tar.gz"
    count, size, sha = build_tar_gz_file(proj, tar_out)
    assert count == 1
    assert size > 0 and len(sha) == 64
    with tarfile.open(str(tar_out), "r:gz") as tf:
        assert tf.getnames() == ["solver.cpp"]


def test_statestore_concurrent_locking_preserves_all_sessions(tmp_path: pathlib.Path) -> None:
    store = StateStore(sessions_file=tmp_path / "sessions.json")

    def _writer(idx: int) -> None:
        rec = SessionRecord(
            name=f"worker-{idx}",
            instance_name=f"gcolab-worker-{idx}-abcd",
            project="proj",
            region="us-central1",
            zone="us-central1-a",
            machine_type="e2-standard-4",
        )
        store.upsert_session(rec)
        store.append_event(rec.name, "heartbeat", {"idx": idx})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_writer, range(20)))

    loaded = store.load_sessions()
    assert len(loaded) == 20
    assert {k for k in loaded} == {f"worker-{i}" for i in range(20)}


def test_single_file_upload_scp_failure_falls_back_to_stream_and_verifies_sha256(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(sessions_file=tmp_path / "state" / "sessions.json")
    rec = SessionRecord(
        name="s1",
        instance_name="gcolab-s1-9999",
        project="proj",
        region="us-central1",
        zone="us-central1-a",
        machine_type="e2-standard-8",
        external_ip="203.0.113.5",
    )
    payload_file = tmp_path / "artifact.bin"
    raw = b"native-solver-binary-payload-" * 512
    payload_file.write_bytes(raw)
    expected_sha = hashlib.sha256(raw).hexdigest()

    priv = tmp_path / "id_ed25519"
    priv.write_text("fake", encoding="utf-8")
    monkeypatch.setattr("gcolab.transport.ensure_ssh_keypair", lambda key_path=None: (priv, "ssh-ed25519 AAAA"))
    monkeypatch.setattr("gcolab.transport.find_ssh_binary", lambda tool="ssh": f"{tool}.exe")
    monkeypatch.setattr("gcolab.transport.time.sleep", lambda _s: None)

    remote_fs: dict[str, bytes] = {}
    attempts: list[str] = []

    def fake_scp_upload(*args, **kwargs):
        attempts.append("scp")
        raise TransportError("Simulated SCP SFTP banner timeout")

    def fake_stream_send(ssh_cmd, local_file, *, start_offset, total_bytes, options, progress):
        attempts.append("stream")
        cmd_tail = ssh_cmd[-1]
        # Extract target .part path after 'cat > '
        part_path = cmd_tail.split("cat >", 1)[1].strip().strip("'\"")
        remote_fs[part_path] = pathlib.Path(local_file).read_bytes()[start_offset:]

    def fake_subproc_run(cmd, input=None, capture_output=True, timeout=60, check=False):
        remote_sh = cmd[-1]
        if "stat -c %s" in remote_sh and "sha256sum" not in remote_sh:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"0\n", stderr=b"")
        if "sha256sum" in remote_sh:
            # Verify staged bytes
            part_key = next(iter(remote_fs))
            bdata = remote_fs[part_key]
            dg = hashlib.sha256(bdata).hexdigest()
            out = f"{len(bdata)} {dg}\n".encode("utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")
        if "mv -f" in remote_sh:
            part_key = next(iter(remote_fs))
            remote_fs["/workspace/artifact.bin"] = remote_fs.pop(part_key)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("gcolab.transport._run_scp_upload", fake_scp_upload)
    monkeypatch.setattr("gcolab.transport._stream_send_file", fake_stream_send)
    monkeypatch.setattr("gcolab.transport.subprocess.run", fake_subproc_run)

    res = upload_path(
        store,
        rec,
        payload_file,
        remote_path="/workspace/artifact.bin",
        options=TransferOptions(method="auto", retries=2, show_progress=False),
    )
    assert attempts == ["scp", "stream"]
    assert res["transport"] == "stream"
    assert res["sha256"] == expected_sha
    assert res["verified"] is True
    assert remote_fs["/workspace/artifact.bin"] == raw


def test_run_retains_vm_and_fails_nonzero_when_required_deliverable_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "sessions.json"
    script = tmp_path / "solve.py"
    script.write_text("print('computed 515 files')\n", encoding="utf-8")

    events: list[str] = []

    def fake_provision(store, **kwargs):
        events.append("provision")
        rec = SessionRecord(
            name="solver-run",
            instance_name="gcolab-solver-run-a1b2",
            project="demo-proj",
            region="us-central1",
            zone="us-central1-a",
            machine_type="e2-standard-8",
            spot=True,
            external_ip="203.0.113.20",
            status="RUNNING",
        )
        store.upsert_session(rec)
        return rec

    monkeypatch.setattr("gcolab.cli._provision_session", fake_provision)
    monkeypatch.setattr(
        "gcolab.cli.upload_path",
        lambda store, rec, local_path, remote_path="/workspace", options=None: {
            "files_synced": 1,
            "bytes_transferred": 30,
            "sha256": "abc",
            "verified": True,
            "remote_path": "/workspace/solve.py",
        },
    )
    monkeypatch.setattr("gcolab.cli.ssh_run_stream", lambda store, rec, cmd, stdin_bytes=None: 0)
    monkeypatch.setattr(
        "gcolab.cli.download_path",
        lambda *a, **kw: (_ for _ in ()).throw(TransportError("Simulated network drop during download")),
    )
    monkeypatch.setattr(
        "gcolab.cli._set_remote_autostop",
        lambda store, rec, *, idle_minutes, autodown, wait_for="jobs_and_ssh": events.append(
            f"retain:{idle_minutes}:down={autodown}"
        ),
    )

    class MustNotDeleteClient:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def delete_and_verify_instance(self, *a, **kw):
            events.append("DELETED_PREMATURELY")
            raise AssertionError("VM must be retained for artifact recovery!")

    monkeypatch.setattr("gcolab.cli.ComputeClient", MustNotDeleteClient)

    rc = main(
        [
            "--config",
            str(cfg_file),
            "run",
            "--spot",
            "--download",
            f"/workspace/evidence:{tmp_path / 'evidence'}",
            "--retain-on-download-error-mins",
            "25",
            str(script),
        ]
    )
    assert rc == EXIT_ARTIFACT_DELIVERY_FAILED
    assert events == ["provision", "retain:25:down=True"]
    # Session remains in sessions.json so user/agent can run `gcolab download` then `gcolab down`
    store = StateStore(sessions_file=cfg_file)
    assert "solver-run" in store.load_sessions()


def test_workspace_bind_tagged_wait_and_verified_down_receipt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_file = tmp_path / "state" / "sessions.json"
    store = StateStore(sessions_file=cfg_file)
    rec = SessionRecord(
        name="build-env",
        instance_name="gcolab-build-env-7788",
        project="proj-verify",
        region="asia-east1",
        zone="asia-east1-a",
        machine_type="e2-standard-8",
        spot=True,
        provisioning_model="SPOT",
        boot_disk_name="gcolab-build-env-7788",
        disk_gb=60,
        disk_type="pd-balanced",
        external_ip="203.0.113.77",
        status="RUNNING",
    )
    store.upsert_session(rec)
    store.append_event(
        "build-env",
        "download",
        {
            "remote_path": "/workspace/evidence",
            "local_path": str(tmp_path / "evidence"),
            "files_synced": 515,
            "bytes_transferred": 1048576,
            "sha256": "deadbeef" * 8,
            "verified": True,
        },
    )

    ws = tmp_path / "bound_project"
    ws.mkdir()
    monkeypatch.chdir(ws)

    # Bind workspace to session
    rc_bind = main(["--config", str(cfg_file), "-o", "json", "bind", "-s", "build-env"])
    assert rc_bind == 0
    capsys.readouterr()
    assert find_workspace_binding(ws) is not None

    # Wait on a tagged job without passing -s or --config (resolved from .gcolab/project.json!)
    poll_responses = [
        {
            "found": True,
            "job": {"job_id": 2, "tag": "离线构建任务", "status": "RUNNING", "exit_code": None},
            "offset": 0,
            "next_offset": 12,
            "log_size": 12,
            "chunk": "[1/2] cmake\n",
            "done": False,
        },
        {
            "found": True,
            "job": {"job_id": 2, "tag": "离线构建任务", "status": "SUCCEEDED", "exit_code": 0},
            "offset": 12,
            "next_offset": 25,
            "log_size": 25,
            "chunk": "[2/2] passed\n",
            "done": True,
        },
    ]

    def fake_poll_ssh(store_arg, rec_arg, remote_cmd, timeout=35, connect_timeout=15):
        assert "离线构建任务" in remote_cmd
        payload = json.dumps(poll_responses.pop(0), ensure_ascii=False).encode("utf-8")
        return subprocess.CompletedProcess(["ssh"], 0, stdout=payload, stderr=b"")

    monkeypatch.setattr("gcolab.cli.ssh_run_capture", fake_poll_ssh)
    rc_wait = main(["-o", "json", "wait", "离线构建任务", "--poll", "0.01"])
    assert rc_wait == 0
    wait_out = json.loads(capsys.readouterr().out)
    assert wait_out["ok"] is True
    assert wait_out["next_offset"] == 25
    assert wait_out["new_output"] == "[1/2] cmake\n[2/2] passed\n"

    # Verified down with full receipt (confirming both VM 404 and boot disk 404)
    monkeypatch.setattr("gcolab.gcp_api.get_access_token_and_adc_project", lambda: ("tok", "proj-verify", "adc"))
    deleted_flag = {"done": False}

    class FakeResp:
        def __init__(self, code: int, body: dict[str, Any]) -> None:
            self.status_code = code
            self._body = body
            self.text = json.dumps(body)

        def json(self) -> dict[str, Any]:
            return self._body

    class FakeHttp:
        def request(self, method: str, url: str, headers=None, params=None, json=None, timeout=30):
            if method == "GET" and url.endswith("/instances/gcolab-build-env-7788"):
                if not deleted_flag["done"]:
                    return FakeResp(
                        200,
                        {
                            "name": "gcolab-build-env-7788",
                            "status": "RUNNING",
                            "creationTimestamp": "2026-11-12T12:00:00Z",
                            "labels": {MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE, "gcolab-session": "build-env"},
                            "scheduling": {"provisioningModel": "SPOT"},
                            "disks": [{"boot": True, "source": "zones/asia-east1-a/disks/gcolab-build-env-7788"}],
                        },
                    )
                return FakeResp(404, {"error": {"message": "Not Found"}})
            if method == "DELETE" and url.endswith("/instances/gcolab-build-env-7788"):
                deleted_flag["done"] = True
                return FakeResp(200, {"name": "op-del-99", "status": "DONE"})
            if method == "GET" and url.endswith("/operations/op-del-99"):
                return FakeResp(200, {"name": "op-del-99", "status": "DONE"})
            if method == "GET" and url.endswith("/disks/gcolab-build-env-7788"):
                return FakeResp(404, {"error": {"message": "Disk Not Found"}})
            if method == "GET" and "/aggregated/instances" in url:
                return FakeResp(200, {"items": {}})
            raise AssertionError(f"Unexpected HTTP {method} {url}")

    monkeypatch.setattr("gcolab.cli.ComputeClient", lambda project_id: ComputeClient(project_id, session=FakeHttp()))  # type: ignore[arg-type]

    # Run `gcolab down` without -s (resolved via workspace binding!)
    rc_down = main(["-o", "json", "down"])
    assert rc_down == 0
    down_out = json.loads(capsys.readouterr().out)
    assert down_out["deleted_sessions"] == ["build-env"]
    receipt = down_out["receipts"][0]
    assert receipt["instance_absent"] is True
    assert receipt["boot_disk_absent"] is True
    assert receipt["provisioning_model"] == "SPOT"
    assert receipt["deliverables"]["total_files_retrieved"] == 515
    assert receipt["deliverables"]["all_required_verified"] is True
    assert receipt["remaining_session_resources"] == []
    assert pathlib.Path(receipt["receipt_path"]).exists()
    # Workspace binding auto-cleared on down
    assert find_workspace_binding(ws) is None


def test_remote_job_runner_idempotency_tags_and_poll_offsets(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = types.ModuleType("remote_runner_test")
    exec(compile(REMOTE_JOB_RUNNER_PY, "job_runner.py", "exec"), mod.__dict__)
    mod.BASE = tmp_path
    mod.JOBS_DIR = tmp_path / "jobs"
    mod.LOGS_DIR = tmp_path / "logs"
    mod.LOCKS_DIR = tmp_path / "locks"
    mod.SEQ_PATH = mod.JOBS_DIR / "seq"
    mod.SUBMIT_LOCK_PATH = mod.LOCKS_DIR / "submit.lock"
    mod.STATE_PATH = tmp_path / "autostop_state.json"
    mod.ensure_dirs()

    # Verify monotonic sequence allocation
    assert mod.allocate_job_id_unlocked() == 1
    assert mod.allocate_job_id_unlocked() == 2

    # Write a tagged, idempotent job record and verify lookup + deduplication + byte-offset poll
    log_p = mod.LOGS_DIR / "2.log"
    log_p.write_bytes(b"line1\nline2\n")
    meta = {
        "job_id": 2,
        "tag": "数据预处理",
        "idempotency_key": "preprocess-v1",
        "command": "python3 preprocess.py",
        "workdir": "/workspace",
        "status": "SUCCEEDED",
        "pid": 999999,
        "log_path": str(log_p),
        "started_at": "2026-11-13T00:00:00Z",
        "ended_at": "2026-11-13T00:05:00Z",
        "exit_code": 0,
    }
    mod.atomic_write_json(mod.JOBS_DIR / "2.json", meta)

    resolved = mod.resolve_job_ref(job_ref="数据预处理")
    assert resolved is not None and resolved["job_id"] == 2

    dedup = mod.find_idempotent_job_unlocked("preprocess-v1", retry_failed=False)
    assert dedup is not None and dedup["job_id"] == 2

    ns = types.SimpleNamespace(job_id=None, job_ref="数据预处理", offset=6, max_bytes=4096)
    rc = mod.cmd_poll(ns)
    assert rc == 0
    polled = json.loads(capsys.readouterr().out)
    assert polled["chunk"] == "line2\n"
    assert polled["next_offset"] == 12
    assert polled["done"] is True


def test_stream_send_stall_watchdog_aborts_hung_process_promptly(tmp_path: pathlib.Path) -> None:
    import sys as _sys
    from gcolab.transport import _StageProgress, _stream_send_file

    # Create a 4 MiB file so OS pipe buffer fills and writer stalls when child refuses to read stdin
    big_file = tmp_path / "stalled.bin"
    big_file.write_bytes(b"x" * (4 * 1024 * 1024))
    prog = _StageProgress("stall-test", enabled=False)
    prog.stage = "send"

    t0 = time.monotonic()
    with pytest.raises(TransportError, match="Upload stalled"):
        _stream_send_file(
            [_sys.executable, "-c", "import time; time.sleep(30)"],
            big_file,
            start_offset=0,
            total_bytes=4 * 1024 * 1024,
            options=TransferOptions(stall_timeout=1, max_duration=30, show_progress=False),
            progress=prog,
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 6.0


def test_run_exits_teardown_failed_and_retains_session_when_teardown_raises(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "sessions.json"
    script = tmp_path / "job_ok.py"
    script.write_text("print('ok')\n", encoding="utf-8")

    def fake_provision(store: StateStore, **kwargs: Any) -> SessionRecord:
        rec = SessionRecord(
            name="td-err",
            instance_name="gcolab-td-err-1234",
            project="demo-proj",
            region="us-central1",
            zone="us-central1-a",
            machine_type="e2-standard-4",
            spot=True,
            external_ip="203.0.113.50",
            status="RUNNING",
        )
        store.upsert_session(rec)
        return rec

    monkeypatch.setattr("gcolab.cli._provision_session", fake_provision)
    monkeypatch.setattr(
        "gcolab.cli.upload_path",
        lambda store, rec, local_path, remote_path="/workspace", options=None: {
            "files_synced": 1,
            "bytes_transferred": 16,
            "sha256": "abc",
            "verified": True,
            "remote_path": "/workspace/job_ok.py",
        },
    )
    monkeypatch.setattr("gcolab.cli.ssh_run_stream", lambda store, rec, cmd, stdin_bytes=None: 0)

    class FailingDeleteClient:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def delete_and_verify_instance(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise GCPApiError("503 Compute Engine backend unavailable", status_code=503)

    monkeypatch.setattr("gcolab.cli.ComputeClient", FailingDeleteClient)

    rc = main(["--config", str(cfg_file), "run", "--spot", str(script)])
    assert rc == EXIT_TEARDOWN_FAILED

    store = StateStore(sessions_file=cfg_file)
    sessions = store.load_sessions()
    assert "td-err" in sessions
    assert sessions["td-err"].status == "TEARDOWN_FAILED"
    events = store.read_events("td-err", event_type="teardown_failed")
    assert len(events) == 1


def test_run_exits_teardown_failed_when_post_delete_404_verification_incomplete(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "sessions.json"
    task_yaml = tmp_path / "setup_only.yaml"
    task_yaml.write_text(
        "name: setup-check\nresources:\n  cpus: 4\n  use_spot: true\nsetup: |\n  echo ready\n",
        encoding="utf-8",
    )

    def fake_provision(store: StateStore, **kwargs: Any) -> SessionRecord:
        rec = SessionRecord(
            name="setup-check",
            instance_name="gcolab-setup-check-5678",
            project="demo-proj",
            region="us-central1",
            zone="us-central1-a",
            machine_type="e2-standard-4",
            boot_disk_name="gcolab-setup-check-5678",
            spot=True,
            provisioning_model="SPOT",
            external_ip="203.0.113.51",
            status="RUNNING",
        )
        store.upsert_session(rec)
        return rec

    monkeypatch.setattr("gcolab.cli._provision_session", fake_provision)
    monkeypatch.setattr("gcolab.cli.ssh_run_stream", lambda store, rec, cmd, stdin_bytes=None: 0)

    class UnverifiedDiskClient:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def delete_and_verify_instance(self, zone: str, instance_name: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "was_present": True,
                "instance_name": instance_name,
                "boot_disk_name": instance_name,
                "instance_absent": True,
                "boot_disk_absent": False,
                "session_scan_verified": True,
                "remaining_session_resources": [],
                "teardown_verified": False,
                "provisioning_model": "SPOT",
            }

    monkeypatch.setattr("gcolab.cli.ComputeClient", UnverifiedDiskClient)

    rc = main(["--config", str(cfg_file), "run", str(task_yaml)])
    assert rc == EXIT_TEARDOWN_FAILED

    store = StateStore(sessions_file=cfg_file)
    sessions = store.load_sessions()
    assert "setup-check" in sessions
    assert sessions["setup-check"].status == "TEARDOWN_INCOMPLETE"
    receipt = json.loads((store.history_dir / "setup-check-receipt.json").read_text(encoding="utf-8"))
    assert receipt["teardown_verified"] is False
    assert any("boot disk" in item for item in receipt["verification_failures"])


def test_down_exits_teardown_failed_until_404_verified(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_file = tmp_path / "state" / "sessions.json"
    store = StateStore(sessions_file=cfg_file)
    rec = SessionRecord(
        name="s-verify",
        instance_name="gcolab-s-verify-1122",
        project="proj-verify",
        region="us-central1",
        zone="us-central1-a",
        machine_type="e2-standard-4",
        boot_disk_name="gcolab-s-verify-1122",
        spot=True,
        provisioning_model="SPOT",
        status="RUNNING",
    )
    store.upsert_session(rec)

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    assert main(["--config", str(cfg_file), "bind", "-s", "s-verify"]) == 0
    capsys.readouterr()

    state = {"disk_gone": False}

    class StagedVerifyClient:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def delete_and_verify_instance(self, zone: str, instance_name: str, **kwargs: Any) -> dict[str, Any]:
            gone = state["disk_gone"]
            return {
                "was_present": not gone,
                "instance_name": instance_name,
                "boot_disk_name": instance_name,
                "instance_absent": True,
                "boot_disk_absent": gone,
                "session_scan_verified": True,
                "remaining_session_resources": [],
                "teardown_verified": gone,
                "provisioning_model": "SPOT",
            }

    monkeypatch.setattr("gcolab.cli.ComputeClient", StagedVerifyClient)

    rc_first = main(["-o", "json", "down"])
    assert rc_first == EXIT_TEARDOWN_FAILED
    out_first = json.loads(capsys.readouterr().out)
    assert out_first["ok"] is False
    assert out_first["deleted_sessions"] == []
    assert len(out_first["failed_sessions"]) == 1
    assert "s-verify" in store.load_sessions()
    assert find_workspace_binding(ws) is not None

    state["disk_gone"] = True
    rc_second = main(["-o", "json", "down"])
    assert rc_second == 0
    out_second = json.loads(capsys.readouterr().out)
    assert out_second["ok"] is True
    assert out_second["deleted_sessions"] == ["s-verify"]
    assert "s-verify" not in store.load_sessions()
    assert find_workspace_binding(ws) is None


def test_cleanup_apply_exits_teardown_failed_when_instance_remains(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_file = tmp_path / "state" / "sessions.json"
    store = StateStore(sessions_file=cfg_file)
    for name in ("s-ok", "s-stuck"):
        store.upsert_session(
            SessionRecord(
                name=name,
                instance_name=f"gcolab-{name}-0001",
                project="proj-clean",
                region="us-central1",
                zone="us-central1-a",
                machine_type="e2-standard-4",
                status="RUNNING",
            )
        )

    class PartialCleanupClient:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id
            self.calls = 0

        def list_managed_instances(self) -> list[dict[str, Any]]:
            self.calls += 1
            if self.calls == 1:
                return [
                    {"name": "gcolab-s-ok-0001", "session": "s-ok", "zone": "us-central1-a", "status": "RUNNING"},
                    {"name": "gcolab-s-stuck-0001", "session": "s-stuck", "zone": "us-central1-a", "status": "STOPPING"},
                ]
            return [{"name": "gcolab-s-stuck-0001", "session": "s-stuck", "zone": "us-central1-a", "status": "STOPPING"}]

        def delete_and_verify_instance(self, zone: str, instance_name: str, **kwargs: Any) -> dict[str, Any]:
            is_ok = instance_name == "gcolab-s-ok-0001"
            return {
                "was_present": True,
                "instance_name": instance_name,
                "boot_disk_name": instance_name,
                "instance_absent": is_ok,
                "boot_disk_absent": is_ok,
                "session_scan_verified": True,
                "remaining_session_resources": [],
                "teardown_verified": is_ok,
            }

    client = PartialCleanupClient("proj-clean")
    monkeypatch.setattr("gcolab.cli._resolve_client_and_project", lambda st, ep: (client, "proj-clean"))

    rc = main(["--config", str(cfg_file), "-o", "json", "cleanup", "--apply"])
    assert rc == EXIT_TEARDOWN_FAILED
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["deleted_instances"] == ["us-central1-a/gcolab-s-ok-0001"]
    assert len(out["failed_instances"]) == 1
    remaining_local = store.load_sessions()
    assert "s-ok" not in remaining_local
    assert "s-stuck" in remaining_local


def test_delete_and_verify_instance_flags_failed_session_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gcolab.gcp_api.get_access_token_and_adc_project", lambda: ("tok", "proj-1", "adc"))
    monkeypatch.setattr("gcolab.gcp_api.time.sleep", lambda _s: None)

    class FakeResp:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self) -> dict[str, Any]:
            return self._payload

    class FakeHttp:
        def request(self, method: str, url: str, headers=None, params=None, json=None, timeout=30):
            if method == "GET" and url.endswith("/instances/gcolab-s1-0001"):
                return FakeResp(404, {"error": {"message": "Not Found"}})
            if method == "GET" and url.endswith("/disks/gcolab-s1-0001"):
                return FakeResp(404, {"error": {"message": "Disk Not Found"}})
            if method == "GET" and "/aggregated/instances" in url:
                return FakeResp(503, {"error": {"message": "Service Unavailable"}})
            raise AssertionError(f"Unexpected {method} {url}")

    client = ComputeClient("proj-1", session=FakeHttp())  # type: ignore[arg-type]
    ver = client.delete_and_verify_instance("us-central1-a", "gcolab-s1-0001", session_name="s1")
    assert ver["instance_absent"] is True
    assert ver["boot_disk_absent"] is True
    assert ver["session_scan_verified"] is False
    assert ver["teardown_verified"] is False


def test_run_tears_down_vm_when_readiness_fails_mid_provision(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "sessions.json"
    script = tmp_path / "hello.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    deleted_calls: list[str] = []

    def fake_provision_partial(store: StateStore, *, session_name: str, **kwargs: Any) -> SessionRecord:
        rec = SessionRecord(
            name=session_name,
            instance_name=f"gcolab-{session_name}-9090",
            project="demo-proj",
            region="us-central1",
            zone="us-central1-a",
            machine_type="e2-standard-4",
            boot_disk_name=f"gcolab-{session_name}-9090",
            spot=True,
            provisioning_model="SPOT",
            status="RUNNING",
        )
        store.upsert_session(rec)
        raise TransportError("SSH readiness probe timed out")

    class VerifyDeleteClient:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def delete_and_verify_instance(self, zone: str, instance_name: str, **kwargs: Any) -> dict[str, Any]:
            deleted_calls.append(f"{zone}/{instance_name}")
            return {
                "was_present": True,
                "instance_name": instance_name,
                "boot_disk_name": instance_name,
                "instance_absent": True,
                "boot_disk_absent": True,
                "session_scan_verified": True,
                "remaining_session_resources": [],
                "teardown_verified": True,
                "provisioning_model": "SPOT",
            }

    monkeypatch.setattr("gcolab.cli._provision_session", fake_provision_partial)
    monkeypatch.setattr("gcolab.cli.ComputeClient", VerifyDeleteClient)

    rc = main(["--config", str(cfg_file), "run", "-s", "partial-boot", "--spot", str(script)])
    assert rc == 1
    assert deleted_calls == ["us-central1-a/gcolab-partial-boot-9090"]
    assert "partial-boot" not in StateStore(sessions_file=cfg_file).load_sessions()
