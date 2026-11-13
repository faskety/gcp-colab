"""Tests for Windows tarfile archive builder and embedded remote daemon/runner scripts."""

from __future__ import annotations

import io
import json
import pathlib
import tarfile
import types

from gcolab.remote_agent import REMOTE_DAEMON_PY, REMOTE_JOB_RUNNER_PY, render_startup_script
from gcolab.transport import build_tar_gz_bytes


def test_windows_tar_builder_excludes_secrets_and_normalizes_shell_crlf(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"123")
    (tmp_path / ".env").write_text("API_KEY=topsecret\n", encoding="utf-8")
    (tmp_path / "prod.env").write_text("API_KEY=prodsecret\n", encoding="utf-8")
    (tmp_path / ".env-staging").write_text("API_KEY=stagesecret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("API_KEY=placeholder\n", encoding="utf-8")
    (tmp_path / "gcolab_ed25519").write_text("PRIVATE KEY\n", encoding="utf-8")
    (tmp_path / "deploy_ed25519").write_text("PRIVATE KEY\n", encoding="utf-8")
    (tmp_path / "key.json").write_text("{}", encoding="utf-8")
    (tmp_path / "oauth_client.json").write_text("{}", encoding="utf-8")
    (tmp_path / "my-credentials-prod.json").write_text("{}", encoding="utf-8")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "notes.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("checkpoints/\n/work/\n*.ckpt\n", encoding="utf-8")
    (tmp_path / "checkpoints").mkdir()
    (tmp_path / "checkpoints" / "model.pt").write_bytes(b"big")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "scratch.bin").write_bytes(b"tmp")
    (tmp_path / "weights.ckpt").write_bytes(b"big2")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "train.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "run_entry.sh").write_bytes(b"#!/bin/bash\r\necho hello\r\n")

    archive, count = build_tar_gz_bytes(tmp_path, respect_gitignore=True)
    # Only .env.example, .gitignore, run_entry.sh, src/train.py should be included
    assert count == 4

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        names = set(tf.getnames())
        assert names == {".env.example", ".gitignore", "run_entry.sh", "src/train.py"}
        sh_info = tf.getmember("run_entry.sh")
        assert sh_info.mode == 0o755
        sh_bytes = tf.extractfile("run_entry.sh").read()  # type: ignore[union-attr]
        assert b"\r\n" not in sh_bytes
        assert sh_bytes == b"#!/bin/bash\necho hello\n"


def test_embedded_remote_scripts_compile_and_autostop_state_machine(tmp_path: pathlib.Path) -> None:
    compile(REMOTE_DAEMON_PY, "/opt/gcolab/daemon.py", "exec")
    compile(REMOTE_JOB_RUNNER_PY, "/opt/gcolab/job_runner.py", "exec")

    startup = render_startup_script(ssh_user="gcolab", idle_minutes=25, autodown=True, has_gpu=True)
    assert "gcolab-autostop.timer" in startup
    assert "/opt/gcolab/READY" in startup
    assert '"idle_minutes": 25' in startup

    # Execute daemon module in isolated namespace with mocked paths to verify idle logic
    mod = types.ModuleType("remote_daemon_test")
    exec(compile(REMOTE_DAEMON_PY, "daemon.py", "exec"), mod.__dict__)

    mod.BASE = tmp_path
    mod.CONFIG_PATH = tmp_path / "autostop.json"
    mod.STATE_PATH = tmp_path / "autostop_state.json"
    mod.JOBS_DIR = tmp_path / "jobs"
    mod.LOCKS_DIR = tmp_path / "locks"
    mod.boot_time_seconds = lambda: 1000.0
    mod.uptime_seconds = lambda: 500.0
    mod.has_running_jobs = lambda: False
    mod.has_active_gpu_compute = lambda: False
    mod.has_active_ssh_sessions = lambda: False

    deleted = {"called": False}
    mod.try_self_delete = lambda: deleted.update(called=True) or True

    mod.CONFIG_PATH.write_text(json.dumps({"idle_minutes": 10, "autodown": True, "wait_for": "jobs_and_ssh"}))

    # First check initializes last_active=2000
    r1 = mod.check_once(now=2000.0)
    assert r1["action"] == "idle_waiting"

    # 5 minutes later -> still waiting
    r2 = mod.check_once(now=2300.0)
    assert r2["action"] == "idle_waiting"

    # 11 minutes later -> triggers self_delete!
    r3 = mod.check_once(now=2660.0)
    assert r3["action"] == "self_delete_requested"
    assert deleted["called"] is True
