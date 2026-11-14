"""Host discovery, native OpenSSH, permissions, and locking on supported systems."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile

import pytest

from gcolab import auth, transport
from gcolab.cli import main
from gcolab.config import SessionRecord, StateStore


@pytest.mark.parametrize("platform,command", [("win32", "gcloud.cmd"), ("darwin", "gcloud"), ("linux", "gcloud")])
def test_gcloud_discovery_uses_host_executable(monkeypatch, platform, command):
    monkeypatch.delenv("GCOLAB_GCLOUD_BIN", raising=False)
    monkeypatch.setattr(auth.sys, "platform", platform)
    searched = []

    def which(name):
        searched.append(name)
        return f"/sdk/bin/{name}" if name == command else None

    monkeypatch.setattr(auth.shutil, "which", which)
    assert auth.find_gcloud_binary() == f"/sdk/bin/{command}"
    assert searched == [command]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_gcloud_sdk_fallback_in_user_home(tmp_path, monkeypatch, platform):
    monkeypatch.delenv("GCOLAB_GCLOUD_BIN", raising=False)
    monkeypatch.setattr(auth.sys, "platform", platform)
    monkeypatch.setattr(auth.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    executable = tmp_path / "google-cloud-sdk/bin/gcloud"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    assert auth.find_gcloud_binary() == str(executable)


def test_gcloud_override_expands_home_and_rejects_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    executable = tmp_path / "custom-sdk"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("GCOLAB_GCLOUD_BIN", "~/custom-sdk")
    assert auth.find_gcloud_binary() == str(executable.resolve())
    monkeypatch.setenv("GCOLAB_GCLOUD_BIN", str(tmp_path / "missing-sdk"))
    assert auth.find_gcloud_binary() is None


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_adc_location_and_explicit_overrides(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(auth.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("CLOUDSDK_CONFIG", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    base = tmp_path / "roaming/gcloud" if platform == "win32" else tmp_path / ".config/gcloud"
    assert auth.adc_file_path() == base / "application_default_credentials.json"
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "custom-config"))
    assert auth.adc_file_path() == tmp_path / "custom-config/application_default_credentials.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "explicit.json"))
    assert auth.adc_file_path() == tmp_path / "explicit.json"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_posix_openssh_search_uses_path(monkeypatch, platform):
    monkeypatch.setattr(transport.sys, "platform", platform)
    searched = []

    def which(name):
        searched.append(name)
        return f"/usr/bin/{name}"

    monkeypatch.setattr(transport.shutil, "which", which)
    assert transport.find_ssh_binary("ssh") == "/usr/bin/ssh"
    assert searched == ["ssh"]


def test_windows_openssh_uses_system_root(tmp_path, monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    executable = tmp_path / "System32/OpenSSH/ssh.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder")
    assert transport.find_ssh_binary("ssh") == str(executable)


def test_doctor_reports_missing_openssh_as_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("gcolab.cli.inspect_local_auth_status", lambda settings: {
        "credentials_valid": False, "resolved_project": None,
        "gcloud_installed": False, "gcloud_path": None,
    })

    def missing(tool):
        raise transport.TransportError("OpenSSH client is missing")

    monkeypatch.setattr("gcolab.cli.find_ssh_binary", missing)
    assert main(["--config", str(tmp_path / "sessions.json"), "doctor", "-o", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["openssh_available"] is False
    assert report["gcolab_ssh_key"] is None
    assert "missing" in report["openssh_error"]


def test_native_key_generation_and_public_key_recovery(tmp_path):
    private, public = transport.ensure_ssh_keypair(tmp_path / "ssh keys" / "client.key")
    original = private.read_bytes()
    assert public.startswith("ssh-ed25519 ")
    public_file = Path(f"{private}.pub")
    public_file.unlink()
    recovered_private, recovered_public = transport.ensure_ssh_keypair(private)
    assert recovered_private.read_bytes() == original
    assert recovered_public.split()[:2] == public.split()[:2]
    if os.name != "nt":
        assert stat.S_IMODE(private.stat().st_mode) == 0o600
        assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700


def test_native_ssh_accepts_config_with_spaces(tmp_path):
    store = StateStore(tmp_path / "state with spaces" / "sessions.json")
    key, _ = transport.ensure_ssh_keypair(tmp_path / "ssh keys" / "client")
    args = transport._base_ssh_args(store, key, "127.0.0.1", 22, "gcolab")
    result = subprocess.run([args[0], "-G", *args[1:]], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    known_hosts = next(line for line in result.stdout.splitlines() if line.startswith("userknownhostsfile "))
    assert str(store.known_hosts_path).replace("\\", "/") in known_hosts


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
def test_archive_and_upload_preserve_posix_executable_modes(tmp_path, monkeypatch):
    executable = tmp_path / "solver"
    executable.write_bytes(b"executable-placeholder")
    executable.chmod(0o750)
    archive, count = transport.build_tar_gz_bytes(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        assert tf.getmember("solver").mode == 0o750
    assert count == 1
    commands = []
    monkeypatch.setattr(transport, "_transfer_file_to_remote_staged", lambda *args, **kwargs: "stream")

    def capture(store, session, command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(["ssh"], 0, b"", b"")

    monkeypatch.setattr(transport, "ssh_run_capture", capture)
    session = SessionRecord("example", "example-vm", "example-project", "us-central1", "us-central1-a", "e2-standard-2")
    result = transport.upload_path(StateStore(tmp_path / "state/sessions.json"), session, executable, "/workspace/solver")
    assert result["verified"] is True
    assert commands[-1].startswith("chmod 0750 ")


def test_native_state_lock_blocks_another_process(tmp_path):
    store = StateStore(tmp_path / "state/sessions.json")
    source = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from gcolab.config import StateStore\n"
        "try:\n"
        "    with StateStore(sys.argv[2]).locked(timeout_s=0.2): pass\n"
        "except TimeoutError:\n"
        "    print('blocked')\n"
        "else:\n"
        "    print('acquired')\n"
    )
    with store.locked():
        result = subprocess.run([sys.executable, "-c", code, str(source), str(store.sessions_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "blocked"
    store.save_settings({"default_project": "example-project"})
    if os.name != "nt":
        assert stat.S_IMODE(store.settings_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions")
def test_existing_private_key_permissions_are_repaired(tmp_path):
    key, _ = transport.ensure_ssh_keypair(tmp_path / "key")
    key.chmod(0o644)
    transport.ensure_ssh_keypair(key)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
