"""Cross-platform OpenSSH transfers and IAP tunnels to Linux Compute Engine VMs."""

from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import hashlib
import io
import json
import os
import pathlib
import posixpath
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any, Iterator

from gcolab.auth import ATTRIBUTION_TOKEN, find_gcloud_binary
from gcolab.config import SessionRecord, StateStore

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".gcolab",
        ".ssh",
        ".gcloud",
        ".agent",
        ".agents",
        ".claude",
        ".codex",
        ".cursor",
        ".gemini",
        ".windsurf",
        "private",
        "secrets",
        ".secrets",
        "credentials",
        ".credentials",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        ".next",
    }
)

DEFAULT_ALLOWED_FILES = frozenset(
    {
        ".env.example",
        ".env.template",
    }
)

DEFAULT_EXCLUDED_PATTERNS = (
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    ".env",
    ".env.*",
    ".env-*",
    ".env_*",
    "*.env",
    "*.local",
    "*.local.*",
    "*.private",
    "*.private.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.ppk",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*_rsa",
    "*_dsa",
    "*_ecdsa",
    "*_ed25519",
    "gcolab_ed25519*",
    "google_compute_engine*",
    "application_default_credentials.json",
    "*credentials*.json",
    "*credential*.json",
    "client_secret*.json",
    "*client_secret*.json",
    "oauth*.json",
    "*oauth*.json",
    "*service-account*.json",
    "*service_account*.json",
    "key.json",
    "keys.json",
    "*-key.json",
    "*_key.json",
    "*-keys.json",
    "*_keys.json",
    "*token*.json",
    "*refresh_token*",
)

STREAM_CHUNK_BYTES = 65536


class TransportError(RuntimeError):
    """Raised on SSH, IAP tunnel, or file transfer failure."""


@dataclasses.dataclass
class TransferOptions:
    method: str = "auto"  # "auto" | "scp" | "tar" | "stream"
    connect_timeout: int = 20
    stall_timeout: int = 90
    max_duration: int = 3600
    retries: int = 2
    show_progress: bool = True


def _fmt_bytes(num_bytes: float) -> str:
    val = float(max(0.0, num_bytes))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if val < 1024.0 or unit == "GiB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} GiB"


