"""Embedded VM-side job runner and idle-autostop systemd daemon for GCE Linux instances.

Provides an autonomous zero-dependency Python 3 agent injected via GCE startup-script.
"""

from __future__ import annotations

import json
import shlex

# Executed inside the GCE Linux VM under /opt/gcolab/daemon.py (Python 3 stdlib only)
REMOTE_DAEMON_PY = r'''#!/usr/bin/env python3
"""gcolab VM-side idle autostop/autodown monitor (runs every 60s via systemd timer)."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
import urllib.request

BASE = pathlib.Path("/opt/gcolab")
CONFIG_PATH = BASE / "autostop.json"
STATE_PATH = BASE / "autostop_state.json"
JOBS_DIR = BASE / "jobs"
LOCKS_DIR = BASE / "locks"
GRACE_SECONDS_AFTER_BOOT = 180


def atomic_write_json(path: pathlib.Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def boot_time_seconds() -> float:
    try:
        for line in pathlib.Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except Exception:
        pass
    return 0.0


def uptime_seconds() -> float:
    try:
        return float(pathlib.Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return 9999.0


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    return pathlib.Path(f"/proc/{pid}").exists()


def has_running_jobs() -> bool:
    if LOCKS_DIR.exists():
        for lock in LOCKS_DIR.glob("*.pid"):
            try:
                pid = int(lock.read_text().strip())
                if pid_alive(pid):
                    return True
            except Exception:
                continue
    if JOBS_DIR.exists():
        for jf in JOBS_DIR.glob("*.json"):
            try:
                meta = json.loads(jf.read_text())
            except Exception:
                continue
            if meta.get("status") == "RUNNING" and pid_alive(meta.get("pid")):
                return True
    return False


def has_active_ssh_sessions() -> bool:
    try:
        res = subprocess.run(["who"], capture_output=True, text=True, timeout=5, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return True
    except Exception:
        pass
    return False


def has_active_gpu_compute() -> bool:
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.isdigit() and int(line) >= 5:
                    return True
    except Exception:
        pass
    return False


def metadata_get(rel_path: str) -> str:
    req = urllib.request.Request(
        f"http://metadata.google.internal/computeMetadata/v1/{rel_path}",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8").strip()


def try_self_delete() -> bool:
    """Attempt GCE API self-deletion if VM service account has compute.instances.delete."""
    try:
        project = metadata_get("project/project-id")
        zone_full = metadata_get("instance/zone")
        zone = zone_full.rsplit("/", 1)[-1]
        name = metadata_get("instance/name")
        token_raw = metadata_get("instance/service-accounts/default/token")
        token = json.loads(token_raw)["access_token"]
        url = f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/instances/{name}"
        req = urllib.request.Request(
            url,
            method="DELETE",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def check_once(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not CONFIG_PATH.exists():
        return {"action": "disabled"}
    cfg = json.loads(CONFIG_PATH.read_text())
    idle_minutes = int(cfg.get("idle_minutes", -1))
    if idle_minutes < 0:
        return {"action": "disabled"}
    wait_for = cfg.get("wait_for", "jobs_and_ssh")
    autodown = bool(cfg.get("autodown", False))

    btime = boot_time_seconds()
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except Exception:
            state = {}

    if state.get("boot_time") != btime:
        state = {"boot_time": btime, "last_active": now, "triggered": False}

    busy = False
    if wait_for in ("jobs_and_ssh", "jobs"):
        busy = busy or has_running_jobs() or has_active_gpu_compute()
    if wait_for == "jobs_and_ssh":
        busy = busy or has_active_ssh_sessions()

    if busy:
        state["last_active"] = now
        atomic_write_json(STATE_PATH, state)
        return {"action": "busy", "last_active": now}

    atomic_write_json(STATE_PATH, state)
    idle_sec = now - float(state.get("last_active", now))
    if uptime_seconds() < GRACE_SECONDS_AFTER_BOOT:
        return {"action": "boot_grace", "idle_sec": idle_sec}

    if idle_sec >= idle_minutes * 60 and not state.get("triggered"):
        state["triggered"] = True
        state["triggered_at"] = now
        atomic_write_json(STATE_PATH, state)
        if autodown:
            if try_self_delete():
                return {"action": "self_delete_requested", "idle_sec": idle_sec}
        subprocess.run(["/sbin/shutdown", "-h", "now", "gcolab idle autostop"], check=False)
        return {"action": "shutdown_requested", "idle_sec": idle_sec}

    return {"action": "idle_waiting", "idle_sec": idle_sec, "threshold_sec": idle_minutes * 60}


if __name__ == "__main__":
    check_once()
'''

