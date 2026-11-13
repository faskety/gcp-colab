"""gcolab CLI entrypoint: Native Windows 'Colab + SkyPilot' workflow for GCP Compute Engine."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import secrets
import shlex
import sys
import time
from typing import Any

from gcolab import __version__
from gcolab.auth import (
    AuthConfigError,
    get_access_token_and_adc_project,
    inspect_local_auth_status,
    resolve_project_id,
)
from gcolab.catalog import catalog_summary_rows, estimate_session_cost, resolve_hardware_spec
from gcolab.config import (
    SessionRecord,
    StateStore,
    clear_workspace_binding,
    find_workspace_binding,
    make_instance_name,
    sanitize_session_name,
    write_workspace_binding,
)
from gcolab.gcp_api import ComputeClient, GCPApiError, SafetyIsolationError, StockoutError
from gcolab.remote_agent import build_autostop_json, render_startup_script
from gcolab.task_spec import TaskSpec, load_task_yaml
from gcolab.transport import (
    TransferOptions,
    TransportError,
    download_path,
    ensure_ssh_keypair,
    find_ssh_binary,
    remove_ssh_config_entry,
    ssh_run_capture,
    ssh_run_stream,
    sync_workdir,
    update_ssh_config_entry,
    upload_path,
    wait_for_vm_ready,
)

EXIT_ARTIFACT_DELIVERY_FAILED = 3
EXIT_TEARDOWN_FAILED = 4


def _log(msg: str) -> None:
    print(f"[gcolab] {msg}", file=sys.stderr, flush=True)


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def _parse_iso_utc(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        cleaned = str(ts).strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _transfer_opts_from_args(args: argparse.Namespace) -> TransferOptions:
    return TransferOptions(
        method=getattr(args, "method", "auto") or "auto",
        connect_timeout=int(getattr(args, "connect_timeout", 20) or 20),
        stall_timeout=int(getattr(args, "stall_timeout", 90) or 90),
        max_duration=int(getattr(args, "max_duration", 3600) or 3600),
        retries=int(getattr(args, "retries", 2) if getattr(args, "retries", None) is not None else 2),
        show_progress=not bool(getattr(args, "no_progress", False)),
    )


def _call_upload(
    store: StateStore,
    rec: SessionRecord,
    local_path: str | pathlib.Path,
    remote_path: str = "/workspace",
    options: TransferOptions | None = None,
) -> dict[str, Any]:
    try:
        return upload_path(store, rec, local_path, remote_path=remote_path, options=options)
    except TypeError:
        return upload_path(store, rec, local_path, remote_path=remote_path)


def _call_sync(
    store: StateStore,
    rec: SessionRecord,
    local_dir: str | pathlib.Path,
    remote_dir: str = "/workspace",
    *,
    clean: bool = False,
    options: TransferOptions | None = None,
) -> dict[str, Any]:
    try:
        return sync_workdir(store, rec, local_dir, remote_dir=remote_dir, clean=clean, options=options)
    except TypeError:
        return sync_workdir(store, rec, local_dir, remote_dir=remote_dir, clean=clean)


def _call_download(
    store: StateStore,
    rec: SessionRecord,
    remote_path: str,
    local_dest: str | pathlib.Path = ".",
    options: TransferOptions | None = None,
) -> dict[str, Any]:
    try:
        return download_path(store, rec, remote_path, local_dest=local_dest, options=options)
    except TypeError:
        return download_path(store, rec, remote_path, local_dest=local_dest)


def _resolve_session_name(args: argparse.Namespace, *, required: bool = True) -> str | None:
    explicit = getattr(args, "session", None)
    if explicit:
        return sanitize_session_name(explicit)
    found = find_workspace_binding()
    if found:
        _, data = found
        if data.get("session"):
            return sanitize_session_name(str(data["session"]))
    if required:
        raise ValueError(
            "No session specified. Pass '-s <session>' or bind this project folder via 'gcolab bind -s <session>'."
        )
    return None


def _resolve_client_and_project(store: StateStore, explicit_project: str | None) -> tuple[ComputeClient, str]:
    settings = store.load_settings()
    bound_proj = None
    found = find_workspace_binding()
    if found:
        bound_proj = found[1].get("project")
    _, adc_proj, _ = get_access_token_and_adc_project()
    project_id = resolve_project_id(
        explicit_project=explicit_project or bound_proj,
        settings_project=settings.get("default_project"),
        adc_project=adc_proj,
    )
    return ComputeClient(project_id=project_id), project_id


def _set_remote_autostop(
    store: StateStore,
    rec: SessionRecord,
    *,
    idle_minutes: int,
    autodown: bool,
    wait_for: str = "jobs_and_ssh",
) -> None:
    cfg_json = build_autostop_json(
        idle_minutes=idle_minutes,
        autodown=autodown,
        wait_for=wait_for,
    )
    remote_cmd = (
        f"cat <<'EOF' >/opt/gcolab/autostop.json\n{cfg_json}\nEOF\n"
        "rm -f /opt/gcolab/autostop_state.json"
    )
    res = ssh_run_capture(store, rec, remote_cmd, timeout=30)
    if res.returncode != 0:
        raise TransportError(res.stderr.decode("utf-8", errors="replace"))
    rec.idle_minutes = idle_minutes
    rec.autodown = autodown
    store.upsert_session(rec)


def _teardown_verification_failures(data: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not bool(data.get("instance_absent")):
        reasons.append(f"instance still present or unverified ({data.get('instance_name')})")
    if not bool(data.get("boot_disk_absent")):
        reasons.append(f"boot disk still present or unverified ({data.get('boot_disk_name')})")
    if not bool(data.get("session_scan_verified", True)):
        err = data.get("session_scan_error") or "aggregated instance scan failed"
        reasons.append(f"remaining resource scan failed ({err})")
    rem = data.get("remaining_session_resources") or []
    if rem:
        names = ", ".join(f"{r.get('zone', '?')}/{r.get('name', '?')}" for r in rem)
        reasons.append(f"remaining session resources still exist ({names})")
    return reasons


def _teardown_and_build_receipt(
    store: StateStore,
    rec: SessionRecord,
    *,
    required_artifacts: list[dict[str, Any]] | None = None,
    required_failures: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    client = ComputeClient(project_id=rec.project)
    teardown_started = dt.datetime.now(dt.timezone.utc)
    if hasattr(client, "delete_and_verify_instance"):
        cloud_verify = client.delete_and_verify_instance(
            rec.zone,
            rec.instance_name,
            session_name=rec.name,
            boot_disk_name=rec.boot_disk_name or None,
        )
    else:
        deleted = client.delete_instance(rec.zone, rec.instance_name)
        cloud_verify = {
            "was_present": bool(deleted),
            "instance_name": rec.instance_name,
            "boot_disk_name": rec.boot_disk_name or rec.instance_name,
            "instance_absent": True,
            "boot_disk_absent": True,
            "session_scan_verified": True,
            "remaining_session_resources": [],
            "teardown_verified": True,
            "provisioning_model": rec.provisioning_model or ("SPOT" if rec.spot else "STANDARD"),
            "creation_timestamp": rec.created_at,
        }

    teardown_finished = dt.datetime.now(dt.timezone.utc)
    start_dt = _parse_iso_utc(cloud_verify.get("creation_timestamp")) or _parse_iso_utc(rec.created_at)
    runtime_sec = max(0.0, (teardown_finished - start_dt).total_seconds()) if start_dt else 0.0
    prov_model = str(
        cloud_verify.get("provisioning_model")
        or rec.provisioning_model
        or ("SPOT" if rec.spot else "STANDARD")
    )
    is_spot = prov_model.upper() == "SPOT"
    cost_est = estimate_session_cost(
        machine_type=rec.machine_type,
        gpu=rec.gpu,
        gpu_count=rec.gpu_count,
        spot=is_spot,
        disk_gb=rec.disk_gb,
        disk_type=rec.disk_type,
        runtime_seconds=runtime_sec,
    )

    dl_events = store.read_events(rec.name, limit=500, event_type="download")
    artifacts = required_artifacts if required_artifacts is not None else dl_events
    failures = required_failures or []
    verified_count = sum(1 for a in artifacts if a.get("verified", True))
    total_files = sum(int(a.get("files_synced", a.get("items_extracted", 0)) or 0) for a in artifacts)
    total_bytes = sum(int(a.get("bytes_transferred", a.get("bytes_received", 0)) or 0) for a in artifacts)

    verification_failures = _teardown_verification_failures(cloud_verify)
    teardown_verified = len(verification_failures) == 0
    resolved_boot_disk = str(cloud_verify.get("boot_disk_name") or rec.boot_disk_name or rec.instance_name)

    receipt: dict[str, Any] = {
        "session": rec.name,
        "project": rec.project,
        "region": rec.region,
        "zone": rec.zone,
        "instance_name": rec.instance_name,
        "boot_disk_name": resolved_boot_disk,
        "machine_type": rec.machine_type,
        "gpu": rec.gpu,
        "gpu_count": rec.gpu_count,
        "provisioning_model": prov_model,
        "teardown_verified": teardown_verified,
        "verification_failures": verification_failures,
        "instance_absent": bool(cloud_verify.get("instance_absent")),
        "boot_disk_absent": bool(cloud_verify.get("boot_disk_absent")),
        "session_scan_verified": bool(cloud_verify.get("session_scan_verified", True)),
        "remaining_session_resources": cloud_verify.get("remaining_session_resources") or [],
        "deliverables": {
            "artifacts_recorded": len(artifacts),
            "verified_artifacts": verified_count,
            "failed_required_artifacts": len(failures),
            "failures": failures,
            "total_files_retrieved": total_files,
            "total_bytes_retrieved": total_bytes,
            "all_required_verified": len(failures) == 0,
            "items": [
                {
                    "remote_path": a.get("remote_path"),
                    "local_path": a.get("local_path"),
                    "files_synced": a.get("files_synced", a.get("items_extracted", 0)),
                    "bytes_transferred": a.get("bytes_transferred", a.get("bytes_received", 0)),
                    "sha256": a.get("sha256"),
                    "verified": a.get("verified", True),
                }
                for a in artifacts
            ],
        },
        "runtime": {
            "created_at": start_dt.isoformat() if start_dt else rec.created_at,
            "teardown_started_at": teardown_started.isoformat(),
            "teardown_completed_at": teardown_finished.isoformat(),
            "runtime_seconds": round(runtime_sec, 1),
            "runtime_minutes": round(runtime_sec / 60.0, 2),
        },
        "cost_estimate": cost_est,
    }

    receipt_path = store.write_receipt(rec.name, receipt)
    receipt["receipt_path"] = str(receipt_path)

    if teardown_verified:
        store.remove_session(rec.name)
        remove_ssh_config_entry(store, rec.name)
        clear_workspace_binding(only_if_session=rec.name)
        store.append_event(
            rec.name,
            "down",
            {
                "instance_name": rec.instance_name,
                "teardown_verified": True,
                "instance_absent": receipt["instance_absent"],
                "boot_disk_absent": receipt["boot_disk_absent"],
                "runtime_seconds": receipt["runtime"]["runtime_seconds"],
                "estimated_total_usd": cost_est["estimated_total_usd"],
                "receipt_path": str(receipt_path),
            },
        )
    else:
        rec.status = "TEARDOWN_INCOMPLETE"
        rec.boot_disk_name = resolved_boot_disk
        store.upsert_session(rec)
        store.append_event(
            rec.name,
            "down_incomplete",
            {
                "instance_name": rec.instance_name,
                "teardown_verified": False,
                "verification_failures": verification_failures,
                "instance_absent": receipt["instance_absent"],
                "boot_disk_absent": receipt["boot_disk_absent"],
                "receipt_path": str(receipt_path),
            },
        )
    return receipt


def _format_receipt_text(r: dict[str, Any]) -> str:
    dl = r["deliverables"]
    rt = r["runtime"]
    ce = r["cost_estimate"]
    rem = r["remaining_session_resources"]
    verified = bool(r.get("teardown_verified", False))
    verified_label = "YES" if verified else f"NO — RETRY gcolab down -s {r['session']}"
    lines = [
        f"Teardown receipt for session '{r['session']}' ({r['project']} / {r['zone']}):",
        f"  Teardown verified      : {verified_label}",
        f"  Instance absent (404)  : {'YES' if r['instance_absent'] else 'NO — CHECK MANUALLY'} ({r['instance_name']})",
        f"  Boot disk absent (404) : {'YES' if r['boot_disk_absent'] else 'NO — CHECK MANUALLY'} ({r['boot_disk_name']})",
        f"  Provisioning model     : {r['provisioning_model']} ({r['machine_type']})",
        f"  Deliverables verified  : {'YES' if dl['all_required_verified'] else 'FAILED'} "
        f"({dl['verified_artifacts']} artifact set(s), {dl['total_files_retrieved']} file(s), {dl['total_bytes_retrieved']} B)",
        f"  Runtime & est. cost    : {rt['runtime_minutes']} min ({rt['runtime_seconds']}s) | "
        f"~${ce['estimated_total_usd']:.4f} USD (compute ${ce['compute_hourly_usd']}/hr + disk ${ce['disk_hourly_usd']}/hr, 60s min)",
        f"  Remaining resources    : {len(rem)} (session-scoped managed instances)",
        f"  Receipt saved to       : {r.get('receipt_path')}",
    ]
    if r.get("verification_failures"):
        lines.append(f"  Verification issues    : {'; '.join(r['verification_failures'])}")
    return "\n".join(lines)


def _provision_session(
    store: StateStore,
    *,
    session_name: str,
    explicit_project: str | None,
    gpu: str | None,
    gpu_count: int,
    cpus: int | None,
    machine_type: str | None,
    disk_gb: int | None,
    disk_type: str | None,
    spot: bool,
    spot_action: str,
    idle_minutes: int,
    autodown: bool,
    max_hours: float | None,
    region: str | None,
    zone: str | None,
    image: str | None,
    ssh_mode: str,
    no_external_ip: bool,
    allow_any_ip: bool,
    service_account: str | None,
) -> SessionRecord:
    sname = sanitize_session_name(session_name)
    existing = store.load_sessions()
    if sname in existing and existing[sname].status in (
        "RUNNING",
        "PROVISIONING",
        "STOPPED",
        "TERMINATED",
        "TEARDOWN_FAILED",
        "TEARDOWN_INCOMPLETE",
    ):
        raise ValueError(
            f"Session {sname!r} already exists ({existing[sname].instance_name} in {existing[sname].zone}, "
            f"status={existing[sname].status}). Stop/down it first or choose another '-s <name>'."
        )

    hw = resolve_hardware_spec(
        gpu=gpu,
        gpu_count=gpu_count,
        cpus=cpus,
        machine_type=machine_type,
        disk_gb=disk_gb,
        disk_type=disk_type,
        spot=spot,
    )
    client, project_id = _resolve_client_and_project(store, explicit_project)

    eff_ssh_mode = "iap" if no_external_ip else ssh_mode
    _, pubkey = ensure_ssh_keypair()

    if not no_external_ip and eff_ssh_mode == "direct":
        _log("Ensuring SSH ingress firewall rule (caller /32 + Google IAP range)...")
        client.ensure_ssh_firewall(allow_any_ip=allow_any_ip)

    source_image = client.resolve_source_image(has_gpu=bool(hw["gcp_accelerator"]), explicit_image=image)

    if zone:
        inferred_region = region or zone.rsplit("-", 1)[0]
        candidates = [
            {
                "region": inferred_region,
                "zone": zone,
                "machine_type": hw["machine_type"],
                "obtainability": 1.0,
                "estimated_uptime_s": 3600,
            }
        ]
    else:
        regions = [region] if region else hw["candidate_regions"]
        _log(
            f"Ranking candidate GCP zones across {', '.join(regions)} "
            f"for {hw['machine_type']} ({'SPOT' if spot else 'STANDARD'})..."
        )
        candidates = client.rank_candidate_zones(
            regions=regions,
            machine_type=hw["machine_type"],
            gcp_accelerator=hw["gcp_accelerator"],
            spot=spot,
        )
        if not candidates:
            raise GCPApiError(
                f"No candidate zones found for {hw['machine_type']} in regions {regions}."
            )

    max_run_seconds = int(max_hours * 3600) if max_hours and max_hours > 0 else None
    if not spot and str(hw["machine_type"]).lower().startswith("e2-") and max_run_seconds:
        _log(
            f"Note: GCE {hw['machine_type']} STANDARD requires MIGRATE maintenance and omits hardware "
            f"maxRunDuration; VM-side autostop timer (--idle {idle_minutes}m) remains active."
        )

    startup_script = render_startup_script(
        ssh_user="gcolab",
        idle_minutes=idle_minutes,
        autodown=autodown,
        wait_for="jobs_and_ssh",
        has_gpu=bool(hw["gcp_accelerator"]),
    )

    inst_name = make_instance_name(sname)
    created_inst: dict[str, Any] | None = None
    chosen_zone = ""
    chosen_region = ""
    errors_seen: list[str] = []

    for idx, cand in enumerate(candidates[:8], start=1):
        czone = cand["zone"]
        cregion = cand["region"]
        score_note = f" (obtainability={cand['obtainability']})" if spot else ""
        _log(f"[{idx}/{min(len(candidates), 8)}] Provisioning {inst_name} in {czone}{score_note}...")
        body = client.build_instance_insert_body(
            instance_name=inst_name,
            session_name=sname,
            zone=czone,
            machine_type=hw["machine_type"],
            source_image=source_image,
            disk_gb=hw["disk_gb"],
            disk_type=hw["disk_type"],
            gcp_accelerator=hw["gcp_accelerator"],
            gpu_count=hw["gpu_count"],
            spot=spot,
            spot_action=spot_action,
            max_run_seconds=max_run_seconds,
            ssh_user="gcolab",
            ssh_pubkey=pubkey,
            startup_script=startup_script,
            enable_external_ip=not no_external_ip,
            service_account_email=service_account,
        )
        try:
            created_inst = client.create_instance_in_zone(czone, body)
            chosen_zone = czone
            chosen_region = cregion
            break
        except StockoutError as exc:
            _log(f"Zone {czone} capacity unavailable ({exc.reason or 'stockout'}), failing over...")
            errors_seen.append(f"{czone}: {exc}")
            inst_name = make_instance_name(sname)
            continue

    if created_inst is None:
        raise StockoutError(
            "All candidate zones exhausted or unavailable:\n  " + "\n  ".join(errors_seen)
        )

    ext_ip, int_ip = client.extract_ips(created_inst)
    live_prov_model = str((created_inst.get("scheduling") or {}).get("provisioningModel") or ("SPOT" if spot else "STANDARD"))
    live_disk_name = client.extract_boot_disk_name(created_inst, fallback=inst_name)
    created_ts = created_inst.get("creationTimestamp") or dt.datetime.now(dt.timezone.utc).isoformat()

    rec = SessionRecord(
        name=sname,
        instance_name=inst_name,
        project=project_id,
        region=chosen_region,
        zone=chosen_zone,
        machine_type=hw["machine_type"],
        gpu=hw["gpu"],
        gpu_count=hw["gpu_count"],
        spot=(live_prov_model.upper() == "SPOT"),
        spot_action=spot_action.upper(),
        provisioning_model=live_prov_model,
        boot_disk_name=live_disk_name,
        disk_gb=hw["disk_gb"],
        disk_type=hw["disk_type"],
        image=source_image,
        ssh_mode=eff_ssh_mode,
        ssh_user="gcolab",
        external_ip=ext_ip,
        internal_ip=int_ip,
        idle_minutes=idle_minutes,
        autodown=autodown,
        max_run_seconds=max_run_seconds,
        status=created_inst.get("status", "RUNNING"),
        created_at=created_ts,
    )
    store.upsert_session(rec)
    update_ssh_config_entry(store, rec)
    store.append_event(
        sname,
        "provision",
        {
            "instance_name": inst_name,
            "boot_disk_name": live_disk_name,
            "zone": chosen_zone,
            "machine_type": hw["machine_type"],
            "gpu": hw["gpu"],
            "provisioning_model": live_prov_model,
            "spot": rec.spot,
            "external_ip": ext_ip,
        },
    )

    _log(
        f"Instance {inst_name} RUNNING ({ext_ip or 'IAP-only'}, model={live_prov_model}). "
        "Waiting for SSH & VM agent readiness..."
    )
    wait_for_vm_ready(store, rec, timeout_s=420 if hw["gpu"] else 240)
    _log(f"Session {sname!r} is ready.")
    return rec


def cmd_doctor(args: argparse.Namespace, store: StateStore) -> int:
    settings = store.load_settings()
    auth_info = inspect_local_auth_status(settings)
    ssh_ok = False
    ssh_path = None
    scp_path = None
    try:
        ssh_path = find_ssh_binary("ssh")
        scp_path = find_ssh_binary("scp")
        ssh_ok = True
    except TransportError:
        ssh_ok = False

    priv_key, _ = ensure_ssh_keypair()
    binding = find_workspace_binding()
    proj_check: dict[str, Any] | None = None
    target_proj = args.project or (binding[1].get("project") if binding else None) or auth_info.get("resolved_project")
    if auth_info["credentials_valid"] and target_proj:
        try:
            client = ComputeClient(project_id=target_proj)
            proj_check = client.check_project_and_billing()
        except Exception as exc:
            proj_check = {"project": target_proj, "error": str(exc)}

    report = {
        "version": __version__,
        "platform": sys.platform,
        "openssh_available": ssh_ok,
        "openssh_path": ssh_path,
        "scp_path": scp_path,
        "gcolab_ssh_key": str(priv_key),
        "state_file": str(store.sessions_path),
        "workspace_binding": {"path": str(binding[0]), **binding[1]} if binding else None,
        **auth_info,
        "project_check": proj_check,
    }
    if args.output == "json":
        _emit(report, True)
    else:
        lines = [
            f"gcolab v{__version__} environment check:",
            f"  Windows OpenSSH : {'OK (' + str(ssh_path) + ')' if ssh_ok else 'MISSING'}",
            f"  gcloud CLI      : {'OK (' + str(auth_info['gcloud_path']) + ')' if auth_info['gcloud_installed'] else 'NOT FOUND'}",
            f"  ADC file        : {'EXISTS' if auth_info['adc_file_exists'] else 'MISSING'} ({auth_info['adc_file_path']})",
            f"  Credentials     : {'VALID (' + str(auth_info['auth_source']) + ')' if auth_info['credentials_valid'] else 'INVALID'}",
            f"  Active Project  : {target_proj or '(not set — run: gcolab config set project <PROJECT_ID>)'}",
        ]
        if binding:
            lines.append(f"  Bound Session   : {binding[1].get('session')} ({binding[0]})")
        if proj_check:
            lines.append(f"  Compute API     : {'ENABLED' if proj_check.get('compute_api_enabled') else 'NOT ENABLED / NO ACCESS'}")
            if proj_check.get("billing_enabled") is not None:
                lines.append(f"  Billing Linked  : {proj_check['billing_enabled']}")
        print("\n".join(lines))
    return 0 if (ssh_ok and auth_info["credentials_valid"]) else 1


def cmd_config(args: argparse.Namespace, store: StateStore) -> int:
    if args.config_action == "show":
        _emit(store.load_settings(), args.output == "json")
        return 0
    key_map = {
        "project": "default_project",
        "region": "default_region",
        "ssh-mode": "default_ssh_mode",
    }
    mapped = key_map[args.key]
    updated = store.save_settings({mapped: args.value})
    _emit(updated, args.output == "json")
    return 0


def cmd_bind(args: argparse.Namespace, store: StateStore) -> int:
    sname = sanitize_session_name(args.session)
    proj_id = args.project
    if not args.force:
        rec = store.get_session(sname)
        proj_id = proj_id or rec.project
    cfg_override = args.config if args.config else None
    bpath = write_workspace_binding(
        args.workdir,
        session_name=sname,
        project_id=proj_id,
        config_path=cfg_override,
    )
    payload = {
        "bound": True,
        "session": sname,
        "project": proj_id,
        "binding_file": str(bpath),
    }
    _emit(payload, args.output == "json")
    return 0


def cmd_unbind(args: argparse.Namespace, _store: StateStore) -> int:
    removed = clear_workspace_binding(args.workdir)
    payload = {
        "unbound": removed is not None,
        "binding_file": str(removed) if removed else None,
    }
    _emit(payload, args.output == "json")
    return 0


def cmd_gpus(args: argparse.Namespace, _store: StateStore) -> int:
    rows = catalog_summary_rows()
    if args.output == "json":
        _emit({"accelerators": rows}, True)
        return 0
    header = f"{'GPU':<10} {'VRAM':<6} {'Default Machine':<16} {'Spot $/hr':<10} {'OnDemand $/hr':<14} {'Notes'}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['gpu']:<10} {str(r['vram_gb']) + 'GB':<6} {r['default_machine']:<16} "
            f"${r['approx_spot_usd_hr']:<9.2f} ${r['approx_ondemand_usd_hr']:<13.2f} {r['notes']}"
        )
    return 0


def cmd_capacity(args: argparse.Namespace, store: StateStore) -> int:
    hw = resolve_hardware_spec(
        gpu=args.gpu,
        gpu_count=args.count,
        cpus=args.cpus,
        machine_type=args.machine_type,
        spot=args.spot,
    )
    client, project_id = _resolve_client_and_project(store, args.project)
    regions = [r.strip() for r in args.regions.split(",") if r.strip()] if args.regions else hw["candidate_regions"]
    ranked = client.rank_candidate_zones(
        regions=regions,
        machine_type=hw["machine_type"],
        gcp_accelerator=hw["gcp_accelerator"],
        spot=args.spot,
    )
    payload = {
        "project": project_id,
        "hardware": hw,
        "recommendations": ranked,
    }
    if args.output == "json":
        _emit(payload, True)
        return 0
    print(
        f"Capacity ranking for {hw['machine_type']} "
        f"({'SPOT' if args.spot else 'STANDARD'}, ~${hw['estimated_hourly_usd']}/hr):"
    )
    for item in ranked[:15]:
        print(
            f"  {item['zone']:<20} obtainability={item['obtainability']:<4} "
            f"est_uptime={item['estimated_uptime_s']}s"
        )
    return 0


def cmd_new(args: argparse.Namespace, store: StateStore) -> int:
    settings = store.load_settings()
    rec = _provision_session(
        store,
        session_name=args.session,
        explicit_project=args.project,
        gpu=args.gpu,
        gpu_count=args.count,
        cpus=args.cpus,
        machine_type=args.machine_type,
        disk_gb=args.disk_gb,
        disk_type=args.disk_type,
        spot=not args.no_spot,
        spot_action=args.spot_action,
        idle_minutes=args.idle,
        autodown=args.down,
        max_hours=args.max_hours,
        region=args.region or settings.get("default_region"),
        zone=args.zone,
        image=args.image,
        ssh_mode=args.ssh_mode or settings.get("default_ssh_mode", "direct"),
        no_external_ip=args.no_external_ip,
        allow_any_ip=args.allow_any_ip,
        service_account=args.service_account,
    )
    if args.bind:
        write_workspace_binding(
            pathlib.Path.cwd(),
            session_name=rec.name,
            project_id=rec.project,
            config_path=args.config if args.config else None,
        )
    _emit(rec.to_dict(), args.output == "json")
    return 0


def cmd_sync(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    opts = _transfer_opts_from_args(args)
    res = _call_sync(store, rec, args.local_dir, remote_dir=args.remote_dir, clean=args.clean, options=opts)
    store.append_event(rec.name, "sync", dict(res))
    _emit(res, args.output == "json")
    return 0


def _build_remote_exec_command(
    store: StateStore,
    rec: SessionRecord,
    *,
    file_path: str | None,
    code: str | None,
    cmd_tokens: list[str],
    script_args: list[str],
    transfer_opts: TransferOptions | None = None,
) -> str:
    if file_path:
        src = pathlib.Path(file_path).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"Script file not found: {src}")
        upload_info = _call_upload(store, rec, src, remote_path=f"/workspace/{src.name}", options=transfer_opts)
        remote_file = str(upload_info["remote_path"])
        quoted_args = " ".join(shlex.quote(a) for a in script_args)
        if src.suffix.lower() == ".ipynb":
            out_nb = f"/workspace/{src.stem}_output.ipynb"
            return (
                f"jupyter nbconvert --to notebook --execute {shlex.quote(remote_file)} "
                f"--output {shlex.quote(out_nb)}"
            )
        if src.suffix.lower() in (".sh", ".bash"):
            return f"bash {shlex.quote(remote_file)} {quoted_args}".strip()
        return f"python3 {shlex.quote(remote_file)} {quoted_args}".strip()

    if code is not None:
        return f"python3 -c {shlex.quote(code)}"

    if cmd_tokens:
        tokens = cmd_tokens[1:] if cmd_tokens and cmd_tokens[0] == "--" else cmd_tokens
        if len(tokens) == 1:
            return tokens[0]
        return " ".join(shlex.quote(t) for t in tokens)

    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        return f"python3 -c {shlex.quote(piped)}"

    raise ValueError("Provide '-f <script.py>', '-c <code>', piped stdin, or '-- <command>'.")


def _wait_for_remote_job(
    store: StateStore,
    rec: SessionRecord,
    *,
    job_ref: str | None,
    poll_interval: float = 4.0,
    timeout_s: int = 3600,
    from_offset: int = 0,
    as_json: bool = False,
) -> int:
    start_ts = time.monotonic()
    cur_offset = max(0, int(from_offset))
    start_offset = cur_offset
    collected_chunks: list[str] = []
    last_status: str | None = None
    consecutive_ssh_errors = 0
    final_job: dict[str, Any] | None = None

    while True:
        if (time.monotonic() - start_ts) > timeout_s:
            if as_json:
                _emit(
                    {
                        "ok": False,
                        "timed_out": True,
                        "job": final_job,
                        "start_offset": start_offset,
                        "next_offset": cur_offset,
                        "new_output": "".join(collected_chunks),
                        "elapsed_s": round(time.monotonic() - start_ts, 2),
                    },
                    True,
                )
            else:
                _log(f"Timed out waiting for job {job_ref or '(latest)'} after {timeout_s}s (next_offset={cur_offset}).")
            return 124

        parts = ["python3", "/opt/gcolab/job_runner.py", "poll", "--offset", str(cur_offset)]
        if job_ref:
            parts.extend(["--job-ref", str(job_ref)])
        remote_cmd = " ".join(shlex.quote(p) for p in parts)

        try:
            res = ssh_run_capture(store, rec, remote_cmd, timeout=35, connect_timeout=15)
        except Exception as exc:
            consecutive_ssh_errors += 1
            if consecutive_ssh_errors >= 5:
                raise TransportError(f"Lost SSH polling connection after 5 retries: {exc}") from exc
            time.sleep(min(10.0, 2.0 * consecutive_ssh_errors))
            continue

        if res.returncode == 102:
            if as_json:
                _emit({"ok": False, "error": "JOB_NOT_FOUND", "job_ref": job_ref}, True)
            else:
                _log(f"No matching job found for ref={job_ref!r} on {rec.name}.")
            return 102
        if res.returncode != 0:
            consecutive_ssh_errors += 1
            if consecutive_ssh_errors >= 5:
                err = res.stderr.decode("utf-8", errors="replace").strip()
                raise TransportError(f"Remote job poll failed (rc={res.returncode}): {err}")
            time.sleep(min(10.0, 2.0 * consecutive_ssh_errors))
            continue

        consecutive_ssh_errors = 0
        data = json.loads(res.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1])
        job = data["job"]
        final_job = job
        st = job.get("status")
        if st != last_status and not as_json:
            tag_label = f" tag={job['tag']}" if job.get("tag") else ""
            _log(f"Job #{job['job_id']}{tag_label} status={st}")
            last_status = st

        chunk = data.get("chunk") or ""
        if chunk:
            if as_json:
                collected_chunks.append(chunk)
            else:
                sys.stdout.write(chunk)
                sys.stdout.flush()
        cur_offset = int(data.get("next_offset", cur_offset))

        if data.get("done"):
            break
        if st != "RUNNING" and cur_offset >= int(data.get("log_size", cur_offset)):
            break
        time.sleep(max(0.5, float(poll_interval)))

    assert final_job is not None
    final_status = final_job.get("status")
    rc = int(final_job.get("exit_code") or 0) if final_status == "SUCCEEDED" else (
        103 if final_status == "CANCELLED" else int(final_job.get("exit_code") or 100)
    )
    if rc == 0 and final_status != "SUCCEEDED":
        rc = 100

    if as_json:
        _emit(
            {
                "ok": final_status == "SUCCEEDED",
                "job": final_job,
                "status": final_status,
                "exit_code": rc,
                "start_offset": start_offset,
                "next_offset": cur_offset,
                "new_output": "".join(collected_chunks),
                "elapsed_s": round(time.monotonic() - start_ts, 2),
            },
            True,
        )
    return rc


def cmd_exec(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    opts = _transfer_opts_from_args(args)
    if args.workdir:
        _call_sync(store, rec, args.workdir, remote_dir="/workspace", clean=False, options=opts)

    inner_cmd = _build_remote_exec_command(
        store,
        rec,
        file_path=args.file,
        code=args.code,
        cmd_tokens=args.cmd or [],
        script_args=args.script_args or [],
        transfer_opts=opts,
    )
    runner_parts = ["python3", "/opt/gcolab/job_runner.py", "submit", "--command", inner_cmd]
    if args.detach or args.wait:
        runner_parts.append("--detach")
    if args.tag:
        runner_parts.extend(["--tag", args.tag])
    if args.idempotency_key:
        runner_parts.extend(["--idempotency-key", args.idempotency_key])
    if args.retry_failed:
        runner_parts.append("--retry-failed")
    for env_kv in args.env or []:
        runner_parts.extend(["--env", env_kv])

    remote_invoke = " ".join(shlex.quote(p) for p in runner_parts)
    if args.detach or args.wait:
        res = ssh_run_capture(store, rec, remote_invoke, timeout=60)
        out = res.stdout.decode("utf-8", errors="replace").strip()
        store.append_event(
            rec.name,
            "exec_detach",
            {"command": inner_cmd, "tag": args.tag, "idempotency_key": args.idempotency_key, "rc": res.returncode},
        )
        if res.returncode != 0:
            print(res.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
            return res.returncode
        if args.wait:
            job_meta = json.loads(out.splitlines()[-1])
            return _wait_for_remote_job(
                store,
                rec,
                job_ref=str(job_meta["job_id"]),
                poll_interval=4.0,
                timeout_s=opts.max_duration,
                from_offset=0,
                as_json=(args.output == "json"),
            )
        print(out)
        return 0

    rc = ssh_run_stream(store, rec, remote_invoke)
    store.append_event(rec.name, "exec", {"command": inner_cmd, "tag": args.tag, "exit_code": rc})
    return rc


def _parse_download_pairs(pairs: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for pair in pairs:
        if ":" in pair:
            r_src, l_dst = pair.split(":", 1)
            parsed.append((r_src, l_dst))
        else:
            parsed.append((pair, "."))
    return parsed


def cmd_run(args: argparse.Namespace, store: StateStore) -> int:
    """One-shot 'provision -> sync -> execute -> verify deliverables -> destroy' workflow."""
    target_path = pathlib.Path(args.target).expanduser().resolve()
    if not target_path.exists():
        _log(f"Target file does not exist (aborting before VM allocation): {target_path}")
        return 2

    is_yaml = target_path.suffix.lower() in (".yaml", ".yml")
    spec = load_task_yaml(target_path) if is_yaml else TaskSpec()
    opts = _transfer_opts_from_args(args)

    eff_gpu = args.gpu or spec.gpu
    eff_count = args.count if args.count is not None else spec.gpu_count
    eff_cpus = args.cpus or spec.cpus
    eff_mt = args.machine_type or spec.machine_type
    eff_spot = False if args.no_spot else (True if args.spot else spec.spot)
    eff_disk_gb = args.disk_gb or spec.disk_gb
    eff_disk_type = args.disk_type or spec.disk_type
    eff_project = args.project or spec.project
    eff_region = args.region or spec.region
    eff_zone = args.zone or spec.zone
    eff_max_hours = args.max_hours if args.max_hours is not None else (spec.max_hours or 6.0)
    retain_mins = (
        args.retain_on_download_error_mins
        if args.retain_on_download_error_mins is not None
        else spec.retain_on_download_error_mins
    )
    sname = sanitize_session_name(args.session or spec.name or f"run-{secrets.token_hex(3)}")

    settings = store.load_settings()
    had_existing_session = sname in store.load_sessions()
    rec: SessionRecord | None = None
    exit_code = 1
    preserve_for_recovery = False
    required_artifacts: list[dict[str, Any]] = []
    required_failures: list[dict[str, str]] = []
    teardown_failed = False

    try:
        rec = _provision_session(
            store,
            session_name=sname,
            explicit_project=eff_project,
            gpu=eff_gpu,
            gpu_count=eff_count or 1,
            cpus=eff_cpus,
            machine_type=eff_mt,
            disk_gb=eff_disk_gb,
            disk_type=eff_disk_type,
            spot=eff_spot,
            spot_action="DELETE",
            idle_minutes=spec.idle_minutes,
            autodown=True,
            max_hours=eff_max_hours,
            region=eff_region or settings.get("default_region"),
            zone=eff_zone,
            image=args.image,
            ssh_mode=args.ssh_mode or settings.get("default_ssh_mode", "direct"),
            no_external_ip=args.no_external_ip,
            allow_any_ip=False,
            service_account=None,
        )

        eff_workdir = args.workdir or spec.workdir
        if eff_workdir:
            _log(f"Syncing workdir {eff_workdir} -> /workspace ...")
            sync_res = _call_sync(store, rec, eff_workdir, remote_dir="/workspace", clean=False, options=opts)
            store.append_event(rec.name, "sync", dict(sync_res))

        for remote_dst, local_src in spec.file_mounts.items():
            _log(f"Mounting {local_src} -> {remote_dst} ...")
            up_res = _call_upload(store, rec, local_src, remote_path=remote_dst, options=opts)
            store.append_event(rec.name, "upload", dict(up_res))

        merged_envs = [f"{k}={v}" for k, v in spec.envs.items()] + list(args.env or [])

        setup_ok = True
        if spec.setup:
            _log("Running setup commands...")
            setup_parts = ["python3", "/opt/gcolab/job_runner.py", "submit", "--tag", "setup", "--command", spec.setup]
            for kv in merged_envs:
                setup_parts.extend(["--env", kv])
            setup_rc = ssh_run_stream(store, rec, " ".join(shlex.quote(p) for p in setup_parts))
            if setup_rc != 0:
                _log(f"Setup failed with exit code {setup_rc}")
                exit_code = setup_rc
                setup_ok = False

        if setup_ok:
            main_cmd: str | None = None
            if is_yaml:
                if not spec.run:
                    _log("No 'run:' block in Task YAML; setup completed.")
                    exit_code = 0
                else:
                    main_cmd = spec.run
            else:
                main_cmd = _build_remote_exec_command(
                    store,
                    rec,
                    file_path=str(target_path),
                    code=None,
                    cmd_tokens=[],
                    script_args=args.target_args or [],
                    transfer_opts=opts,
                )

            if main_cmd is not None:
                _log("Executing workload...")
                run_parts = ["python3", "/opt/gcolab/job_runner.py", "submit", "--tag", "main", "--command", main_cmd]
                for kv in merged_envs:
                    run_parts.extend(["--env", kv])
                exit_code = ssh_run_stream(store, rec, " ".join(shlex.quote(p) for p in run_parts))
                store.append_event(rec.name, "run_complete", {"exit_code": exit_code})

            req_downloads = list(spec.outputs) + _parse_download_pairs(args.download or [])
            opt_downloads = list(spec.optional_outputs) + _parse_download_pairs(args.optional_download or [])

            for r_src, l_dst in req_downloads:
                try:
                    _log(f"Downloading required deliverable {r_src} -> {l_dst} ...")
                    dl_res = _call_download(store, rec, r_src, l_dst, options=opts)
                    if not dl_res.get("verified", True):
                        raise TransportError(f"Digest verification failed for {r_src}")
                    store.append_event(rec.name, "download", {"required": True, **dl_res})
                    required_artifacts.append(dl_res)
                except Exception as dl_exc:
                    required_failures.append({"remote_path": r_src, "local_path": l_dst, "error": str(dl_exc)})
                    store.append_event(
                        rec.name,
                        "download_failed",
                        {"required": True, "remote_path": r_src, "local_path": l_dst, "error": str(dl_exc)},
                    )
                    _log(f"ERROR: Required deliverable download failed for {r_src}: {dl_exc}")

            for r_src, l_dst in opt_downloads:
                try:
                    _log(f"Downloading optional artifact {r_src} -> {l_dst} ...")
                    dl_res = _call_download(store, rec, r_src, l_dst, options=opts)
                    store.append_event(rec.name, "download", {"required": False, **dl_res})
                except Exception as dl_exc:
                    _log(f"Note: optional artifact {r_src} skipped/unavailable: {dl_exc}")

            if exit_code == 0 and required_failures:
                exit_code = EXIT_ARTIFACT_DELIVERY_FAILED
                if not args.destroy_on_download_error and retain_mins > 0:
                    preserve_for_recovery = True
                    try:
                        _set_remote_autostop(
                            store,
                            rec,
                            idle_minutes=retain_mins,
                            autodown=True,
                            wait_for="jobs_and_ssh",
                        )
                    except Exception as auto_exc:
                        _log(f"Warning: could not update retention autostop timer: {auto_exc}")
                    failed_paths = ", ".join(f["remote_path"] for f in required_failures)
                    _log(
                        f"DELIVERABLE PROTECTION: Workload succeeded, but required artifact download failed ({failed_paths}). "
                        f"Preserving VM {rec.instance_name} for up to {retain_mins} idle min (--down armed) to prevent data loss.\n"
                        f"  Recover with: gcolab download -s {rec.name} <remote_path> <local_path> && gcolab down -s {rec.name}"
                    )
    finally:
        if rec is None and not had_existing_session:
            rec = store.load_sessions().get(sname)
        if rec is not None and not args.keep and not preserve_for_recovery:
            _log(f"Tearing down ephemeral VM {rec.instance_name} in {rec.zone}...")
            try:
                receipt = _teardown_and_build_receipt(
                    store,
                    rec,
                    required_artifacts=required_artifacts,
                    required_failures=required_failures,
                )
                _log(_format_receipt_text(receipt))
                if not receipt.get("teardown_verified", False):
                    teardown_failed = True
                    reasons = "; ".join(receipt.get("verification_failures") or ["cloud 404 verification incomplete"])
                    _log(
                        f"ERROR: Post-delete verification failed for {rec.instance_name} ({rec.zone}): {reasons}. "
                        f"Session '{rec.name}' retained in local state; run 'gcolab down -s {rec.name}' to verify or retry."
                    )
            except Exception as td_exc:
                teardown_failed = True
                rec.status = "TEARDOWN_FAILED"
                try:
                    store.upsert_session(rec)
                    store.append_event(
                        rec.name,
                        "teardown_failed",
                        {"instance_name": rec.instance_name, "zone": rec.zone, "error": str(td_exc)},
                    )
                except Exception:
                    pass
                _log(
                    f"ERROR tearing down {rec.instance_name} ({rec.zone}): {td_exc}. "
                    f"Session '{rec.name}' retained in local state; run 'gcolab down -s {rec.name}' to retry!"
                )
        elif rec is not None and args.keep:
            _log(f"Keeping session {rec.name!r} alive (--keep). Tear down with: gcolab down -s {rec.name}")

    if teardown_failed:
        return exit_code if exit_code != 0 else EXIT_TEARDOWN_FAILED
    return exit_code


def cmd_install(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    if args.requirements:
        req_p = pathlib.Path(args.requirements).expanduser().resolve()
        up = _call_upload(store, rec, req_p, remote_path=f"/workspace/{req_p.name}")
        r_req = shlex.quote(str(up["remote_path"]))
        install_sh = (
            f"(command -v uv >/dev/null 2>&1 && uv pip install --system -r {r_req}) "
            f"|| python3 -m pip install -r {r_req}"
        )
    elif args.packages:
        pkgs = " ".join(shlex.quote(p) for p in args.packages)
        install_sh = f"(command -v uv >/dev/null 2>&1 && uv pip install --system {pkgs}) || python3 -m pip install {pkgs}"
    else:
        raise ValueError("Specify packages or '-r requirements.txt'.")

    rc = ssh_run_stream(store, rec, install_sh)
    store.append_event(rec.name, "install", {"exit_code": rc})
    return rc


def cmd_queue(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    parts = ["python3", "/opt/gcolab/job_runner.py", "queue"]
    if args.tag:
        parts.extend(["--tag", args.tag])
    res = ssh_run_capture(store, rec, " ".join(shlex.quote(p) for p in parts), timeout=30)
    if res.returncode != 0:
        print(res.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        return res.returncode
    data = json.loads(res.stdout.decode("utf-8", errors="replace"))
    if args.output == "json":
        _emit(data, True)
    else:
        jobs = data.get("jobs") or []
        if not jobs:
            print(f"No jobs queued on {rec.name}.")
        for j in jobs:
            tag_disp = f"[{j['tag']}] " if j.get("tag") else ""
            print(
                f"#{j['job_id']:<4} {j['status']:<10} rc={str(j.get('exit_code')):<5} "
                f"{tag_disp}started={j.get('started_at')}  cmd={j.get('command')}"
            )
    return 0


def cmd_logs(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    ref = args.tag or (str(args.job_ref) if args.job_ref is not None else None)
    parts = ["python3", "/opt/gcolab/job_runner.py", "logs"]
    if ref is not None:
        parts.extend(["--job-ref", ref])
    if args.tail:
        parts.extend(["--tail", str(args.tail)])
    if args.status:
        parts.append("--status")
    remote_cmd = " ".join(shlex.quote(p) for p in parts)
    res = ssh_run_capture(store, rec, remote_cmd, timeout=60)
    if res.stdout:
        sys.stdout.write(res.stdout.decode("utf-8", errors="replace"))
    if res.stderr and not args.status:
        sys.stderr.write(res.stderr.decode("utf-8", errors="replace"))
    return res.returncode


def cmd_wait(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    ref = args.tag or (str(args.job_ref) if args.job_ref is not None else None)
    return _wait_for_remote_job(
        store,
        rec,
        job_ref=ref,
        poll_interval=args.poll,
        timeout_s=args.timeout,
        from_offset=args.from_offset,
        as_json=(args.output == "json"),
    )


def cmd_cancel(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    ref = args.tag or (str(args.job_ref) if args.job_ref is not None else None)
    if not ref:
        raise ValueError("Specify a job ID or '--tag <tag>' to cancel.")
    res = ssh_run_capture(
        store,
        rec,
        f"python3 /opt/gcolab/job_runner.py cancel --job-ref {shlex.quote(ref)}",
        timeout=30,
    )
    if res.stdout:
        print(res.stdout.decode("utf-8", errors="replace").strip())
    return res.returncode


def cmd_autostop(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    _set_remote_autostop(
        store,
        rec,
        idle_minutes=args.idle,
        autodown=args.down,
        wait_for=args.wait_for,
    )
    store.append_event(rec.name, "autostop_update", {"idle_minutes": args.idle, "autodown": args.down})
    _emit({"session": rec.name, "idle_minutes": args.idle, "autodown": args.down, "wait_for": args.wait_for}, args.output == "json")
    return 0


def cmd_upload(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    opts = _transfer_opts_from_args(args)
    res = _call_upload(store, rec, args.local_path, remote_path=args.remote_path, options=opts)
    store.append_event(rec.name, "upload", dict(res))
    _emit(res, args.output == "json")
    return 0


def cmd_download(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    opts = _transfer_opts_from_args(args)
    res = _call_download(store, rec, args.remote_path, local_dest=args.local_path, options=opts)
    store.append_event(rec.name, "download", dict(res))
    _emit(res, args.output == "json")
    return 0


def cmd_ls(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    target = shlex.quote(args.remote_path)
    return ssh_run_stream(store, rec, f"ls -la {target}")


def cmd_ssh(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    tokens = args.cmd or []
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    if not tokens:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Refusing interactive 'gcolab ssh' without a TTY (prevents AI agent hang). "
                "Pass '-- <command>' for one-shot shell commands."
            )
        alias = update_ssh_config_entry(store, rec)
        print(f"Connect interactively with:\n  ssh -F \"{store.ssh_config_path}\" {alias}")
        return 0
    cmd_str = tokens[0] if len(tokens) == 1 else " ".join(shlex.quote(t) for t in tokens)
    return ssh_run_stream(store, rec, f"cd /workspace && {cmd_str}")


def cmd_status(args: argparse.Namespace, store: StateStore) -> int:
    sessions = store.load_sessions()
    if args.session:
        key = sanitize_session_name(args.session)
        sessions = {k: v for k, v in sessions.items() if k == key}

    rows: list[dict[str, Any]] = []
    updates: dict[str, SessionRecord] = {}
    pruned_keys: set[str] = set()

    for key, rec in list(sessions.items()):
        try:
            client = ComputeClient(project_id=rec.project)
            inst = client.get_instance(rec.zone, rec.instance_name)
            ext_ip, int_ip = client.extract_ips(inst)
            rec.status = inst.get("status", rec.status)
            rec.provisioning_model = str(
                (inst.get("scheduling") or {}).get("provisioningModel") or rec.provisioning_model
            )
            rec.boot_disk_name = client.extract_boot_disk_name(inst, fallback=rec.boot_disk_name or rec.instance_name)
            rec.external_ip = ext_ip
            rec.internal_ip = int_ip
            updates[key] = rec
        except GCPApiError as exc:
            if exc.status_code == 404:
                rec.status = "GONE"
                if args.prune:
                    pruned_keys.add(key)
                    remove_ssh_config_entry(store, key)
                    clear_workspace_binding(only_if_session=key)
                    continue
                updates[key] = rec
        except Exception:
            pass
        rows.append(rec.to_dict())

    if updates or pruned_keys:
        def _apply(all_s: dict[str, SessionRecord]) -> None:
            for k, v in updates.items():
                if k in all_s:
                    all_s[k] = v
            for k in pruned_keys:
                all_s.pop(k, None)

        store.mutate_sessions(_apply)

    if args.output == "json":
        _emit({"sessions": rows}, True)
        return 0
    if not rows:
        print("No active gcolab sessions.")
        return 0
    for r in rows:
        gpu_tag = f"{r['gpu']}x{r['gpu_count']}" if r.get("gpu") else "CPU"
        mode_tag = r.get("provisioning_model") or ("SPOT" if r.get("spot") else "STD")
        print(
            f"{r['name']:<16} {r['status']:<12} {gpu_tag:<8} {r['machine_type']:<16} "
            f"{mode_tag:<8} {r['zone']:<18} ip={r.get('external_ip') or '-'}"
        )
    return 0


def cmd_stop(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    client = ComputeClient(project_id=rec.project)
    _log(f"Stopping {rec.instance_name} in {rec.zone} (boot disk preserved; storage charges still apply)...")
    inst = client.stop_instance(rec.zone, rec.instance_name)
    rec.status = inst.get("status", "TERMINATED")
    rec.external_ip = None
    store.upsert_session(rec)
    store.append_event(rec.name, "stop", {"status": rec.status})
    _emit(rec.to_dict(), args.output == "json")
    return 0


def cmd_start(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    rec = store.get_session(sname)
    client = ComputeClient(project_id=rec.project)
    _log(f"Starting {rec.instance_name} in {rec.zone}...")
    inst = client.start_instance(rec.zone, rec.instance_name)
    ext_ip, int_ip = client.extract_ips(inst)
    rec.status = inst.get("status", "RUNNING")
    rec.external_ip = ext_ip
    rec.internal_ip = int_ip
    store.upsert_session(rec)
    update_ssh_config_entry(store, rec)
    wait_for_vm_ready(store, rec, timeout_s=240)
    store.append_event(rec.name, "start", {"status": rec.status, "external_ip": ext_ip})
    _emit(rec.to_dict(), args.output == "json")
    return 0


def cmd_down(args: argparse.Namespace, store: StateStore) -> int:
    if args.all:
        targets = list(store.load_sessions().values())
    else:
        sname = _resolve_session_name(args, required=False)
        if not sname:
            raise ValueError("Pass '-s <session>', '--all', or run inside a bound workspace ('gcolab bind -s <name>').")
        targets = [store.get_session(sname)]

    deleted: list[str] = []
    failed_sessions: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for rec in targets:
        _log(f"Deleting {rec.instance_name} and verifying boot disk cleanup in {rec.zone}...")
        try:
            receipt = _teardown_and_build_receipt(store, rec)
            receipts.append(receipt)
            if receipt.get("teardown_verified", False):
                deleted.append(rec.name)
            else:
                failed_sessions.append(
                    {
                        "session": rec.name,
                        "instance_name": rec.instance_name,
                        "zone": rec.zone,
                        "reasons": receipt.get("verification_failures") or ["post-delete 404 verification incomplete"],
                    }
                )
        except Exception as exc:
            rec.status = "TEARDOWN_FAILED"
            store.upsert_session(rec)
            store.append_event(
                rec.name,
                "teardown_failed",
                {"instance_name": rec.instance_name, "zone": rec.zone, "error": str(exc)},
            )
            failed_sessions.append(
                {
                    "session": rec.name,
                    "instance_name": rec.instance_name,
                    "zone": rec.zone,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            _log(f"ERROR deleting {rec.instance_name} ({rec.zone}): {exc}")

    ok = len(failed_sessions) == 0
    if args.output == "json":
        _emit(
            {
                "ok": ok,
                "deleted_sessions": deleted,
                "failed_sessions": failed_sessions,
                "receipts": receipts,
            },
            True,
        )
    else:
        if not receipts and not failed_sessions:
            print("No sessions to tear down.")
        for r in receipts:
            print(_format_receipt_text(r))
        for f in failed_sessions:
            detail = f.get("error") or "; ".join(f.get("reasons") or [])
            print(
                f"TEARDOWN FAILED for session '{f['session']}' ({f['zone']}/{f['instance_name']}): {detail}",
                file=sys.stderr,
            )
    return 0 if ok else EXIT_TEARDOWN_FAILED


def cmd_cleanup(args: argparse.Namespace, store: StateStore) -> int:
    client, project_id = _resolve_client_and_project(store, args.project)
    managed = client.list_managed_instances()
    if args.dry_run or not args.apply:
        payload = {"project": project_id, "dry_run": True, "managed_instances": managed}
        _emit(payload, args.output == "json")
        return 0

    removed: list[str] = []
    failed_instances: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    verified_sessions: set[str] = set()
    blocked_sessions: set[str] = set()

    for item in managed:
        sess_slug = item.get("session")
        try:
            if hasattr(client, "delete_and_verify_instance"):
                ver = client.delete_and_verify_instance(
                    item["zone"],
                    item["name"],
                    session_name=None,
                )
                receipts.append(ver)
                reasons = _teardown_verification_failures(ver)
                if reasons:
                    if sess_slug:
                        blocked_sessions.add(sess_slug)
                    failed_instances.append(
                        {
                            "zone": item["zone"],
                            "name": item["name"],
                            "session": sess_slug,
                            "reasons": reasons,
                        }
                    )
                    continue
            else:
                client.delete_instance(item["zone"], item["name"])

            removed.append(f"{item['zone']}/{item['name']}")
            if sess_slug:
                verified_sessions.add(sess_slug)
        except Exception as exc:
            if sess_slug:
                blocked_sessions.add(sess_slug)
            failed_instances.append(
                {
                    "zone": item["zone"],
                    "name": item["name"],
                    "session": sess_slug,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            _log(f"ERROR cleaning up {item['zone']}/{item['name']}: {exc}")

    remaining_managed: list[dict[str, Any]] = []
    scan_error: str | None = None
    try:
        remaining_managed = client.list_managed_instances()
        for rem in remaining_managed:
            if rem.get("session"):
                blocked_sessions.add(str(rem["session"]))
    except GCPApiError as exc:
        scan_error = str(exc)

    if scan_error is None:
        for sess_slug in sorted(verified_sessions - blocked_sessions):
            store.remove_session(sess_slug)
            remove_ssh_config_entry(store, sess_slug)
            clear_workspace_binding(only_if_session=sess_slug)

    ok = (len(failed_instances) == 0) and (scan_error is None) and (len(remaining_managed) == 0)
    payload: dict[str, Any] = {
        "ok": ok,
        "project": project_id,
        "deleted_instances": removed,
        "failed_instances": failed_instances,
        "remaining_managed_instances": remaining_managed,
        "verifications": receipts,
    }
    if scan_error is not None:
        payload["scan_error"] = scan_error
    _emit(payload, args.output == "json")
    return 0 if ok else EXIT_TEARDOWN_FAILED


def cmd_log(args: argparse.Namespace, store: StateStore) -> int:
    sname = _resolve_session_name(args)
    assert sname is not None
    events = store.read_events(sname, limit=args.limit, event_type=args.type)
    _emit({"session": sname, "events": events}, args.output == "json")
    return 0


def cmd_skill(_args: argparse.Namespace, _store: StateStore) -> int:
    bundled = pathlib.Path(__file__).with_name("bundled_skill.md")
    repo_copy = pathlib.Path(__file__).resolve().parents[2] / "skills" / "gcp-colab-operator" / "SKILL.md"
    for p in (bundled, repo_copy):
        if p.exists():
            print(p.read_text(encoding="utf-8"))
            return 0
    print("SKILL.md not found in package.", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS, help="Target Google Cloud project ID")
    common.add_argument("--config", default=argparse.SUPPRESS, help="Custom sessions.json path for isolated agent runs")
    common.add_argument("-o", "--output", choices=("text", "json"), default=argparse.SUPPRESS, help="Output format")

    transfer_common = argparse.ArgumentParser(add_help=False)
    transfer_common.add_argument(
        "--method",
        choices=("auto", "scp", "tar", "stream"),
        default="auto",
        help="File transfer transport path (auto uses SCP for single files with resumable SSH stream fallback)",
    )
    transfer_common.add_argument("--connect-timeout", type=int, default=20, help="SSH/SCP connect timeout in seconds")
    transfer_common.add_argument("--stall-timeout", type=int, default=90, help="Abort attempt if 0 bytes move for N seconds")
    transfer_common.add_argument("--timeout", dest="max_duration", type=int, default=3600, help="Max transfer duration in seconds")
    transfer_common.add_argument("--retries", type=int, default=2, help="Automatic transfer retry count")
    transfer_common.add_argument("--no-progress", action="store_true", help="Suppress transfer stage/progress logs")

    parser = argparse.ArgumentParser(
        prog="gcolab",
        description="Native Windows Colab-like CLI & SkyPilot-style orchestrator for GCP Compute Engine.",
    )
    parser.add_argument("--project", default=None, help="Target Google Cloud project ID")
    parser.add_argument("--config", default=None, help="Custom sessions.json path for isolated agent runs")
    parser.add_argument("-o", "--output", choices=("text", "json"), default="text", help="Output format")
    parser.add_argument("-V", "--version", action="version", version=f"gcolab {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", parents=[common], help="Verify Windows OpenSSH, gcloud, ADC auth, and GCP project")

    p_cfg = sub.add_parser("config", parents=[common], help="View or set default project/region/ssh-mode")
    cfg_sub = p_cfg.add_subparsers(dest="config_action", required=True)
    cfg_sub.add_parser("show", parents=[common])
    p_cfg_set = cfg_sub.add_parser("set", parents=[common])
    p_cfg_set.add_argument("key", choices=("project", "region", "ssh-mode"))
    p_cfg_set.add_argument("value")

    p_bind = sub.add_parser("bind", parents=[common], help="Bind a project directory (.gcolab/project.json) to a session")
    p_bind.add_argument("-s", "--session", required=True, help="Session name to bind to this workspace")
    p_bind.add_argument("--workdir", default=".", help="Workspace directory to bind (default: current directory)")
    p_bind.add_argument("--force", action="store_true", help="Write binding even if session is not in local state yet")

    p_unbind = sub.add_parser("unbind", parents=[common], help="Remove workspace .gcolab/project.json session binding")
    p_unbind.add_argument("--workdir", default=".", help="Workspace directory to unbind")

    sub.add_parser("gpus", parents=[common], help="List supported GPU types, GCE machine shapes, and reference rates")

    p_cap = sub.add_parser("capacity", parents=[common], help="Query live GCP Spot capacity scores across regions")
    p_cap.add_argument("--gpu", default=None, help="GPU type (T4, L4, V100, A100, A100-80GB, H100)")
    p_cap.add_argument("--count", type=int, default=1)
    p_cap.add_argument("--cpus", type=int, default=None)
    p_cap.add_argument("--machine-type", default=None)
    p_cap.add_argument("--spot", action=argparse.BooleanOptionalAction, default=True)
    p_cap.add_argument("--regions", default=None, help="Comma-separated regions to query")

    p_new = sub.add_parser("new", parents=[common], help="Provision a new GCE session with cross-zone failover")
    p_new.add_argument("-s", "--session", required=True, help="Session name")
    p_new.add_argument("--gpu", default=None, help="GPU alias: T4, L4, V100, A100, A100-80GB, H100")
    p_new.add_argument("--count", type=int, default=1, help="Number of GPUs")
    p_new.add_argument("--cpus", type=int, default=None, help="vCPU count for CPU sessions (2,4,8,16,32)")
    p_new.add_argument("--machine-type", default=None, help="Explicit GCE machine type override")
    p_new.add_argument("--disk-gb", type=int, default=None)
    p_new.add_argument("--disk-type", default=None)
    p_new.add_argument("--no-spot", action="store_true", help="Provision standard on-demand VM instead of Spot")
    p_new.add_argument("--spot-action", choices=("DELETE", "STOP"), default="DELETE")
    p_new.add_argument("-i", "--idle", type=int, default=30, help="Idle autostop minutes (-1 disables)")
    p_new.add_argument("--down", action="store_true", help="Delete (instead of stop) when idle timer triggers")
    p_new.add_argument("--max-hours", type=float, default=None, help="Hardware maxRunDuration cutoff in hours")
    p_new.add_argument("--region", default=None)
    p_new.add_argument("--zone", default=None)
    p_new.add_argument("--image", default=None)
    p_new.add_argument("--ssh-mode", choices=("direct", "iap"), default=None)
    p_new.add_argument("--no-external-ip", action="store_true", help="Omit external IP (requires Cloud NAT + IAP)")
    p_new.add_argument("--allow-any-ip", action="store_true", help="Open port 22 to 0.0.0.0/0 instead of caller /32")
    p_new.add_argument("--service-account", default=None)
    p_new.add_argument("--bind", action="store_true", help="Bind current directory (.gcolab/project.json) to this session")

    p_sync = sub.add_parser("sync", parents=[common, transfer_common], help="Sync local directory to remote /workspace")
    p_sync.add_argument("-s", "--session", default=None, help="Session name (optional if bound via 'gcolab bind')")
    p_sync.add_argument("local_dir", nargs="?", default=".")
    p_sync.add_argument("--remote-dir", default="/workspace")
    p_sync.add_argument("--clean", action="store_true", help="Wipe remote_dir before extracting")

    p_exec = sub.add_parser("exec", parents=[common, transfer_common], help="Execute Python script, code, or shell command")
    p_exec.add_argument("-s", "--session", default=None, help="Session name (optional if bound via 'gcolab bind')")
    p_exec.add_argument("-f", "--file", default=None, help="Local .py, .sh, or .ipynb file to upload and run")
    p_exec.add_argument("-c", "--code", default=None, help="Inline Python snippet")
    p_exec.add_argument("--workdir", default=None, help="Sync local directory to /workspace before running")
    p_exec.add_argument("--env", action="append", default=[], help="Environment variable KEY=VALUE")
    p_exec.add_argument("--tag", default=None, help="Human-readable task alias/label (e.g. 'build-solvers')")
    p_exec.add_argument("--idempotency-key", default=None, help="Prevent duplicate execution across retries")
    p_exec.add_argument("--retry-failed", action="store_true", help="Re-run if matching idempotency-key previously FAILED")
    p_exec.add_argument("-d", "--detach", action="store_true", help="Run detached in background and print job JSON")
    p_exec.add_argument("--wait", action="store_true", help="Submit detached and stream incremental logs until completion")
    p_exec.add_argument("--arg", dest="script_args", action="append", default=[], help="Argument passed to -f script")
    p_exec.add_argument("cmd", nargs=argparse.REMAINDER, help="Command after '--'")

    p_run = sub.add_parser("run", parents=[common, transfer_common], help="One-shot: provision -> sync -> run -> verify outputs -> destroy")
    p_run.add_argument("-s", "--session", default=None, help="Optional session name")
    p_run.add_argument("--gpu", default=None)
    p_run.add_argument("--count", type=int, default=None)
    p_run.add_argument("--cpus", type=int, default=None)
    p_run.add_argument("--machine-type", default=None)
    p_run.add_argument("--disk-gb", type=int, default=None)
    p_run.add_argument("--disk-type", default=None)
    p_run.add_argument("--spot", action="store_true", default=False)
    p_run.add_argument("--no-spot", action="store_true", default=False)
    p_run.add_argument("--keep", action="store_true", help="Do not tear down the VM after completion")
    p_run.add_argument("--workdir", default=None, help="Local workdir to sync before running")
    p_run.add_argument("--env", action="append", default=[])
    p_run.add_argument(
        "--download",
        action="append",
        default=[],
        help="Required artifact REMOTE[:LOCAL] verified via SHA-256 before task succeeds",
    )
    p_run.add_argument(
        "--optional-download",
        action="append",
        default=[],
        help="Optional best-effort artifact REMOTE[:LOCAL] (missing/failed does not fail run)",
    )
    p_run.add_argument(
        "--retain-on-download-error-mins",
        type=int,
        default=None,
        help="Minutes to retain VM (with autodown armed) if workload succeeds but required download fails (default: 30)",
    )
    p_run.add_argument(
        "--destroy-on-download-error",
        action="store_true",
        help="Destroy VM immediately even if a required output download fails",
    )
    p_run.add_argument("--max-hours", type=float, default=None, help="Safety maxRunDuration hours (default: 6)")
    p_run.add_argument("--region", default=None)
    p_run.add_argument("--zone", default=None)
    p_run.add_argument("--image", default=None)
    p_run.add_argument("--ssh-mode", choices=("direct", "iap"), default=None)
    p_run.add_argument("--no-external-ip", action="store_true")
    p_run.add_argument("target", help="Path to local script (.py/.sh/.ipynb) or SkyPilot-style task.yaml")
    p_run.add_argument("target_args", nargs=argparse.REMAINDER)

    p_inst = sub.add_parser("install", parents=[common], help="Install Python packages on remote session")
    p_inst.add_argument("-s", "--session", default=None)
    p_inst.add_argument("-r", "--requirements", default=None)
    p_inst.add_argument("packages", nargs="*")

    p_q = sub.add_parser("queue", parents=[common], help="List remote job history and statuses")
    p_q.add_argument("-s", "--session", default=None)
    p_q.add_argument("--tag", default=None, help="Filter jobs by tag")

    p_logs = sub.add_parser("logs", parents=[common], help="Fetch job logs by numeric ID or tag, or exit with status code")
    p_logs.add_argument("-s", "--session", default=None)
    p_logs.add_argument("job_ref", nargs="?", default=None, help="Numeric job ID or task tag (omit for latest)")
    p_logs.add_argument("--tag", default=None, help="Select latest job matching tag")
    p_logs.add_argument("--tail", type=int, default=0)
    p_logs.add_argument("--status", action="store_true")

    p_wait = sub.add_parser("wait", parents=[common], help="Wait for job completion while streaming only incremental logs")
    p_wait.add_argument("-s", "--session", default=None)
    p_wait.add_argument("job_ref", nargs="?", default=None, help="Numeric job ID or task tag (omit for latest)")
    p_wait.add_argument("--tag", default=None, help="Wait for latest job matching tag")
    p_wait.add_argument("--poll", type=float, default=4.0, help="Poll interval in seconds")
    p_wait.add_argument("--timeout", type=int, default=3600, help="Maximum wait time in seconds")
    p_wait.add_argument("--from-offset", type=int, default=0, help="Byte offset in remote log to resume reading from")

    p_can = sub.add_parser("cancel", parents=[common], help="Cancel a running detached job by ID or tag")
    p_can.add_argument("-s", "--session", default=None)
    p_can.add_argument("job_ref", nargs="?", default=None, help="Numeric job ID or task tag")
    p_can.add_argument("--tag", default=None, help="Cancel latest job matching tag")

    p_auto = sub.add_parser("autostop", parents=[common], help="Update idle autostop/autodown timer on live VM")
    p_auto.add_argument("-s", "--session", default=None)
    p_auto.add_argument("-i", "--idle", type=int, required=True, help="Idle minutes (-1 disables)")
    p_auto.add_argument("--down", action="store_true")
    p_auto.add_argument("--wait-for", choices=("jobs_and_ssh", "jobs", "none"), default="jobs_and_ssh")

    p_up = sub.add_parser("upload", parents=[common, transfer_common], help="Upload local file or directory with SHA-256 staging")
    p_up.add_argument("-s", "--session", default=None)
    p_up.add_argument("local_path")
    p_up.add_argument("remote_path", nargs="?", default="/workspace")

    p_dl = sub.add_parser("download", parents=[common, transfer_common], help="Download remote file or directory with SHA-256 verification")
    p_dl.add_argument("-s", "--session", default=None)
    p_dl.add_argument("remote_path")
    p_dl.add_argument("local_path", nargs="?", default=".")

    p_ls = sub.add_parser("ls", parents=[common], help="List files on remote VM")
    p_ls.add_argument("-s", "--session", default=None)
    p_ls.add_argument("remote_path", nargs="?", default="/workspace")

    p_ssh = sub.add_parser("ssh", parents=[common], help="Run one-shot SSH command (-- cmd) or print SSH alias")
    p_ssh.add_argument("-s", "--session", default=None)
    p_ssh.add_argument("cmd", nargs=argparse.REMAINDER)

    for alias in ("status", "sessions"):
        p_st = sub.add_parser(alias, parents=[common], help="Show active sessions reconciled with live GCP state")
        p_st.add_argument("-s", "--session", default=None)
        p_st.add_argument("--prune", action="store_true", help="Remove entries whose GCE VM no longer exists")

    p_stop = sub.add_parser("stop", parents=[common], help="Stop GCE VM (preserves boot disk)")
    p_stop.add_argument("-s", "--session", default=None)

    p_start = sub.add_parser("start", parents=[common], help="Restart a stopped GCE VM")
    p_start.add_argument("-s", "--session", default=None)

    p_down = sub.add_parser("down", parents=[common], help="Delete GCE VM + boot disk and emit verification receipt")
    p_down.add_argument("-s", "--session", default=None)
    p_down.add_argument("--all", action="store_true", help="Tear down all tracked gcolab sessions")

    p_clean = sub.add_parser("cleanup", parents=[common], help="Scan GCP project for orphaned managed-by=gcolab VMs")
    p_clean.add_argument("--dry-run", action="store_true", default=False)
    p_clean.add_argument("--apply", action="store_true", default=False, help="Actually delete discovered gcolab VMs")

    p_log = sub.add_parser("log", parents=[common], help="Show local structured session audit history")
    p_log.add_argument("-s", "--session", default=None)
    p_log.add_argument("-n", "--limit", type=int, default=20)
    p_log.add_argument("-t", "--type", default=None)

    sub.add_parser("skill", parents=[common], help="Print bundled gcp-colab-operator SKILL.md")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    eff_config = args.config
    if eff_config is None:
        binding = find_workspace_binding()
        if binding and binding[1].get("config"):
            eff_config = str(binding[1]["config"])

    store = StateStore(sessions_file=eff_config)

    dispatch = {
        "doctor": cmd_doctor,
        "config": cmd_config,
        "bind": cmd_bind,
        "unbind": cmd_unbind,
        "gpus": cmd_gpus,
        "capacity": cmd_capacity,
        "new": cmd_new,
        "sync": cmd_sync,
        "exec": cmd_exec,
        "run": cmd_run,
        "install": cmd_install,
        "queue": cmd_queue,
        "logs": cmd_logs,
        "wait": cmd_wait,
        "cancel": cmd_cancel,
        "autostop": cmd_autostop,
        "upload": cmd_upload,
        "download": cmd_download,
        "ls": cmd_ls,
        "ssh": cmd_ssh,
        "status": cmd_status,
        "sessions": cmd_status,
        "stop": cmd_stop,
        "start": cmd_start,
        "down": cmd_down,
        "cleanup": cmd_cleanup,
        "log": cmd_log,
        "skill": cmd_skill,
    }

    try:
        return dispatch[args.command](args, store)
    except (
        AuthConfigError,
        SafetyIsolationError,
        GCPApiError,
        TransportError,
        TimeoutError,
        FileNotFoundError,
        KeyError,
        ValueError,
    ) as exc:
        if getattr(args, "output", "text") == "json":
            _emit({"ok": False, "error": type(exc).__name__, "message": str(exc)}, True)
        else:
            _log(f"ERROR ({type(exc).__name__}): {exc}")
        return 1
    except KeyboardInterrupt:
        _log("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
