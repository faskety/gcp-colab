"""Google Cloud Compute Engine v1 & Beta REST API client with strict gcolab safety guardrails."""

from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

from gcolab import (
    FIREWALL_RULE_SSH,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    NETWORK_TAG_SSH,
    SESSION_LABEL_KEY,
    __version__,
)
from gcolab.auth import ATTRIBUTION_TOKEN, get_access_token_and_adc_project
from gcolab.catalog import CPU_IMAGE_CANDIDATES, GPU_IMAGE_CANDIDATES
from gcolab.config import sanitize_session_name

COMPUTE_V1 = "https://compute.googleapis.com/compute/v1"
COMPUTE_BETA = "https://compute.googleapis.com/compute/beta"
BILLING_V1 = "https://cloudbilling.googleapis.com/v1"
IAP_TCP_CIDR = "35.235.240.0/20"

STOCKOUT_CODES = frozenset(
    {
        "ZONE_RESOURCE_POOL_EXHAUSTED",
        "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS",
        "STOCKOUT",
        "IP_SPACE_EXHAUSTED",
        "RESOURCE_POOL_EXHAUSTED",
        "UNSUPPORTED_OPERATION",
    }
)


class GCPApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class SafetyIsolationError(RuntimeError):
    """Raised when an operation attempts to touch a GCP resource not created by gcolab."""


class StockoutError(GCPApiError):
    """Raised when a zone lacks physical capacity for the requested VM shape."""


def detect_caller_public_ip(timeout: float = 4.0) -> str | None:
    for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                ip = resp.text.strip()
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                    return ip
        except Exception:
            continue
    return None


def build_scheduling_config(
    spot: bool,
    has_gpu: bool,
    spot_action: str = "DELETE",
    max_run_seconds: int | None = None,
    machine_type: str = "",
) -> dict[str, Any]:
    """Construct exact GCE v1 scheduling object (avoids legacy preemptible+SPOT 400 conflict)."""
    action = spot_action.upper()
    if action not in ("DELETE", "STOP"):
        raise ValueError(f"spot_action must be DELETE or STOP, got {spot_action!r}")

    mt = (machine_type or "").strip().lower()

    if spot:
        sched: dict[str, Any] = {
            "provisioningModel": "SPOT",
            "instanceTerminationAction": action,
            "automaticRestart": False,
            "onHostMaintenance": "TERMINATE",
        }
        if max_run_seconds and max_run_seconds > 0:
            sched["maxRunDuration"] = {"seconds": str(int(max_run_seconds))}
            sched["instanceTerminationAction"] = "DELETE"
        return sched

    # GCE E2 Standard instances require onHostMaintenance=MIGRATE and reject GCE maxRunDuration;
    # VM-side gcolab-autostop.timer enforces idle shutdown/deletion on E2 Standard shapes.
    if mt.startswith("e2-") and not has_gpu:
        return {
            "provisioningModel": "STANDARD",
            "automaticRestart": True,
            "onHostMaintenance": "MIGRATE",
        }

    if max_run_seconds and max_run_seconds > 0:
        return {
            "provisioningModel": "STANDARD",
            "automaticRestart": False,
            "onHostMaintenance": "TERMINATE",
            "instanceTerminationAction": "DELETE",
            "maxRunDuration": {"seconds": str(int(max_run_seconds))},
        }

    if has_gpu:
        return {
            "provisioningModel": "STANDARD",
            "automaticRestart": True,
            "onHostMaintenance": "TERMINATE",
        }

    return {
        "provisioningModel": "STANDARD",
        "automaticRestart": True,
        "onHostMaintenance": "MIGRATE",
    }


def verify_managed_instance_dict(instance: dict[str, Any], instance_name: str = "") -> None:
    labels = instance.get("labels") or {}
    if labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE:
        target = instance_name or instance.get("name") or "<unknown>"
        raise SafetyIsolationError(
            f"SAFETY BLOCK: Refusing to modify or delete GCP instance {target!r} because it does "
            f"not carry label '{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}'. Existing / non-gcolab "
            "servers are strictly read-only."
        )