def _fmt_eta(remaining_bytes: float, rate_bps: float) -> str:
    if rate_bps <= 1.0 or remaining_bytes <= 0:
        return "--"
    secs = int(remaining_bytes / rate_bps)
    if secs < 60:
        return f"{secs}s"
    mins, rem = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{rem:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h{mins:02d}m"


class _StageProgress:
    """Concise stderr stage & progress logger suitable for both terminals and agent logs."""

    def __init__(self, label: str, enabled: bool = True, interval_s: float = 2.0) -> None:
        self.label = label
        self.enabled = enabled
        self.interval_s = interval_s
        self.stage = "init"
        self._last_emit = 0.0

    def set_stage(self, stage: str, detail: str = "") -> None:
        self.stage = stage
        if not self.enabled:
            return
        suffix = f" ({detail})" if detail else ""
        print(f"[gcolab:transfer] {self.label} stage={stage}{suffix}", file=sys.stderr, flush=True)
        self._last_emit = time.monotonic()

    def update(self, done_bytes: int, total_bytes: int, start_ts: float, *, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < self.interval_s:
            return
        self._last_emit = now
        elapsed = max(0.001, now - start_ts)
        rate = done_bytes / elapsed
        pct = (100.0 * done_bytes / total_bytes) if total_bytes > 0 else 100.0
        eta = _fmt_eta(max(0, total_bytes - done_bytes), rate)
        print(
            f"[gcolab:transfer] {self.label} stage={self.stage} "
            f"{_fmt_bytes(done_bytes)}/{_fmt_bytes(total_bytes)} ({pct:.1f}%) "
            f"{_fmt_bytes(rate)}/s eta={eta}",
            file=sys.stderr,
            flush=True,
        )


def sha256_file(path: pathlib.Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(262144)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def session_host_key_alias(session: SessionRecord, host: str, port: int) -> str:
    if session.instance_name:
        return f"gcolab-{session.project}-{session.zone}-{session.instance_name}"
    return f"gcolab-{host}-{port}"


def find_ssh_binary(tool: str = "ssh") -> str:
    windows = sys.platform == "win32"
    exe_name = f"{tool}.exe" if windows else tool
    if windows:
        system_root = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
        system32_candidate = system_root / "System32" / "OpenSSH" / exe_name
        if system32_candidate.is_file():
            return str(system32_candidate)
    hit = shutil.which(exe_name) or shutil.which(tool)
    if hit:
        return hit
    if not windows:
        for directory in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"):
            candidate = pathlib.Path(directory) / tool
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    hint = (
        "Enable OpenSSH Client in Windows Optional Features."
        if windows else "Install OpenSSH (openssh-client on Debian/Ubuntu, openssh-clients on Fedora) and add it to PATH."
    )
    raise TransportError(f"OpenSSH tool '{exe_name}' not found. {hint}")


def ensure_ssh_keypair(key_path: pathlib.Path | None = None) -> tuple[pathlib.Path, str]:
    """Ensure dedicated Ed25519 SSH keypair exists; return (private_key_path, public_key_text)."""
    priv = (key_path or (pathlib.Path.home() / ".ssh" / "gcolab_ed25519")).expanduser().resolve()
    pub = pathlib.Path(f"{priv}.pub")
    priv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        if priv.parent == (pathlib.Path.home() / ".ssh").resolve():
            priv.parent.chmod(0o700)
        if priv.exists():
            priv.chmod(0o600)
    if not priv.exists():
        keygen = find_ssh_binary("ssh-keygen")
        res = subprocess.run(
            [keygen, "-t", "ed25519", "-N", "", "-C", "gcolab", "-f", str(priv)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if res.returncode != 0 or not pub.exists():
            raise TransportError(f"ssh-keygen failed: {res.stderr or res.stdout}")
    elif not pub.exists():
        res = subprocess.run(
            [find_ssh_binary("ssh-keygen"), "-y", "-P", "", "-f", str(priv)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if res.returncode != 0:
            raise TransportError(f"Could not derive SSH public key: {res.stderr or res.stdout}")
        pub.write_text(res.stdout.strip() + "\n", encoding="utf-8")
    if os.name != "nt":
        priv.chmod(0o600)
        pub.chmod(0o644)
    return priv, pub.read_text(encoding="utf-8").strip()


def _pick_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_tcp_port(host: str, port: int, timeout: float = 20.0, proc: subprocess.Popen | None = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            out = ""
            if proc.stderr:
                out = proc.stderr.read().decode("utf-8", errors="replace")
            raise TransportError(f"gcloud IAP tunnel exited early (code {proc.returncode}): {out.strip()}")
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.35)
    raise TransportError(f"Timed out waiting for local IAP TCP bridge on {host}:{port}")


@contextlib.contextmanager
def open_ssh_endpoint(session: SessionRecord) -> Iterator[tuple[str, int]]:
    """Yield (host, port) for SSH — direct IP:22 or localhost:<port> via gcloud IAP tunnel."""
    use_iap = session.ssh_mode == "iap" or not session.external_ip
    if not use_iap:
        assert session.external_ip is not None
        yield (session.external_ip, 22)
        return

    gcloud_bin = find_gcloud_binary()
    if not gcloud_bin:
        raise TransportError(
            f"Session {session.name!r} uses IAP tunnel mode (or has no external IP), "
            "which requires Google Cloud CLI (gcloud) installed on the local host."
        )

    local_port = _pick_free_local_port()
    env = os.environ.copy()
    if os.environ.get("GCOLAB_DISABLE_ATTRIBUTION") != "1":
        env["CLOUDSDK_METRICS_REQUEST_ATTRIBUTION"] = ATTRIBUTION_TOKEN

    cmd = [
        gcloud_bin,
        "compute",
        "start-iap-tunnel",
        session.instance_name,
        "22",
        f"--local-host-port=127.0.0.1:{local_port}",
        f"--project={session.project}",
        f"--zone={session.zone}",
        "--quiet",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        _wait_tcp_port("127.0.0.1", local_port, timeout=25.0, proc=proc)
        yield ("127.0.0.1", local_port)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _common_openssh_options(
    store: StateStore,
    host_key_alias: str,
    connect_timeout: int = 20,
) -> list[str]:
    store.ensure_dirs()
    known_hosts = str(store.known_hosts_path).replace("\\", "/")
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f'UserKnownHostsFile="{known_hosts}"',
        "-o",
        f"HostKeyAlias={host_key_alias}",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "ConnectionAttempts=2",
        "-o",
        "ServerAliveInterval=20",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "LogLevel=ERROR",
    ]


def _base_ssh_args(
    store: StateStore,
    priv_key: pathlib.Path,
    host: str,
    port: int,
    ssh_user: str,
    connect_timeout: int = 20,
    host_key_alias: str | None = None,
) -> list[str]:
    ssh_bin = find_ssh_binary("ssh")
    alias = host_key_alias or f"gcolab-{host}-{port}"
    return [
        ssh_bin,
        "-i",
        str(priv_key),
        "-p",
        str(port),
        *_common_openssh_options(store, alias, connect_timeout=connect_timeout),
        f"{ssh_user}@{host}",
    ]


def _base_scp_args(
    store: StateStore,
    priv_key: pathlib.Path,
    port: int,
    connect_timeout: int = 20,
    host_key_alias: str | None = None,
) -> list[str]:
    scp_bin = find_ssh_binary("scp")
    alias = host_key_alias or f"gcolab-scp-{port}"
    return [
        scp_bin,
        "-q",
        "-i",
        str(priv_key),
        "-P",
        str(port),
        *_common_openssh_options(store, alias, connect_timeout=connect_timeout),
    ]


def ssh_run_capture(
    store: StateStore,
    session: SessionRecord,
    remote_cmd: str,
    *,
    stdin_bytes: bytes | None = None,
    timeout: int = 120,
    connect_timeout: int = 20,
) -> subprocess.CompletedProcess[bytes]:
    priv_key, _ = ensure_ssh_keypair()
    with open_ssh_endpoint(session) as (host, port):
        alias = session_host_key_alias(session, host, port)
        cmd = [
            *_base_ssh_args(
                store,
                priv_key,
                host,
                port,
                session.ssh_user,
                connect_timeout=connect_timeout,
                host_key_alias=alias,
            ),
            remote_cmd,
        ]
        return subprocess.run(
            cmd,
            input=stdin_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )


def ssh_run_stream(
    store: StateStore,
    session: SessionRecord,
    remote_cmd: str,
    *,
    stdin_bytes: bytes | None = None,
    connect_timeout: int = 20,
) -> int:
    priv_key, _ = ensure_ssh_keypair()
    with open_ssh_endpoint(session) as (host, port):
        alias = session_host_key_alias(session, host, port)
        cmd = [
            *_base_ssh_args(
                store,
                priv_key,
                host,
                port,
                session.ssh_user,
                connect_timeout=connect_timeout,
                host_key_alias=alias,
            ),
            remote_cmd,
        ]
        if stdin_bytes is not None:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            assert proc.stdin is not None
            proc.stdin.write(stdin_bytes)
            proc.stdin.close()
            return proc.wait()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        return proc.wait()


def wait_for_vm_ready(
    store: StateStore,
    session: SessionRecord,
    timeout_s: int = 360,
) -> dict[str, str]:
    """Wait until SSH connects and /opt/gcolab/READY exists on the GCE VM."""
    deadline = time.time() + timeout_s
    last_err = ""
    probe = "test -f /opt/gcolab/READY && cat /opt/gcolab/READY"
    while time.time() < deadline:
        try:
            res = ssh_run_capture(store, session, probe, timeout=25, connect_timeout=15)
            if res.returncode == 0:
                ready_ts = res.stdout.decode("utf-8", errors="replace").strip()
                return {"ready_at": ready_ts}
            last_err = res.stderr.decode("utf-8", errors="replace").strip()
        except Exception as exc:
            last_err = str(exc)
        time.sleep(5.0)
    raise TransportError(
        f"Timed out waiting for VM {session.instance_name!r} SSH/startup readiness ({timeout_s}s). "
        f"Last error: {last_err}"
    )


def load_gitignore_patterns(root: pathlib.Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.exists():
        return []
    patterns: list[str] = []
    for raw in gi.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line)
    return patterns


def _is_ignored(rel_posix: str, name: str, is_dir: bool, extra_patterns: list[str]) -> bool:
    if not is_dir and name in DEFAULT_ALLOWED_FILES:
        return False
    if is_dir and name in DEFAULT_EXCLUDED_DIRS:
        return True
    for pat in DEFAULT_EXCLUDED_PATTERNS:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_posix, pat):
            return True
    for raw_pat in extra_patterns:
        dir_only = raw_pat.endswith("/")
        pat = raw_pat.rstrip("/")
        if not pat:
            continue
        if dir_only and not is_dir:
            continue
        if pat.startswith("/"):
            if fnmatch.fnmatch(f"/{rel_posix}", pat):
                return True
        else:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_posix, pat):
                return True
            if "/" not in pat and any(fnmatch.fnmatch(seg, pat) for seg in rel_posix.split("/")):
                return True
    return False


def _remote_file_mode(path: pathlib.Path) -> int:
    """Preserve POSIX permissions and provide executable shell scripts on every host."""
    if path.suffix.lower() in (".sh", ".bash"):
        return 0o755
    if os.name != "nt":
        return stat.S_IMODE(path.stat().st_mode) & 0o777
    return 0o644


def _populate_tar(
    tf: tarfile.TarFile,
    src: pathlib.Path,
    *,
    respect_gitignore: bool = True,
    single_file_arcname: str | None = None,
) -> int:
    file_count = 0
    if src.is_file():
        arc = (single_file_arcname or src.name).lstrip("/")
        if src.suffix.lower() in (".sh", ".bash"):
            data = src.read_bytes().replace(b"\r\n", b"\n")
            ti = tarfile.TarInfo(name=arc)
            ti.size = len(data)
            ti.mtime = int(src.stat().st_mtime)
            ti.mode = 0o755
            tf.addfile(ti, io.BytesIO(data))
        else:
            st = src.stat()
            ti = tarfile.TarInfo(name=arc)
            ti.size = st.st_size
            ti.mtime = int(st.st_mtime)
            ti.mode = _remote_file_mode(src)
            with src.open("rb") as fh:
                tf.addfile(ti, fh)
        return 1

    gi_patterns = load_gitignore_patterns(src) if respect_gitignore else []
    for cur_root, dirs, files in os.walk(src):
        cur_p = pathlib.Path(cur_root)
        rel_dir = cur_p.relative_to(src)
        kept_dirs: list[str] = []
        for d in sorted(dirs):
            d_rel = (rel_dir / d).as_posix() if rel_dir != pathlib.Path(".") else d
            if not _is_ignored(d_rel, d, True, gi_patterns):
                kept_dirs.append(d)
        dirs[:] = kept_dirs

        for fname in sorted(files):
            fpath = cur_p / fname
            rel_posix = (rel_dir / fname).as_posix() if rel_dir != pathlib.Path(".") else fname
            if _is_ignored(rel_posix, fname, False, gi_patterns):
                continue
            try:
                is_shell = fpath.suffix.lower() in (".sh", ".bash")
                if is_shell:
                    data = fpath.read_bytes().replace(b"\r\n", b"\n")
                    ti = tarfile.TarInfo(name=rel_posix)
                    ti.size = len(data)
                    ti.mtime = int(fpath.stat().st_mtime)
                    ti.mode = 0o755
                    tf.addfile(ti, io.BytesIO(data))
                else:
                    st = fpath.stat()
                    ti = tarfile.TarInfo(name=rel_posix)
                    ti.size = st.st_size
                    ti.mtime = int(st.st_mtime)
                    ti.mode = _remote_file_mode(fpath)
                    with fpath.open("rb") as fh:
                        tf.addfile(ti, fh)
                file_count += 1
            except OSError:
                continue
    return file_count


def build_tar_gz_bytes(
    source_path: pathlib.Path,
    *,
    respect_gitignore: bool = True,
    single_file_arcname: str | None = None,
) -> tuple[bytes, int]:
    """Pack a local directory or file into an in-memory POSIX .tar.gz buffer."""
    src = source_path.expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Local path does not exist: {src}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        count = _populate_tar(
            tf,
            src,
            respect_gitignore=respect_gitignore,
            single_file_arcname=single_file_arcname,
        )
    return buf.getvalue(), count


def build_tar_gz_file(
    source_path: pathlib.Path,
    dest_tar_gz: pathlib.Path,
    *,
    respect_gitignore: bool = True,
    single_file_arcname: str | None = None,
) -> tuple[int, int, str]:
    """Stream a local path into dest_tar_gz on disk; return (file_count, byte_size, sha256)."""
    src = source_path.expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Local path does not exist: {src}")
    with tarfile.open(name=str(dest_tar_gz), mode="w:gz") as tf:
        count = _populate_tar(
            tf,
            src,
            respect_gitignore=respect_gitignore,
            single_file_arcname=single_file_arcname,
        )
    digest, size = sha256_file(dest_tar_gz)
    return count, size, digest


def _stream_send_file(
    ssh_cmd: list[str],
    local_file: pathlib.Path,
    *,
    start_offset: int,
    total_bytes: int,
    options: TransferOptions,
    progress: _StageProgress,
) -> None:
    """Stream local_file[start_offset:] to ssh_cmd stdin with stall & max_duration watchdogs."""
    state: dict[str, Any] = {
        "done": start_offset,
        "last_ts": time.monotonic(),
        "err": None,
        "stderr": b"",
    }
    start_ts = time.monotonic()
    proc = subprocess.Popen(
        ssh_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def _writer() -> None:
        try:
            assert proc.stdin is not None
            with local_file.open("rb") as fh:
                if start_offset > 0:
                    fh.seek(start_offset)
                while True:
                    chunk = fh.read(STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                    state["done"] += len(chunk)
                    state["last_ts"] = time.monotonic()
        except Exception as exc:
            state["err"] = exc
        finally:
            if proc.stdin:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

    def _stderr_reader() -> None:
        if proc.stderr:
            with contextlib.suppress(Exception):
                state["stderr"] = proc.stderr.read()

    tw = threading.Thread(target=_writer, daemon=True)
    te = threading.Thread(target=_stderr_reader, daemon=True)
    tw.start()
    te.start()

    try:
        while proc.poll() is None:
            now = time.monotonic()
            if (now - start_ts) > options.max_duration:
                proc.kill()
                raise TransportError(
                    f"Upload exceeded max_duration ({options.max_duration}s) at stage={progress.stage} "
                    f"({state['done']}/{total_bytes} bytes)."
                )
            if (now - state["last_ts"]) > options.stall_timeout:
                proc.kill()
                raise TransportError(
                    f"Upload stalled (no progress for {options.stall_timeout}s) at stage={progress.stage} "
                    f"({state['done']}/{total_bytes} bytes)."
                )
            progress.update(state["done"], total_bytes, start_ts)
            time.sleep(0.25)

        tw.join(timeout=5.0)
        te.join(timeout=5.0)
        progress.update(state["done"], total_bytes, start_ts, force=True)
        if proc.returncode != 0:
            err_txt = state["stderr"].decode("utf-8", errors="replace").strip()
            raise TransportError(
                f"SSH stream upload failed (rc={proc.returncode}, stage={progress.stage}, "
                f"sent={state['done']}/{total_bytes}): {err_txt}"
            )
        if state["err"] is not None:
            raise TransportError(f"Stream writer error at stage={progress.stage}: {state['err']}")
    finally:
        if proc.poll() is None:
            proc.kill()


def _stream_receive_file(
    ssh_cmd: list[str],
    local_part: pathlib.Path,
    *,
    start_offset: int,
    total_bytes: int,
    options: TransferOptions,
    progress: _StageProgress,
) -> None:
    """Stream ssh_cmd stdout into local_part (appending from start_offset) with stall watchdog."""
    state: dict[str, Any] = {
        "done": start_offset,
        "last_ts": time.monotonic(),
        "err": None,
        "stderr": b"",
    }
    start_ts = time.monotonic()
    proc = subprocess.Popen(
        ssh_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def _reader() -> None:
        mode = "ab" if start_offset > 0 else "wb"
        try:
            assert proc.stdout is not None
            with local_part.open(mode) as fh:
                while True:
                    chunk = proc.stdout.read(STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    fh.write(chunk)
                    state["done"] += len(chunk)
                    state["last_ts"] = time.monotonic()
        except Exception as exc:
            state["err"] = exc

    def _stderr_reader() -> None:
        if proc.stderr:
            with contextlib.suppress(Exception):
                state["stderr"] = proc.stderr.read()

    tr = threading.Thread(target=_reader, daemon=True)
    te = threading.Thread(target=_stderr_reader, daemon=True)
    tr.start()
    te.start()

    try:
        while proc.poll() is None:
            now = time.monotonic()
            if (now - start_ts) > options.max_duration:
                proc.kill()
                raise TransportError(
                    f"Download exceeded max_duration ({options.max_duration}s) at stage={progress.stage} "
                    f"({state['done']}/{total_bytes} bytes)."
                )
            if (now - state["last_ts"]) > options.stall_timeout:
                proc.kill()
                raise TransportError(
                    f"Download stalled (no progress for {options.stall_timeout}s) at stage={progress.stage} "
                    f"({state['done']}/{total_bytes} bytes)."
                )
            progress.update(state["done"], total_bytes, start_ts)
            time.sleep(0.25)

        tr.join(timeout=5.0)
        te.join(timeout=5.0)
        progress.update(state["done"], total_bytes, start_ts, force=True)
        if proc.returncode != 0:
            err_txt = state["stderr"].decode("utf-8", errors="replace").strip()
            raise TransportError(
                f"SSH stream download failed (rc={proc.returncode}, stage={progress.stage}, "
                f"received={state['done']}/{total_bytes}): {err_txt}"
            )
        if state["err"] is not None:
            raise TransportError(f"Stream reader error at stage={progress.stage}: {state['err']}")
    finally:
        if proc.poll() is None:
            proc.kill()


def _run_scp_upload(
    scp_base: list[str],
    local_file: pathlib.Path,
    ssh_user: str,
    host: str,
    remote_part_path: str,
    *,
    total_bytes: int,
    options: TransferOptions,
    progress: _StageProgress,
) -> None:
    cmd = [*scp_base, str(local_file), f"{ssh_user}@{host}:{remote_part_path}"]
    start_ts = time.monotonic()
    # Allow proportional wall time for SCP while enforcing max_duration and heartbeat visibility
    effective_cap = min(
        options.max_duration,
        max(options.connect_timeout + options.stall_timeout, int(total_bytes / 32768) + 60),
    )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    last_log = start_ts
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if (now - start_ts) > effective_cap:
                proc.kill()
                raise TransportError(
                    f"SCP upload timed out after {int(now - start_ts)}s (stage={progress.stage}, "
                    f"size={_fmt_bytes(total_bytes)})."
                )
            if options.show_progress and (now - last_log) >= 5.0:
                last_log = now
                print(
                    f"[gcolab:transfer] {progress.label} stage=send(scp) "
                    f"elapsed={int(now - start_ts)}s total={_fmt_bytes(total_bytes)}",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(0.25)
        _, stderr_b = proc.communicate(timeout=5)
        if proc.returncode != 0:
            err_txt = (stderr_b or b"").decode("utf-8", errors="replace").strip()
            raise TransportError(f"SCP upload failed (rc={proc.returncode}): {err_txt}")
    finally:
        if proc.poll() is None:
            proc.kill()


def _run_scp_download(
    scp_base: list[str],
    ssh_user: str,
    host: str,
    remote_file_path: str,
    local_part: pathlib.Path,
    *,
    total_bytes: int,
    options: TransferOptions,
    progress: _StageProgress,
) -> None:
    local_part.unlink(missing_ok=True)
    cmd = [*scp_base, f"{ssh_user}@{host}:{remote_file_path}", str(local_part)]
    start_ts = time.monotonic()
    last_size = 0
    last_growth_ts = start_ts
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        while proc.poll() is None:
            now = time.monotonic()
            cur_size = local_part.stat().st_size if local_part.exists() else 0
            if cur_size > last_size:
                last_size = cur_size
                last_growth_ts = now
            if (now - start_ts) > options.max_duration:
                proc.kill()
                raise TransportError(
                    f"SCP download exceeded max_duration ({options.max_duration}s) "
                    f"at {cur_size}/{total_bytes} bytes."
                )
            if (now - last_growth_ts) > options.stall_timeout:
                proc.kill()
                raise TransportError(
                    f"SCP download stalled (no file growth for {options.stall_timeout}s) "
                    f"at {cur_size}/{total_bytes} bytes."
                )
            progress.update(cur_size, total_bytes, start_ts)
            time.sleep(0.25)
        _, stderr_b = proc.communicate(timeout=5)
        progress.update(
            local_part.stat().st_size if local_part.exists() else 0,
            total_bytes,
            start_ts,
            force=True,
        )
        if proc.returncode != 0:
            err_txt = (stderr_b or b"").decode("utf-8", errors="replace").strip()
            raise TransportError(f"SCP download failed (rc={proc.returncode}): {err_txt}")
    finally:
        if proc.poll() is None:
            proc.kill()


def _query_remote_part_size(ssh_base: list[str], remote_part: str, remote_dir: str, timeout: int = 25) -> int:
    q_dir = shlex.quote(remote_dir)
    q_part = shlex.quote(remote_part)
    cmd = [*ssh_base, f"mkdir -p {q_dir} && if [ -f {q_part} ]; then stat -c %s {q_part}; else echo 0; fi"]
    res = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise TransportError(f"SSH preflight failed preparing {remote_dir}: {err}")
    out = res.stdout.decode("utf-8", errors="replace").strip().splitlines()
    tail = out[-1].strip() if out else "0"
    return int(tail) if tail.isdigit() else 0


def _transfer_file_to_remote_staged(
    store: StateStore,
    session: SessionRecord,
    local_payload: pathlib.Path,
    *,
    remote_dir: str,
    remote_part: str,
    expected_sha256: str,
    total_bytes: int,
    prefer_scp_first: bool,
    options: TransferOptions,
    progress: _StageProgress,
) -> str:
    """Upload local_payload to remote_part with retries, auto SCP/stream fallback, and remote SHA-256 check."""
    priv_key, _ = ensure_ssh_keypair()
    total_attempts = max(1, int(options.retries) + 1)
    last_exc: Exception | None = None

    with open_ssh_endpoint(session) as (host, port):
        alias = session_host_key_alias(session, host, port)
        ssh_base = _base_ssh_args(
            store,
            priv_key,
            host,
            port,
            session.ssh_user,
            connect_timeout=options.connect_timeout,
            host_key_alias=alias,
        )

        scp_base: list[str] | None = None
        if options.method in ("auto", "scp"):
            try:
                scp_base = _base_scp_args(
                    store,
                    priv_key,
                    port,
                    connect_timeout=options.connect_timeout,
                    host_key_alias=alias,
                )
            except TransportError:
                if options.method == "scp":
                    raise
                scp_base = None

        for attempt in range(1, total_attempts + 1):
            try:
                progress.set_stage("connect", f"attempt={attempt}/{total_attempts}")
                existing_offset = _query_remote_part_size(
                    ssh_base,
                    remote_part,
                    remote_dir,
                    timeout=max(25, options.connect_timeout + 10),
                )
                if existing_offset > total_bytes:
                    existing_offset = 0
                    subprocess.run([*ssh_base, f"rm -f {shlex.quote(remote_part)}"], capture_output=True, timeout=20, check=False)

                use_scp = False
                if options.method == "scp":
                    use_scp = True
                elif options.method == "auto" and scp_base is not None:
                    # Use SCP on attempt 1 when starting from byte 0; switch to resumable stream on retry or partial resume
                    if prefer_scp_first and attempt == 1 and existing_offset == 0:
                        use_scp = True
                    elif not prefer_scp_first and attempt > 1 and existing_offset == 0:
                        use_scp = True

                chosen_method = "scp" if use_scp else "stream"
                if existing_offset == total_bytes and total_bytes > 0:
                    progress.set_stage("verify", "reusing complete staged payload")
                elif use_scp and scp_base is not None:
                    progress.set_stage("send", f"method=scp attempt={attempt}/{total_attempts} size={_fmt_bytes(total_bytes)}")
                    _run_scp_upload(
                        scp_base,
                        local_payload,
                        session.ssh_user,
                        host,
                        remote_part,
                        total_bytes=total_bytes,
                        options=options,
                        progress=progress,
                    )
                else:
                    mode_op = ">>" if existing_offset > 0 else ">"
                    resume_note = f" resume_from={_fmt_bytes(existing_offset)}" if existing_offset > 0 else ""
                    progress.set_stage(
                        "send",
                        f"method=stream attempt={attempt}/{total_attempts}{resume_note}",
                    )
                    q_part = shlex.quote(remote_part)
                    recv_sh = (
                        f"head -c {existing_offset} {q_part} > {q_part}.trunc && mv -f {q_part}.trunc {q_part} && cat >> {q_part}"
                        if existing_offset > 0
                        else f"cat > {q_part}"
                    )
                    _stream_send_file(
                        [*ssh_base, recv_sh],
                        local_payload,
                        start_offset=existing_offset,
                        total_bytes=total_bytes,
                        options=options,
                        progress=progress,
                    )

                progress.set_stage("verify", f"sha256={expected_sha256[:12]}...")
                q_part = shlex.quote(remote_part)
                verify_sh = (
                    f"sz=$(stat -c %s {q_part}) && "
                    f"sh=$(sha256sum {q_part} | awk '{{print $1}}') && "
                    f"echo \"$sz $sh\""
                )
                vres = subprocess.run([*ssh_base, verify_sh], capture_output=True, timeout=60, check=False)
                if vres.returncode != 0:
                    raise TransportError(
                        f"Remote digest check failed: {vres.stderr.decode('utf-8', errors='replace').strip()}"
                    )
                parts = vres.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1].split()
                r_size = int(parts[0]) if parts and parts[0].isdigit() else -1
                r_sha = parts[1] if len(parts) > 1 else ""
                if r_size != total_bytes or r_sha != expected_sha256:
                    subprocess.run([*ssh_base, f"rm -f {q_part}"], capture_output=True, timeout=20, check=False)
                    raise TransportError(
                        f"SHA-256/size mismatch on {remote_part}: expected {total_bytes}/{expected_sha256[:12]}, "
                        f"got {r_size}/{r_sha[:12]}"
                    )
                return chosen_method
            except Exception as exc:
                last_exc = exc
                if attempt < total_attempts:
                    backoff = min(8.0, 2.0 * attempt)
                    print(
                        f"[gcolab:transfer] attempt {attempt}/{total_attempts} failed ({exc}); retrying in {backoff:.0f}s...",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(backoff)

    raise TransportError(f"Upload failed after {total_attempts} attempt(s): {last_exc}")


def sync_workdir(
    store: StateStore,
    session: SessionRecord,
    local_dir: str | pathlib.Path,
    remote_dir: str = "/workspace",
    *,
    clean: bool = False,
    options: TransferOptions | None = None,
) -> dict[str, Any]:
    opts = options or TransferOptions()
    t0 = time.monotonic()
    src = pathlib.Path(local_dir).expanduser().resolve()
    progress = _StageProgress(f"sync:{src.name}->{remote_dir}", enabled=opts.show_progress)

    with tempfile.TemporaryDirectory(prefix="gcolab-sync-") as tmpd:
        tar_path = pathlib.Path(tmpd) / "workdir.tar.gz"
        progress.set_stage("pack", str(src))
        count, archive_bytes, archive_sha = build_tar_gz_file(src, tar_path, respect_gitignore=True)
        remote_part = f"/tmp/.gcolab-sync-{archive_sha[:12]}.tar.gz.part"

        used_method = _transfer_file_to_remote_staged(
            store,
            session,
            tar_path,
            remote_dir="/tmp",
            remote_part=remote_part,
            expected_sha256=archive_sha,
            total_bytes=archive_bytes,
            prefer_scp_first=(opts.method == "scp"),
            options=opts,
            progress=progress,
        )

    progress.set_stage("commit", f"extracting {count} file(s) -> {remote_dir}")
    q_remote = shlex.quote(remote_dir)
    q_part = shlex.quote(remote_part)
    clean_prefix = f"rm -rf {q_remote} && " if clean else ""
    extract_cmd = (
        f"{clean_prefix}mkdir -p {q_remote} && "
        f"tar -xzf {q_part} -C {q_remote} && "
        f"rm -f {q_part}"
    )
    res = ssh_run_capture(
        store,
        session,
        extract_cmd,
        timeout=max(120, min(opts.max_duration, 900)),
        connect_timeout=opts.connect_timeout,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise TransportError(f"Failed extracting synced archive into {remote_dir}: {err}")

    elapsed = round(time.monotonic() - t0, 2)
    return {
        "files_synced": count,
        "bytes_sent": archive_bytes,
        "bytes_transferred": archive_bytes,
        "sha256": archive_sha,
        "verified": True,
        "transport": used_method,
        "elapsed_s": elapsed,
        "remote_dir": remote_dir,
    }


def upload_path(
    store: StateStore,
    session: SessionRecord,
    local_path: str | pathlib.Path,
    remote_path: str = "/workspace",
    *,
    options: TransferOptions | None = None,
) -> dict[str, Any]:
    opts = options or TransferOptions()
    src = pathlib.Path(local_path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Local path does not exist: {src}")
    if src.is_dir():
        return sync_workdir(store, session, src, remote_dir=remote_path, clean=False, options=opts)

    t0 = time.monotonic()
    if remote_path.endswith("/") or remote_path in ("/workspace", "~"):
        target_dir = remote_path.rstrip("/") or "/"
        target_name = src.name
    else:
        target_dir = posixpath.dirname(remote_path) or "/workspace"
        target_name = posixpath.basename(remote_path) or src.name
    final_remote = f"{target_dir.rstrip('/')}/{target_name}" if target_dir != "/" else f"/{target_name}"
    progress = _StageProgress(f"upload:{src.name}->{final_remote}", enabled=opts.show_progress)

    with tempfile.TemporaryDirectory(prefix="gcolab-up-") as tmpd:
        progress.set_stage("pack", src.name)
        is_shell = src.suffix.lower() in (".sh", ".bash")
        payload_path = src
        if is_shell:
            raw_bytes = src.read_bytes()
            if b"\r\n" in raw_bytes:
                payload_path = pathlib.Path(tmpd) / src.name
                payload_path.write_bytes(raw_bytes.replace(b"\r\n", b"\n"))

        file_sha, file_size = sha256_file(payload_path)
        stage_id = hashlib.sha256(f"{final_remote}:{file_sha}".encode("utf-8")).hexdigest()[:12]
        remote_part = f"{target_dir.rstrip('/')}/.gcolab-stage-{stage_id}.part"
        mode_octal = f"{_remote_file_mode(src):04o}"

        used_method = _transfer_file_to_remote_staged(
            store,
            session,
            payload_path,
            remote_dir=target_dir,
            remote_part=remote_part,
            expected_sha256=file_sha,
            total_bytes=file_size,
            prefer_scp_first=(opts.method in ("auto", "scp")),
            options=opts,
            progress=progress,
        )

    progress.set_stage("commit", final_remote)
    q_part = shlex.quote(remote_part)
    q_final = shlex.quote(final_remote)
    commit_cmd = f"chmod {mode_octal} {q_part} && mv -f {q_part} {q_final}"
    res = ssh_run_capture(store, session, commit_cmd, timeout=30, connect_timeout=opts.connect_timeout)
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise TransportError(f"Failed atomic move to {final_remote}: {err}")

    elapsed = round(time.monotonic() - t0, 2)
    return {
        "files_synced": 1,
        "bytes_sent": file_size,
        "bytes_transferred": file_size,
        "sha256": file_sha,
        "verified": True,
        "transport": used_method,
        "elapsed_s": elapsed,
        "remote_path": final_remote,
    }


def download_path(
    store: StateStore,
    session: SessionRecord,
    remote_path: str,
    local_dest: str | pathlib.Path = ".",
    *,
    options: TransferOptions | None = None,
) -> dict[str, Any]:
    opts = options or TransferOptions()
    t0 = time.monotonic()
    r_norm = remote_path.rstrip("/")
    r_base = posixpath.basename(r_norm)
    if not r_base:
        raise ValueError(f"Invalid remote_path for download: {remote_path!r}")

    progress = _StageProgress(f"download:{remote_path}", enabled=opts.show_progress)
    progress.set_stage("remote_pack", remote_path)

    token = secrets.token_hex(5)
    remote_tmp_tar = f"/tmp/.gcolab-dl-{token}.tar.gz"
    probe_py = (
        "import hashlib, json, os, pathlib, sys, tarfile\n"
        f"p = pathlib.Path({json.dumps(r_norm)}).expanduser()\n"
        "if not p.exists():\n"
        "    print(json.dumps({'exists': False}))\n"
        "    sys.exit(0)\n"
        "def sha(fp):\n"
        "    h = hashlib.sha256()\n"
        "    sz = 0\n"
        "    with open(fp, 'rb') as f:\n"
        "        while True:\n"
        "            b = f.read(262144)\n"
        "            if not b: break\n"
        "            h.update(b); sz += len(b)\n"
        "    return h.hexdigest(), sz\n"
        "if p.is_file():\n"
        "    dg, sz = sha(p)\n"
        "    print(json.dumps({'exists': True, 'is_file': True, 'transfer_path': str(p), 'size': sz, 'sha256': dg, 'file_count': 1}))\n"
        "else:\n"
        f"    arc = pathlib.Path({json.dumps(remote_tmp_tar)})\n"
        "    cnt = 0\n"
        "    with tarfile.open(str(arc), 'w:gz') as tf:\n"
        "        tf.add(str(p), arcname=p.name)\n"
        "    with tarfile.open(str(arc), 'r:gz') as tf:\n"
        "        cnt = sum(1 for m in tf.getmembers() if m.isfile())\n"
        "    dg, sz = sha(arc)\n"
        "    print(json.dumps({'exists': True, 'is_file': False, 'transfer_path': str(arc), 'size': sz, 'sha256': dg, 'file_count': cnt}))\n"
    )
    pres = ssh_run_capture(
        store,
        session,
        f"python3 -c {shlex.quote(probe_py)}",
        timeout=max(120, min(opts.max_duration, 900)),
        connect_timeout=opts.connect_timeout,
    )
    if pres.returncode != 0:
        err = pres.stderr.decode("utf-8", errors="replace").strip()
        raise TransportError(f"Failed inspecting/packing remote path {remote_path!r}: {err}")

    try:
        meta = json.loads(pres.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1])
    except Exception as exc:
        raise TransportError(f"Malformed metadata response for {remote_path!r}: {exc}") from exc

    if not meta.get("exists"):
        raise TransportError(f"Remote path does not exist on {session.name}: {remote_path}")

    is_single_file = bool(meta["is_file"])
    remote_transfer_path = str(meta["transfer_path"])
    expected_size = int(meta["size"])
    expected_sha = str(meta["sha256"])
    file_count = int(meta["file_count"])

    dest_raw = str(local_dest)
    dest = pathlib.Path(local_dest).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = dest.parent if is_single_file else dest
    stage_dir.mkdir(parents=True, exist_ok=True)
    local_part = stage_dir / f".gcolab-dl-{expected_sha[:12]}.part"

    priv_key, _ = ensure_ssh_keypair()
    total_attempts = max(1, int(opts.retries) + 1)
    used_method = "stream"
    last_exc: Exception | None = None

    try:
        with open_ssh_endpoint(session) as (host, port):
            alias = session_host_key_alias(session, host, port)
            ssh_base = _base_ssh_args(
                store,
                priv_key,
                host,
                port,
                session.ssh_user,
                connect_timeout=opts.connect_timeout,
                host_key_alias=alias,
            )
            scp_base: list[str] | None = None
            if opts.method in ("auto", "scp"):
                try:
                    scp_base = _base_scp_args(
                        store,
                        priv_key,
                        port,
                        connect_timeout=opts.connect_timeout,
                        host_key_alias=alias,
                    )
                except TransportError:
                    if opts.method == "scp":
                        raise
                    scp_base = None

            for attempt in range(1, total_attempts + 1):
                try:
                    cur_offset = local_part.stat().st_size if local_part.exists() else 0
                    if cur_offset > expected_size:
                        local_part.unlink(missing_ok=True)
                        cur_offset = 0

                    use_scp = False
                    if opts.method == "scp":
                        use_scp = True
                    elif opts.method == "auto" and scp_base is not None and attempt == 1 and cur_offset == 0:
                        use_scp = True

                    used_method = "scp" if use_scp else "stream"
                    if cur_offset == expected_size and expected_size > 0:
                        progress.set_stage("verify", "checking existing partial file")
                    elif use_scp and scp_base is not None:
                        progress.set_stage(
                            "receive",
                            f"method=scp attempt={attempt}/{total_attempts} size={_fmt_bytes(expected_size)}",
                        )
                        _run_scp_download(
                            scp_base,
                            session.ssh_user,
                            host,
                            remote_transfer_path,
                            local_part,
                            total_bytes=expected_size,
                            options=opts,
                            progress=progress,
                        )
                    else:
                        resume_note = f" resume_from={_fmt_bytes(cur_offset)}" if cur_offset > 0 else ""
                        progress.set_stage(
                            "receive",
                            f"method=stream attempt={attempt}/{total_attempts}{resume_note}",
                        )
                        q_rpath = shlex.quote(remote_transfer_path)
                        cat_cmd = (
                            f"tail -c +{cur_offset + 1} {q_rpath}"
                            if cur_offset > 0
                            else f"cat {q_rpath}"
                        )
                        _stream_receive_file(
                            [*ssh_base, cat_cmd],
                            local_part,
                            start_offset=cur_offset,
                            total_bytes=expected_size,
                            options=opts,
                            progress=progress,
                        )

                    progress.set_stage("verify", f"sha256={expected_sha[:12]}...")
                    got_sha, got_size = sha256_file(local_part)
                    if got_size != expected_size or got_sha != expected_sha:
                        local_part.unlink(missing_ok=True)
                        raise TransportError(
                            f"Downloaded digest mismatch for {remote_path}: "
                            f"expected {expected_size}/{expected_sha[:12]}, got {got_size}/{got_sha[:12]}"
                        )
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < total_attempts:
                        backoff = min(8.0, 2.0 * attempt)
                        print(
                            f"[gcolab:transfer] download attempt {attempt}/{total_attempts} failed ({exc}); retrying in {backoff:.0f}s...",
                            file=sys.stderr,
                            flush=True,
                        )
                        time.sleep(backoff)

        if last_exc is not None:
            raise TransportError(f"Download of {remote_path!r} failed after {total_attempts} attempt(s): {last_exc}")

        progress.set_stage("commit", str(dest))
        if is_single_file:
            treat_as_dir = dest.is_dir() or dest_raw.endswith(("/", "\\")) or dest_raw in (".", "..")
            final_file = (dest / r_base) if treat_as_dir else dest
            final_file.parent.mkdir(parents=True, exist_ok=True)
            os.replace(local_part, final_file)
            final_target = str(final_file)
            extracted_files = 1
        else:
            extract_tmp = stage_dir / f".gcolab-extract-{token}"
            extract_tmp.mkdir(parents=True, exist_ok=True)
            try:
                with tarfile.open(name=str(local_part), mode="r:gz") as tf:
                    if sys.version_info >= (3, 12):
                        tf.extractall(path=extract_tmp, filter="data")
                    else:
                        tf.extractall(path=extract_tmp)
                extracted_root = extract_tmp / r_base
                actual_files = sum(1 for p in extract_tmp.rglob("*") if p.is_file())
                if actual_files != file_count:
                    raise TransportError(
                        f"Archive extraction file count mismatch: expected {file_count}, got {actual_files}"
                    )
                dest.mkdir(parents=True, exist_ok=True)
                target_root = dest / r_base
                if extracted_root.exists():
                    if target_root.exists():
                        shutil.copytree(extracted_root, target_root, dirs_exist_ok=True)
                    else:
                        shutil.move(str(extracted_root), str(target_root))
                    final_target = str(target_root)
                else:
                    shutil.copytree(extract_tmp, dest, dirs_exist_ok=True)
                    final_target = str(dest)
                extracted_files = actual_files
            finally:
                shutil.rmtree(extract_tmp, ignore_errors=True)
                local_part.unlink(missing_ok=True)
    finally:
        if not is_single_file:
            with contextlib.suppress(Exception):
                ssh_run_capture(store, session, f"rm -f {shlex.quote(remote_tmp_tar)}", timeout=20)

    elapsed = round(time.monotonic() - t0, 2)
    return {
        "items_extracted": extracted_files,
        "files_synced": extracted_files,
        "bytes_received": expected_size,
        "bytes_transferred": expected_size,
        "sha256": expected_sha,
        "verified": True,
        "transport": used_method,
        "elapsed_s": elapsed,
        "remote_path": remote_path,
        "local_path": final_target,
    }


def update_ssh_config_entry(store: StateStore, session: SessionRecord) -> str:
    """Write/update a Host block in ~/.config/gcolab/ssh_config for direct ssh/IDE use."""
    store.ensure_dirs()
    priv_key, _ = ensure_ssh_keypair()
    host_alias = f"gcolab-{session.name}"
    existing = store.ssh_config_path.read_text(encoding="utf-8") if store.ssh_config_path.exists() else ""
    blocks = _strip_host_block(existing, host_alias)

    if session.external_ip and session.ssh_mode != "iap":
        key_p = str(priv_key).replace("\\", "/")
        kh_p = str(store.known_hosts_path).replace("\\", "/")
        hk_alias = session_host_key_alias(session, session.external_ip, 22)
        block = (
            f"Host {host_alias}\n"
            f"  HostName {session.external_ip}\n"
            f"  User {session.ssh_user}\n"
            f"  IdentityFile \"{key_p}\"\n"
            f"  StrictHostKeyChecking accept-new\n"
            f"  HostKeyAlias {hk_alias}\n"
            f"  UserKnownHostsFile \"{kh_p}\"\n"
        )
        blocks = (blocks.rstrip() + "\n\n" + block).lstrip()
    store.ssh_config_path.write_text(blocks, encoding="utf-8")
    if os.name != "nt":
        store.ssh_config_path.chmod(0o600)
    return host_alias


def remove_ssh_config_entry(store: StateStore, session_name: str) -> None:
    if not store.ssh_config_path.exists():
        return
    host_alias = f"gcolab-{session_name}"
    updated = _strip_host_block(store.ssh_config_path.read_text(encoding="utf-8"), host_alias)
    store.ssh_config_path.write_text(updated, encoding="utf-8")


def _strip_host_block(config_text: str, host_alias: str) -> str:
    out_lines: list[str] = []
    skip = False
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("host "):
            skip = stripped.split(None, 1)[1].strip() == host_alias
        if not skip:
            out_lines.append(line)
    return "\n".join(out_lines).strip() + ("\n" if out_lines else "")
