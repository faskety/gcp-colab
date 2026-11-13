"""Local session state, inter-process locking, workspace bindings, and audit logging."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


WORKSPACE_BINDING_DIR = ".gcolab"
WORKSPACE_BINDING_FILE = "project.json"


def default_state_dir() -> pathlib.Path:
    override = os.environ.get("GCOLAB_HOME")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return pathlib.Path.home() / ".config" / "gcolab"


def sanitize_session_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError(f"Invalid session name: {name!r}")
    if not cleaned[0].isalpha():
        cleaned = f"s-{cleaned}"
    return cleaned[:36]


def make_instance_name(session_name: str) -> str:
    slug = sanitize_session_name(session_name)
    suffix = secrets.token_hex(2)
    raw = f"gcolab-{slug}-{suffix}"
    return raw[:62].rstrip("-")


def find_workspace_binding(start_dir: str | pathlib.Path | None = None) -> tuple[pathlib.Path, dict[str, Any]] | None:
    """Walk upward from start_dir to locate a workspace-scoped .gcolab/project.json."""
    cur = pathlib.Path(start_dir or pathlib.Path.cwd()).expanduser().resolve()
    if cur.is_file():
        cur = cur.parent
    home = pathlib.Path.home().resolve()

    while True:
        candidate = cur / WORKSPACE_BINDING_DIR / WORKSPACE_BINDING_FILE
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("session"):
                    return candidate, data
            except json.JSONDecodeError:
                pass
        parent = cur.parent
        if parent == cur or parent == home:
            break
        cur = parent
    return None


def write_workspace_binding(
    workdir: str | pathlib.Path,
    *,
    session_name: str,
    project_id: str | None = None,
    config_path: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """Bind a project directory to a session in <workdir>/.gcolab/project.json."""
    root = pathlib.Path(workdir).expanduser().resolve()
    if root == pathlib.Path.home().resolve() or root.parent == root:
        raise ValueError(f"Refusing to create workspace session binding in home or drive root: {root}")
    bdir = root / WORKSPACE_BINDING_DIR
    bdir.mkdir(parents=True, exist_ok=True)
    gi = bdir / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")

    payload: dict[str, Any] = {
        "session": sanitize_session_name(session_name),
        "workdir": str(root),
        "bound_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if project_id:
        payload["project"] = project_id
    if config_path is not None:
        payload["config"] = str(pathlib.Path(config_path).expanduser().resolve())

    target = bdir / WORKSPACE_BINDING_FILE
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return target


def clear_workspace_binding(
    start_dir: str | pathlib.Path | None = None,
    *,
    only_if_session: str | None = None,
) -> pathlib.Path | None:
    """Remove workspace .gcolab/project.json if present (optionally matching session name)."""
    found = find_workspace_binding(start_dir)
    if not found:
        return None
    bpath, data = found
    if only_if_session and sanitize_session_name(str(data.get("session", ""))) != sanitize_session_name(only_if_session):
        return None
    bpath.unlink(missing_ok=True)
    return bpath


@dataclasses.dataclass
class SessionRecord:
    name: str
    instance_name: str
    project: str
    region: str
    zone: str
    machine_type: str
    gpu: str | None = None
    gpu_count: int = 0
    spot: bool = True
    spot_action: str = "DELETE"
    provisioning_model: str = ""
    boot_disk_name: str = ""
    disk_gb: int = 100
    disk_type: str = "pd-balanced"
    image: str = ""
    ssh_mode: str = "direct"
    ssh_user: str = "gcolab"
    external_ip: str | None = None
    internal_ip: str | None = None
    idle_minutes: int = 30
    autodown: bool = False
    max_run_seconds: int | None = None
    status: str = "PROVISIONING"
    created_at: str = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )
    last_used_at: str = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRecord":
        valid = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid}
        return cls(**filtered)


class StateStore:
    """Manages sessions.json, settings.json, and history/*.jsonl under an OS file lock."""

    def __init__(self, sessions_file: str | pathlib.Path | None = None) -> None:
        base = default_state_dir()
        if sessions_file is not None:
            self.sessions_path = pathlib.Path(sessions_file).expanduser().resolve()
            self.base_dir = self.sessions_path.parent
        else:
            self.base_dir = base
            self.sessions_path = base / "sessions.json"
        self.settings_path = self.base_dir / "settings.json"
        self.history_dir = self.base_dir / "history"
        self.known_hosts_path = self.base_dir / "known_hosts"
        self.ssh_config_path = self.base_dir / "ssh_config"
        self.lock_path = self.base_dir / "state.lock"
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_fd: int | None = None

    def ensure_dirs(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def locked(self, timeout_s: float = 15.0) -> Iterator[None]:
        """Acquire re-entrant thread + OS-level file lock across concurrent agent processes."""
        with self._thread_lock:
            if self._lock_depth > 0:
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return

            self.ensure_dirs()
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o666)
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                deadline = time.monotonic() + timeout_s
                acquired = False
                while True:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        if os.name == "nt":
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        else:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Timed out acquiring gcolab state lock {self.lock_path} after {timeout_s}s"
                            )
                        time.sleep(0.025)

                self._lock_fd = fd
                self._lock_depth = 1
                try:
                    yield
                finally:
                    self._lock_depth = 0
                    self._lock_fd = None
                    if acquired:
                        try:
                            os.lseek(fd, 0, os.SEEK_SET)
                            if os.name == "nt":
                                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                            else:
                                fcntl.flock(fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
            finally:
                os.close(fd)

    def _atomic_write_json(self, path: pathlib.Path, payload: Any) -> None:
        self.ensure_dirs()
        token = secrets.token_hex(3)
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{token}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def load_settings(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {}
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self.locked():
            current = self.load_settings()
            current.update({k: v for k, v in updates.items() if v is not None})
            self._atomic_write_json(self.settings_path, current)
            return current

    def load_sessions(self) -> dict[str, SessionRecord]:
        if not self.sessions_path.exists():
            return {}
        try:
            raw = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        sessions: dict[str, SessionRecord] = {}
        for k, val in raw.get("sessions", {}).items():
            if isinstance(val, dict):
                sessions[k] = SessionRecord.from_dict(val)
        return sessions

    def save_sessions(self, sessions: dict[str, SessionRecord]) -> None:
        with self.locked():
            payload = {
                "version": 1,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "sessions": {k: v.to_dict() for k, v in sorted(sessions.items())},
            }
            self._atomic_write_json(self.sessions_path, payload)

    def mutate_sessions(
        self,
        mutator: Callable[[dict[str, SessionRecord]], Any],
    ) -> Any:
        with self.locked():
            sessions = self.load_sessions()
            result = mutator(sessions)
            self.save_sessions(sessions)
            return result

    def get_session(self, name: str) -> SessionRecord:
        key = sanitize_session_name(name)
        sessions = self.load_sessions()
        if key not in sessions:
            raise KeyError(
                f"Session {key!r} not found in local state ({self.sessions_path}). "
                "Run 'gcolab status' or 'gcolab new -s <name>' first."
            )
        return sessions[key]

    def upsert_session(self, record: SessionRecord) -> None:
        with self.locked():
            key = sanitize_session_name(record.name)
            record.name = key
            record.last_used_at = dt.datetime.now(dt.timezone.utc).isoformat()
            sessions = self.load_sessions()
            sessions[key] = record
            self.save_sessions(sessions)

    def remove_session(self, name: str) -> SessionRecord | None:
        with self.locked():
            key = sanitize_session_name(name)
            sessions = self.load_sessions()
            removed = sessions.pop(key, None)
            self.save_sessions(sessions)
            return removed

    def append_event(self, session_name: str, event_type: str, detail: dict[str, Any]) -> None:
        with self.locked():
            self.ensure_dirs()
            key = sanitize_session_name(session_name)
            log_file = self.history_dir / f"{key}.jsonl"
            entry = {
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "session": key,
                "event": event_type,
                **detail,
            }
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write_receipt(self, session_name: str, receipt: dict[str, Any]) -> pathlib.Path:
        with self.locked():
            self.ensure_dirs()
            key = sanitize_session_name(session_name)
            rpath = self.history_dir / f"{key}-receipt.json"
            self._atomic_write_json(rpath, receipt)
            return rpath

    def read_events(
        self,
        session_name: str,
        limit: int = 20,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        key = sanitize_session_name(session_name)
        log_file = self.history_dir / f"{key}.jsonl"
        if not log_file.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_type and obj.get("event") != event_type:
                continue
            items.append(obj)
        return items[-limit:] if limit > 0 else items