class ComputeClient:
    """Thin, deterministic Google Compute Engine REST client."""

    def __init__(self, project_id: str, session: requests.Session | None = None) -> None:
        self.project_id = project_id
        self.http = session or requests.Session()
        self._token: str | None = None
        self._token_ts: float = 0.0

    def _headers(self) -> dict[str, str]:
        now = time.time()
        if not self._token or (now - self._token_ts) > 1800:
            self._token, _, _ = get_access_token_and_adc_project()
            self._token_ts = now
        ua = f"gcolab/{__version__}"
        if os.environ.get("GCOLAB_DISABLE_ATTRIBUTION") != "1":
            ua = f"{ua} {ATTRIBUTION_TOKEN}"
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "User-Agent": ua,
            "X-Goog-User-Project": self.project_id,
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int = 45,
    ) -> dict[str, Any]:
        resp = self.http.request(
            method=method,
            url=url,
            headers=self._headers(),
            params=params,
            json=json_body,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            reason = ""
            msg = resp.text
            try:
                err = resp.json().get("error", {})
                msg = err.get("message") or msg
                errs = err.get("errors") or []
                if errs:
                    reason = errs[0].get("reason", "")
            except Exception:
                pass
            raise GCPApiError(
                f"GCP API {method} {url} failed ({resp.status_code}): {msg}",
                status_code=resp.status_code,
                reason=reason,
            )
        if not resp.text.strip():
            return {}
        return resp.json()

    def check_project_and_billing(self) -> dict[str, Any]:
        info: dict[str, Any] = {"project": self.project_id, "compute_api_enabled": False, "billing_enabled": None}
        try:
            proj = self._request("GET", f"{COMPUTE_V1}/projects/{self.project_id}", timeout=20)
            info["compute_api_enabled"] = True
            info["default_service_account"] = proj.get("defaultServiceAccount")
        except GCPApiError as exc:
            info["compute_error"] = str(exc)

        try:
            bill = self._request("GET", f"{BILLING_V1}/projects/{self.project_id}/billingInfo", timeout=15)
            info["billing_enabled"] = bool(bill.get("billingEnabled"))
        except GCPApiError:
            info["billing_enabled"] = None
        return info

    def resolve_source_image(self, has_gpu: bool, explicit_image: str | None = None) -> str:
        if explicit_image:
            if explicit_image.startswith("projects/") or explicit_image.startswith("https://"):
                return explicit_image
            raise ValueError(
                "Custom --image must be a full GCP image URI such as "
                "'projects/deeplearning-platform-release/global/images/<name>'."
            )
        candidates = GPU_IMAGE_CANDIDATES if has_gpu else CPU_IMAGE_CANDIDATES
        last_err: Exception | None = None
        for img_proj, family in candidates:
            try:
                res = self._request(
                    "GET",
                    f"{COMPUTE_V1}/projects/{img_proj}/global/images/family/{family}",
                    timeout=20,
                )
                if res.get("selfLink"):
                    return str(res["selfLink"])
            except Exception as exc:
                last_err = exc
                continue
        raise GCPApiError(f"Could not resolve any base VM image from official families: {last_err}")

    def query_spot_capacity_advice(
        self,
        region: str,
        machine_type: str,
        size: int = 1,
    ) -> list[dict[str, Any]]:
        """Query Compute Engine Beta Spot Capacity Advice API (compute-spot-capacity-advice)."""
        url = f"{COMPUTE_BETA}/projects/{self.project_id}/regions/{region}/advice/capacity"
        body = {
            "provisioningModel": "SPOT",
            "distributionPolicy": {"targetShape": "ANY_SINGLE_ZONE"},
            "instanceFlexibilityPolicy": {
                "instanceSelections": {
                    "primary": {
                        "machineTypes": [machine_type],
                    }
                }
            },
            "size": size,
        }
        try:
            data = self._request("POST", url, json_body=body, timeout=25)
        except GCPApiError:
            return []

        results: list[dict[str, Any]] = []
        for rec in data.get("recommendations") or []:
            scores = rec.get("scores") or {}
            obtainability = float(scores.get("obtainability", 0.0))
            uptime_raw = str(scores.get("estimatedUptime", "0s")).rstrip("s")
            uptime_s = int(float(uptime_raw)) if uptime_raw.replace(".", "", 1).isdigit() else 0
            for shard in rec.get("shards") or []:
                zone_uri = str(shard.get("zone", ""))
                zone_name = zone_uri.rsplit("/", 1)[-1] if zone_uri else ""
                if zone_name:
                    results.append(
                        {
                            "region": region,
                            "zone": zone_name,
                            "machine_type": shard.get("machineType", machine_type),
                            "obtainability": obtainability,
                            "estimated_uptime_s": uptime_s,
                        }
                    )
        results.sort(key=lambda r: (r["obtainability"], r["estimated_uptime_s"]), reverse=True)
        return results

    def list_zones_for_region(self, region: str, gcp_accelerator: str | None = None) -> list[str]:
        if gcp_accelerator:
            data = self._request(
                "GET",
                f"{COMPUTE_V1}/projects/{self.project_id}/aggregated/acceleratorTypes",
                params={"filter": f"name={gcp_accelerator}"},
                timeout=25,
            )
            zones: list[str] = []
            for scope_key, scoped in (data.get("items") or {}).items():
                if not scope_key.startswith("zones/"):
                    continue
                zname = scope_key.split("/", 1)[1]
                if zname.startswith(f"{region}-") and scoped.get("acceleratorTypes"):
                    zones.append(zname)
            if zones:
                return sorted(zones)

        data = self._request(
            "GET",
            f"{COMPUTE_V1}/projects/{self.project_id}/zones",
            params={"filter": f"name:{region}-*"},
            timeout=20,
        )
        return sorted(
            item["name"]
            for item in (data.get("items") or [])
            if item.get("status") == "UP" and str(item.get("name", "")).startswith(f"{region}-")
        )

    def rank_candidate_zones(
        self,
        regions: list[str],
        machine_type: str,
        gcp_accelerator: str | None,
        spot: bool,
    ) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for region in regions:
            if spot:
                adv = self.query_spot_capacity_advice(region=region, machine_type=machine_type, size=1)
                if adv:
                    ranked.extend(adv)
                    continue
            try:
                for z in self.list_zones_for_region(region, gcp_accelerator=gcp_accelerator):
                    ranked.append(
                        {
                            "region": region,
                            "zone": z,
                            "machine_type": machine_type,
                            "obtainability": 0.5,
                            "estimated_uptime_s": 3600,
                        }
                    )
            except GCPApiError:
                continue

        if spot:
            ranked.sort(key=lambda item: (item["obtainability"], item["estimated_uptime_s"]), reverse=True)
        return ranked

    def ensure_ssh_firewall(self, allow_any_ip: bool = False, network: str = "global/networks/default") -> dict[str, Any]:
        source_ranges = [IAP_TCP_CIDR]
        if allow_any_ip:
            source_ranges = ["0.0.0.0/0"]
        else:
            caller_ip = detect_caller_public_ip()
            if caller_ip:
                source_ranges.append(f"{caller_ip}/32")

        fw_url = f"{COMPUTE_V1}/projects/{self.project_id}/global/firewalls/{FIREWALL_RULE_SSH}"
        try:
            existing = self._request("GET", fw_url, timeout=20)
            existing_ranges = set(existing.get("sourceRanges") or [])
            merged = sorted(existing_ranges.union(source_ranges)) if not allow_any_ip else ["0.0.0.0/0"]
            if set(merged) != existing_ranges:
                op = self._request(
                    "PATCH",
                    fw_url,
                    json_body={"sourceRanges": merged},
                    timeout=25,
                )
                self.wait_global_operation(op["name"])
            return {"firewall": FIREWALL_RULE_SSH, "source_ranges": merged}
        except GCPApiError as exc:
            if exc.status_code != 404:
                raise

        body = {
            "name": FIREWALL_RULE_SSH,
            "description": "Managed by gcolab: allow SSH to gcolab-ssh tagged VMs from caller IP and Google IAP.",
            "network": network if network.startswith("projects/") else f"projects/{self.project_id}/{network}",
            "direction": "INGRESS",
            "priority": 1000,
            "targetTags": [NETWORK_TAG_SSH],
            "sourceRanges": sorted(set(source_ranges)),
            "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
        }
        op = self._request(
            "POST",
            f"{COMPUTE_V1}/projects/{self.project_id}/global/firewalls",
            json_body=body,
            timeout=25,
        )
        self.wait_global_operation(op["name"])
        return {"firewall": FIREWALL_RULE_SSH, "source_ranges": body["sourceRanges"]}

    def wait_zone_operation(self, zone: str, op_name: str, timeout_s: int = 300) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        url = f"{COMPUTE_V1}/projects/{self.project_id}/zones/{zone}/operations/{op_name}"
        while time.time() < deadline:
            res = self._request("GET", url, timeout=25)
            if res.get("status") == "DONE":
                err = res.get("error", {}).get("errors") or []
                if err:
                    codes = {e.get("code", "") for e in err}
                    summary = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in err)
                    if codes & STOCKOUT_CODES:
                        raise StockoutError(summary, status_code=409, reason=next(iter(codes & STOCKOUT_CODES)))
                    raise GCPApiError(f"Operation {op_name} in {zone} failed: {summary}")
                return res
            time.sleep(2.5)
        raise TimeoutError(f"Timed out waiting for GCE zone operation {op_name} in {zone}")

    def wait_global_operation(self, op_name: str, timeout_s: int = 120) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        url = f"{COMPUTE_V1}/projects/{self.project_id}/global/operations/{op_name}"
        while time.time() < deadline:
            res = self._request("GET", url, timeout=20)
            if res.get("status") == "DONE":
                err = res.get("error", {}).get("errors") or []
                if err:
                    summary = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in err)
                    raise GCPApiError(f"Global operation {op_name} failed: {summary}")
                return res
            time.sleep(2.0)
        raise TimeoutError(f"Timed out waiting for global operation {op_name}")

    def build_instance_insert_body(
        self,
        *,
        instance_name: str,
        session_name: str,
        zone: str,
        machine_type: str,
        source_image: str,
        disk_gb: int,
        disk_type: str,
        gcp_accelerator: str | None,
        gpu_count: int,
        spot: bool,
        spot_action: str,
        max_run_seconds: int | None,
        ssh_user: str,
        ssh_pubkey: str,
        startup_script: str,
        enable_external_ip: bool = True,
        service_account_email: str | None = None,
    ) -> dict[str, Any]:
        nic: dict[str, Any] = {"network": "global/networks/default"}
        if enable_external_ip:
            nic["accessConfigs"] = [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}]

        metadata_items = [
            {"key": "enable-oslogin", "value": "FALSE"},
            {"key": "block-project-ssh-keys", "value": "true"},
            {"key": "ssh-keys", "value": f"{ssh_user}:{ssh_pubkey.strip()}"},
            {"key": "startup-script", "value": startup_script},
        ]
        if gcp_accelerator:
            metadata_items.append({"key": "install-nvidia-driver", "value": "True"})

        body: dict[str, Any] = {
            "name": instance_name,
            "machineType": f"zones/{zone}/machineTypes/{machine_type}",
            "labels": {
                MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
                SESSION_LABEL_KEY: sanitize_session_name(session_name),
            },
            "tags": {"items": [NETWORK_TAG_SSH]},
            "disks": [
                {
                    "boot": True,
                    "autoDelete": True,
                    "type": "PERSISTENT",
                    "initializeParams": {
                        "sourceImage": source_image,
                        "diskSizeGb": str(int(disk_gb)),
                        "diskType": f"zones/{zone}/diskTypes/{disk_type}",
                    },
                }
            ],
            "networkInterfaces": [nic],
            "metadata": {"items": metadata_items},
            "scheduling": build_scheduling_config(
                spot=spot,
                has_gpu=bool(gcp_accelerator),
                spot_action=spot_action,
                max_run_seconds=max_run_seconds,
                machine_type=machine_type,
            ),
        }

        if gcp_accelerator and gpu_count > 0:
            body["guestAccelerators"] = [
                {
                    "acceleratorType": f"projects/{self.project_id}/zones/{zone}/acceleratorTypes/{gcp_accelerator}",
                    "acceleratorCount": int(gpu_count),
                }
            ]

        if service_account_email:
            body["serviceAccounts"] = [
                {
                    "email": service_account_email,
                    "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                }
            ]
        return body

    def create_instance_in_zone(self, zone: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            op = self._request(
                "POST",
                f"{COMPUTE_V1}/projects/{self.project_id}/zones/{zone}/instances",
                json_body=body,
                timeout=45,
            )
        except GCPApiError as exc:
            upper = str(exc).upper()
            if any(code in upper for code in STOCKOUT_CODES) or "DOES NOT HAVE ENOUGH RESOURCES" in upper:
                raise StockoutError(str(exc), status_code=exc.status_code, reason=exc.reason) from exc
            raise
        self.wait_zone_operation(zone, op["name"], timeout_s=360)
        return self.get_instance(zone, body["name"])

    def get_instance(self, zone: str, instance_name: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{COMPUTE_V1}/projects/{self.project_id}/zones/{zone}/instances/{instance_name}",
            timeout=20,
        )

    def get_disk(self, zone: str, disk_name: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{COMPUTE_V1}/projects/{self.project_id}/zones/{zone}/disks/{disk_name}",
            timeout=20,
        )

    @staticmethod
    def extract_boot_disk_name(instance: dict[str, Any], fallback: str = "") -> str:
        for disk in instance.get("disks") or []:
            if disk.get("boot"):
                src = str(disk.get("source") or "")
                if src:
                    return src.rsplit("/", 1)[-1]
                dname = str(disk.get("deviceName") or "")
                if dname and dname != "persistent-disk-0":
                    return dname
        return fallback or str(instance.get("name") or "")

    @staticmethod
    def extract_ips(instance: dict[str, Any]) -> tuple[str | None, str | None]:
        nics = instance.get("networkInterfaces") or []
        if not nics:
            return None, None
        internal_ip = nics[0].get("networkIP")
        acs = nics[0].get("accessConfigs") or []
        external_ip = acs[0].get("natIP") if acs else None
        return external_ip, internal_ip

    def stop_instance(self, zone: str, instance_name: str) -> dict[str, Any]:
        inst = self.get_instance(zone, instance_name)
        verify_managed_instance_dict(inst, instance_name)
        if inst.get("status") in ("TERMINATED", "STOPPED"):
            return inst
        op = self._request(
            "POST",
            f"{COMPUTE_V1}/projects/{self.project_id}/zones/{zone}/instances/{instance_name}/stop",
            timeout=30,
        )
        self.wait_zone_operation(zone, op["name"], timeout_s=300)
        return self.get_instance(zone, instance_name)

    def start_instance(self, zone: str, instance_name: str) -> dict[str, Any]:
        inst = self.get_instance(zone, instance_name)
        verify_managed_instance_dict(inst, instance_name)
        if inst.get("status") == "RUNNING":
            return inst
        op = self._request(
            "POST",
            f"{COMPUTE_V1}/projects/{self.project_id}/zones/{zone}/instances/{instance_name}/start",
            timeout=30,
        )
        self.wait_zone_operation(zone, op["name"], timeout_s=300)
        return self.get_instance(zone, instance_name)

    def delete_instance(self, zone: str, instance_name: str) -> bool:
        try:
            inst = self.get_instance(zone, instance_name)
        except GCPApiError as exc:
            if exc.status_code == 404:
                return False
            raise
        verify_managed_instance_dict(inst, instance_name)
        op = self._request(
            "DELETE",
            f"{COMPUTE_V1}/projects/{self.project_id}/zones/{zone}/instances/{instance_name}",
            timeout=30,
        )
        self.wait_zone_operation(zone, op["name"], timeout_s=300)
        return True

    def delete_and_verify_instance(
        self,
        zone: str,
        instance_name: str,
        *,
        session_name: str | None = None,
        boot_disk_name: str | None = None,
    ) -> dict[str, Any]:
        """Delete a managed instance and independently confirm both VM and boot disk return 404."""
        was_present = True
        pre_meta: dict[str, Any] = {}
        resolved_disk = boot_disk_name or instance_name
        try:
            inst = self.get_instance(zone, instance_name)
            verify_managed_instance_dict(inst, instance_name)
            resolved_disk = self.extract_boot_disk_name(inst, fallback=resolved_disk)
            pre_meta = {
                "creation_timestamp": inst.get("creationTimestamp"),
                "provisioning_model": (inst.get("scheduling") or {}).get("provisioningModel"),
                "pre_delete_status": inst.get("status"),
            }
        except GCPApiError as exc:
            if exc.status_code == 404:
                was_present = False
            else:
                raise

        if was_present:
            op = self._request(
                "DELETE",
                f"{COMPUTE_V1}/projects/{self.project_id}/zones/{zone}/instances/{instance_name}",
                timeout=30,
            )
            self.wait_zone_operation(zone, op["name"], timeout_s=300)

        instance_absent = False
        for _ in range(6):
            try:
                self.get_instance(zone, instance_name)
                time.sleep(1.5)
            except GCPApiError as exc:
                if exc.status_code == 404:
                    instance_absent = True
                    break
                raise

        boot_disk_absent = not bool(resolved_disk)
        if resolved_disk:
            for _ in range(8):
                try:
                    self.get_disk(zone, resolved_disk)
                    time.sleep(1.5)
                except GCPApiError as exc:
                    if exc.status_code == 404:
                        boot_disk_absent = True
                        break
                    raise

        remaining_session_resources: list[dict[str, Any]] = []
        session_scan_verified = True
        session_scan_error: str | None = None
        if session_name:
            slug = sanitize_session_name(session_name)
            try:
                for item in self.list_managed_instances():
                    if item.get("session") == slug:
                        remaining_session_resources.append(item)
            except GCPApiError as exc:
                session_scan_verified = False
                session_scan_error = str(exc)

        teardown_verified = bool(
            instance_absent
            and boot_disk_absent
            and session_scan_verified
            and not remaining_session_resources
        )

        result: dict[str, Any] = {
            "was_present": was_present,
            "instance_name": instance_name,
            "boot_disk_name": resolved_disk,
            "instance_absent": instance_absent,
            "boot_disk_absent": boot_disk_absent,
            "session_scan_verified": session_scan_verified,
            "remaining_session_resources": remaining_session_resources,
            "teardown_verified": teardown_verified,
            **pre_meta,
        }
        if session_scan_error is not None:
            result["session_scan_error"] = session_scan_error
        return result

    def list_managed_instances(self) -> list[dict[str, Any]]:
        """List ONLY instances labelled managed-by=gcolab across all zones."""
        data = self._request(
            "GET",
            f"{COMPUTE_V1}/projects/{self.project_id}/aggregated/instances",
            params={"filter": f"labels.{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}"},
            timeout=30,
        )
        found: list[dict[str, Any]] = []
        for scope_key, scoped in (data.get("items") or {}).items():
            if not scope_key.startswith("zones/"):
                continue
            zone = scope_key.split("/", 1)[1]
            for inst in scoped.get("instances") or []:
                labels = inst.get("labels") or {}
                if labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE:
                    continue
                ext_ip, int_ip = self.extract_ips(inst)
                found.append(
                    {
                        "name": inst.get("name"),
                        "session": labels.get(SESSION_LABEL_KEY),
                        "zone": zone,
                        "status": inst.get("status"),
                        "machine_type": str(inst.get("machineType", "")).rsplit("/", 1)[-1],
                        "provisioning_model": inst.get("scheduling", {}).get("provisioningModel"),
                        "external_ip": ext_ip,
                        "internal_ip": int_ip,
                        "creation_timestamp": inst.get("creationTimestamp"),
                    }
                )
        return found