# Executed inside the GCE Linux VM under /opt/gcolab/job_runner.py
REMOTE_JOB_RUNNER_PY = r'''#!/usr/bin/env python3
"""gcolab VM-side job queue, tagged aliases, idempotent submit, and incremental log poller."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Iterator

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

BASE = pathlib.Path("/opt/gcolab")
JOBS_DIR = BASE / "jobs"
LOGS_DIR = BASE / "logs"
LOCKS_DIR = BASE / "locks"
SEQ_PATH = JOBS_DIR / "seq"
SUBMIT_LOCK_PATH = LOCKS_DIR / "submit.lock"
STATE_PATH = BASE / "autostop_state.json"

EXIT_SUCCEEDED = 0
EXIT_FAILED = 100
EXIT_NOT_FINISHED = 101
EXIT_NOT_FOUND = 102
EXIT_CANCELLED = 103


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_dirs() -> None:
    for d in (JOBS_DIR, LOGS_DIR, LOCKS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: pathlib.Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


@contextlib.contextmanager
def submit_lock() -> Iterator[None]:
    ensure_dirs()
    fd = os.open(str(SUBMIT_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o666)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def touch_activity() -> None:
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text())
            st["last_active"] = time.time()
            atomic_write_json(STATE_PATH, st)
        except Exception:
            pass


def pid_alive(pid: int | None) -> bool:
    return bool(pid and pid > 0 and pathlib.Path(f"/proc/{pid}").exists())


def allocate_job_id_unlocked() -> int:
    existing = [int(p.stem) for p in JOBS_DIR.glob("*.json") if p.stem.isdigit()]
    seq_val = 0
    if SEQ_PATH.exists():
        try:
            seq_val = int(SEQ_PATH.read_text().strip())
        except Exception:
            seq_val = 0
    nxt = max([seq_val, *existing], default=0) + 1
    SEQ_PATH.write_text(str(nxt))
    return nxt


def refresh_job_meta(meta: dict) -> dict:
    if meta.get("status") == "RUNNING" and not pid_alive(meta.get("pid")):
        rc_file = JOBS_DIR / f"{meta['job_id']}.rc"
        if rc_file.exists():
            try:
                rc = int(rc_file.read_text().strip())
            except Exception:
                rc = 1
            meta["exit_code"] = rc
            meta["status"] = "SUCCEEDED" if rc == 0 else "FAILED"
        else:
            meta["status"] = "FAILED"
            meta["exit_code"] = -1
        meta.setdefault("ended_at", utc_now())
        atomic_write_json(JOBS_DIR / f"{meta['job_id']}.json", meta)
    return meta


def load_job(job_id: int) -> dict | None:
    jf = JOBS_DIR / f"{job_id}.json"
    if not jf.exists():
        return None
    meta = json.loads(jf.read_text())
    return refresh_job_meta(meta)


def list_jobs() -> list[dict]:
    ensure_dirs()
    items = []
    for jf in sorted(JOBS_DIR.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        if not jf.stem.isdigit():
            continue
        try:
            items.append(refresh_job_meta(json.loads(jf.read_text())))
        except Exception:
            continue
    return items


def resolve_job_ref(job_ref: str | None = None, job_id: int | None = None) -> dict | None:
    if job_id is not None:
        return load_job(job_id)
    jobs = list_jobs()
    if not job_ref:
        return jobs[-1] if jobs else None
    ref = str(job_ref).strip()
    if ref.isdigit():
        found = load_job(int(ref))
        if found is not None:
            return found
    matching = [j for j in jobs if j.get("tag") == ref]
    return matching[-1] if matching else None


def find_idempotent_job_unlocked(key: str, retry_failed: bool = False) -> dict | None:
    for job in reversed(list_jobs()):
        if job.get("idempotency_key") != key:
            continue
        st = job.get("status")
        if st in ("RUNNING", "SUCCEEDED"):
            return job
        if not retry_failed:
            return job
    return None


def cmd_submit(args: argparse.Namespace) -> int:
    ensure_dirs()
    touch_activity()
    workdir = args.workdir or "/workspace"
    pathlib.Path(workdir).mkdir(parents=True, exist_ok=True)

    env_map = os.environ.copy()
    for item in args.env or []:
        if "=" in item:
            k, v = item.split("=", 1)
            env_map[k] = v

    command_str = args.command

    with submit_lock():
        if args.idempotency_key:
            existing = find_idempotent_job_unlocked(args.idempotency_key, retry_failed=args.retry_failed)
            if existing is not None:
                out = {**existing, "deduplicated": True}
                if args.detach:
                    print(json.dumps(out, ensure_ascii=False))
                    return 0
                if existing.get("status") == "SUCCEEDED":
                    return 0
                if existing.get("status") == "RUNNING":
                    print(json.dumps(out, ensure_ascii=False))
                    return 0
                return int(existing.get("exit_code") or 1)

        jid = allocate_job_id_unlocked()
        log_path = LOGS_DIR / f"{jid}.log"
        rc_path = JOBS_DIR / f"{jid}.rc"
        meta_path = JOBS_DIR / f"{jid}.json"

        wrapper_sh = (
            f"cd {json.dumps(workdir)} && ( {command_str} ) ; "
            f"__rc=$?; printf '%s' \"$__rc\" > {json.dumps(str(rc_path))}; exit $__rc"
        )

        if args.detach:
            with log_path.open("wb") as lf:
                proc = subprocess.Popen(
                    ["/bin/bash", "-lc", wrapper_sh],
                    stdin=subprocess.DEVNULL,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    env=env_map,
                    start_new_session=True,
                )
            meta = {
                "job_id": jid,
                "tag": args.tag or None,
                "idempotency_key": args.idempotency_key or None,
                "command": command_str,
                "workdir": workdir,
                "status": "RUNNING",
                "pid": proc.pid,
                "log_path": str(log_path),
                "started_at": utc_now(),
                "ended_at": None,
                "exit_code": None,
            }
            atomic_write_json(meta_path, meta)
            print(json.dumps(meta, ensure_ascii=False))
            return 0

        lock_file = LOCKS_DIR / f"sync-{os.getpid()}.pid"
        lock_file.write_text(str(os.getpid()))
        meta = {
            "job_id": jid,
            "tag": args.tag or None,
            "idempotency_key": args.idempotency_key or None,
            "command": command_str,
            "workdir": workdir,
            "status": "RUNNING",
            "pid": os.getpid(),
            "log_path": str(log_path),
            "started_at": utc_now(),
            "ended_at": None,
            "exit_code": None,
        }
        atomic_write_json(meta_path, meta)

    try:
        with log_path.open("wb") as lf:
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", wrapper_sh],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env_map,
            )
            meta["pid"] = proc.pid
            atomic_write_json(meta_path, meta)
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                lf.write(chunk)
                lf.flush()
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            rc = proc.wait()
    finally:
        lock_file.unlink(missing_ok=True)
        touch_activity()

    meta["exit_code"] = rc
    meta["status"] = "SUCCEEDED" if rc == 0 else "FAILED"
    meta["ended_at"] = utc_now()
    atomic_write_json(meta_path, meta)
    return rc


def cmd_queue(args: argparse.Namespace) -> int:
    jobs = list_jobs()
    if args.tag:
        jobs = [j for j in jobs if j.get("tag") == args.tag]
    print(json.dumps({"jobs": jobs}, ensure_ascii=False))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    job = resolve_job_ref(job_ref=args.job_ref, job_id=args.job_id)
    if not job:
        if args.status:
            return EXIT_NOT_FOUND
        print("No matching job found on this session.", file=sys.stderr)
        return EXIT_NOT_FOUND

    if args.status:
        st = job.get("status")
        if st == "SUCCEEDED":
            return EXIT_SUCCEEDED
        if st == "FAILED":
            return EXIT_FAILED
        if st == "RUNNING":
            return EXIT_NOT_FINISHED
        if st == "CANCELLED":
            return EXIT_CANCELLED
        return EXIT_FAILED

    lpath = pathlib.Path(job["log_path"])
    if not lpath.exists():
        return 0
    lines = lpath.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.tail and args.tail > 0:
        lines = lines[-args.tail :]
    for ln in lines:
        print(ln)
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    job = resolve_job_ref(job_ref=args.job_ref, job_id=args.job_id)
    if not job:
        print(json.dumps({"found": False}, ensure_ascii=False))
        return EXIT_NOT_FOUND

    lpath = pathlib.Path(job["log_path"])
    offset = max(0, int(args.offset or 0))
    max_bytes = max(1024, int(args.max_bytes or 262144))
    chunk_bytes = b""
    log_size = 0
    if lpath.exists():
        log_size = lpath.stat().st_size
        if offset < log_size:
            with lpath.open("rb") as fh:
                fh.seek(offset)
                chunk_bytes = fh.read(max_bytes)

    next_offset = offset + len(chunk_bytes)
    payload = {
        "found": True,
        "job": job,
        "offset": offset,
        "next_offset": next_offset,
        "log_size": log_size,
        "chunk": chunk_bytes.decode("utf-8", errors="replace"),
        "done": job.get("status") != "RUNNING" and next_offset >= log_size,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    job = resolve_job_ref(job_ref=args.job_ref, job_id=args.job_id)
    if not job:
        return EXIT_NOT_FOUND
    pid = job.get("pid")
    if job.get("status") == "RUNNING" and pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    job["status"] = "CANCELLED"
    job["ended_at"] = utc_now()
    atomic_write_json(JOBS_DIR / f"{job['job_id']}.json", job)
    print(json.dumps(job, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="sub", required=True)

    p_sub = sub.add_parser("submit")
    p_sub.add_argument("--command", required=True)
    p_sub.add_argument("--workdir", default="/workspace")
    p_sub.add_argument("--detach", action="store_true")
    p_sub.add_argument("--tag", default=None)
    p_sub.add_argument("--idempotency-key", default=None)
    p_sub.add_argument("--retry-failed", action="store_true")
    p_sub.add_argument("--env", action="append", default=[])

    p_q = sub.add_parser("queue")
    p_q.add_argument("--tag", default=None)

    p_logs = sub.add_parser("logs")
    p_logs.add_argument("--job-id", type=int, default=None)
    p_logs.add_argument("--job-ref", default=None)
    p_logs.add_argument("--tail", type=int, default=0)
    p_logs.add_argument("--status", action="store_true")

    p_poll = sub.add_parser("poll")
    p_poll.add_argument("--job-id", type=int, default=None)
    p_poll.add_argument("--job-ref", default=None)
    p_poll.add_argument("--offset", type=int, default=0)
    p_poll.add_argument("--max-bytes", type=int, default=262144)

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("--job-id", type=int, default=None)
    p_cancel.add_argument("--job-ref", default=None)

    ns = parser.parse_args()
    if ns.sub == "submit":
        return cmd_submit(ns)
    if ns.sub == "queue":
        return cmd_queue(ns)
    if ns.sub == "logs":
        return cmd_logs(ns)
    if ns.sub == "poll":
        return cmd_poll(ns)
    if ns.sub == "cancel":
        return cmd_cancel(ns)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def build_autostop_json(
    idle_minutes: int = 30,
    autodown: bool = False,
    wait_for: str = "jobs_and_ssh",
) -> str:
    if wait_for not in ("jobs_and_ssh", "jobs", "none"):
        raise ValueError(f"Unsupported wait_for mode: {wait_for!r}")
    return json.dumps(
        {
            "idle_minutes": idle_minutes,
            "autodown": autodown,
            "wait_for": wait_for,
        },
        indent=2,
    )


def render_startup_script(
    ssh_user: str = "gcolab",
    idle_minutes: int = 30,
    autodown: bool = False,
    wait_for: str = "jobs_and_ssh",
    has_gpu: bool = False,
) -> str:
    """Render GCE Linux startup-script installing /workspace, job_runner, and autostop systemd timer."""
    autostop_cfg = build_autostop_json(idle_minutes=idle_minutes, autodown=autodown, wait_for=wait_for)
    gpu_wait_block = ""
    if has_gpu:
        gpu_wait_block = """
# Wait up to 180s if Deep Learning VM first-boot NVIDIA driver activation is in progress
for _i in $(seq 1 36); do
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
"""

    return f"""#!/bin/bash
set -euo pipefail

mkdir -p /opt/gcolab/jobs /opt/gcolab/logs /opt/gcolab/locks /workspace
id -u {shlex.quote(ssh_user)} >/dev/null 2>&1 || useradd -m -s /bin/bash {shlex.quote(ssh_user)}
usermod -aG sudo {shlex.quote(ssh_user)} 2>/dev/null || true
echo "{ssh_user} ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/99-gcolab
chmod 0440 /etc/sudoers.d/99-gcolab
chown -R {shlex.quote(ssh_user)}:{shlex.quote(ssh_user)} /workspace /opt/gcolab
chmod 0777 /opt/gcolab/jobs /opt/gcolab/logs /opt/gcolab/locks

cat <<'EOF_DAEMON' >/opt/gcolab/daemon.py
{REMOTE_DAEMON_PY}
EOF_DAEMON

cat <<'EOF_RUNNER' >/opt/gcolab/job_runner.py
{REMOTE_JOB_RUNNER_PY}
EOF_RUNNER

cat <<'EOF_CFG' >/opt/gcolab/autostop.json
{autostop_cfg}
EOF_CFG

chmod 0755 /opt/gcolab/daemon.py /opt/gcolab/job_runner.py
chmod 0666 /opt/gcolab/autostop.json

cat <<'EOF_SVC' >/etc/systemd/system/gcolab-autostop.service
[Unit]
Description=gcolab Idle Autostop Check

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/gcolab/daemon.py
EOF_SVC

cat <<'EOF_TMR' >/etc/systemd/system/gcolab-autostop.timer
[Unit]
Description=Run gcolab Idle Autostop Check every 60s

[Timer]
OnBootSec=90s
OnUnitActiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF_TMR

systemctl daemon-reload || true
systemctl enable --now gcolab-autostop.timer || true
{gpu_wait_block}
date -u +"%Y-%m-%dT%H:%M:%SZ" >/opt/gcolab/READY
chmod 0644 /opt/gcolab/READY
"""
