"""Google Cloud authentication and CLI discovery on Windows, macOS, and Linux."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import Request

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
ATTRIBUTION_TOKEN = "agent-skills/1.0 (skill:gcp-colab-operator)"


class AuthConfigError(RuntimeError):
    """Raised when GCP credentials or target project cannot be resolved."""


def find_gcloud_binary() -> str | None:
    """Locate the host's Google Cloud CLI, including common SDK installations."""
    env_override = os.environ.get("GCOLAB_GCLOUD_BIN")
    if env_override:
        path = pathlib.Path(env_override).expanduser()
        if path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK)):
            return str(path.resolve())
        return None

    windows = sys.platform == "win32"
    for candidate in (("gcloud.cmd", "gcloud") if windows else ("gcloud",)):
        hit = shutil.which(candidate)
        if hit:
            return hit

    if not windows:
        well_known = [
            pathlib.Path.home() / "google-cloud-sdk" / "bin" / "gcloud",
            pathlib.Path("/opt/homebrew/bin/gcloud"),
            pathlib.Path("/opt/homebrew/share/google-cloud-sdk/bin/gcloud"),
            pathlib.Path("/usr/local/bin/gcloud"),
            pathlib.Path("/usr/local/share/google-cloud-sdk/bin/gcloud"),
            pathlib.Path("/usr/lib/google-cloud-sdk/bin/gcloud"),
            pathlib.Path("/opt/google-cloud-sdk/bin/gcloud"),
            pathlib.Path("/snap/bin/gcloud"),
        ]
        return next((str(p) for p in well_known if p.is_file() and os.access(p, os.X_OK)), None)

    local_app = os.environ.get("LOCALAPPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")
    prog_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    prog = os.environ.get("ProgramFiles", r"C:\Program Files")

    well_known = [
        pathlib.Path(local_app) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd",
        pathlib.Path(user_profile) / "google-cloud-sdk" / "bin" / "gcloud.cmd",
        pathlib.Path(user_profile) / "scoop" / "apps" / "gcloud" / "current" / "bin" / "gcloud.cmd",
        pathlib.Path(prog_x86) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd",
        pathlib.Path(prog) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd",
        pathlib.Path(r"C:\google-cloud-sdk\bin\gcloud.cmd"),
        pathlib.Path(r"D:\google-cloud-sdk\bin\gcloud.cmd"),
    ]
    for path in well_known:
        if path.is_file():
            return str(path)
    return None


def adc_file_path() -> pathlib.Path:
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit:
        return pathlib.Path(explicit).expanduser()
    sdk_config = os.environ.get("CLOUDSDK_CONFIG")
    if sdk_config:
        return pathlib.Path(sdk_config).expanduser() / "application_default_credentials.json"
    appdata = os.environ.get("APPDATA")
    if sys.platform == "win32" and appdata:
        return pathlib.Path(appdata) / "gcloud" / "application_default_credentials.json"
    return pathlib.Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def run_gcloud_readonly(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
    gcloud_bin = find_gcloud_binary()
    if not gcloud_bin:
        return None
    env = os.environ.copy()
    if os.environ.get("GCOLAB_DISABLE_ATTRIBUTION") != "1":
        env["CLOUDSDK_METRICS_REQUEST_ATTRIBUTION"] = ATTRIBUTION_TOKEN
    cmd = [gcloud_bin, *args, "--quiet"]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        check=False,
    )


def get_access_token_and_adc_project() -> tuple[str, str | None, str]:
    """Return (access_token, discovered_project_id, auth_source_label)."""
    try:
        creds, project_id = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        if not creds.valid:
            creds.refresh(Request())
        if creds.token:
            quota_proj = getattr(creds, "quota_project_id", None)
            return str(creds.token), (project_id or quota_proj), "adc"
    except Exception:
        pass

    # Fallback to gcloud CLI session if installed
    for subcmd, label in (
        (["auth", "application-default", "print-access-token"], "gcloud-adc"),
        (["auth", "print-access-token"], "gcloud-user"),
    ):
        res = run_gcloud_readonly(subcmd, timeout=15)
        if res and res.returncode == 0 and res.stdout.strip():
            token = res.stdout.strip().splitlines()[-1].strip()
            proj = get_gcloud_configured_project()
            return token, proj, label

    raise AuthConfigError(
        "No valid Google Cloud credentials found. Run:\n"
        "  gcloud auth application-default login\n"
        "(or 'gcloud auth login' after installing Google Cloud CLI)."
    )


def get_gcloud_configured_project() -> str | None:
    res = run_gcloud_readonly(["config", "get-value", "core/project"], timeout=10)
    if res and res.returncode == 0:
        val = res.stdout.strip()
        if val and val != "(unset)":
            return val
    return None


def resolve_project_id(
    explicit_project: str | None = None,
    settings_project: str | None = None,
    adc_project: str | None = None,
) -> str:
    for candidate in (
        explicit_project,
        os.environ.get("GCOLAB_PROJECT"),
        os.environ.get("GOOGLE_CLOUD_PROJECT"),
        os.environ.get("GCLOUD_PROJECT"),
        os.environ.get("CLOUDSDK_CORE_PROJECT"),
        settings_project,
        adc_project,
        get_gcloud_configured_project(),
    ):
        if candidate and str(candidate).strip() and str(candidate).strip() != "(unset)":
            return str(candidate).strip()

    raise AuthConfigError(
        "No target GCP project configured. Pass '--project <PROJECT_ID>', "
        "run 'gcolab config set project <PROJECT_ID>', or set GCOLAB_PROJECT."
    )


def inspect_local_auth_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Safe diagnostic summary (never returns raw tokens or secret payloads)."""
    gcloud_bin = find_gcloud_binary()
    adc_path = adc_file_path()
    adc_exists = adc_path.exists()

    token_ok = False
    auth_source = None
    adc_project = None
    auth_error = None
    try:
        _, adc_project, auth_source = get_access_token_and_adc_project()
        token_ok = True
    except AuthConfigError as exc:
        auth_error = str(exc)

    resolved_project = None
    try:
        resolved_project = resolve_project_id(
            settings_project=(settings or {}).get("default_project"),
            adc_project=adc_project,
        )
    except AuthConfigError:
        resolved_project = None

    gcloud_account = None
    if gcloud_bin:
        acct_res = run_gcloud_readonly(["config", "get-value", "account"], timeout=10)
        if acct_res and acct_res.returncode == 0:
            val = acct_res.stdout.strip()
            if val and val != "(unset)":
                gcloud_account = val

    return {
        "gcloud_installed": gcloud_bin is not None,
        "gcloud_path": gcloud_bin,
        "gcloud_account": gcloud_account,
        "adc_file_exists": adc_exists,
        "adc_file_path": str(adc_path),
        "credentials_valid": token_ok,
        "auth_source": auth_source,
        "resolved_project": resolved_project,
        "auth_error": auth_error,
    }
