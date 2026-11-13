# Safety Guardrails, Networking & Plugin Routing

## 1. Non-Destructive Resource Isolation (`managed-by=gcolab`)

- Every GCE VM created by `gcolab` has labels `managed-by=gcolab` and `gcolab-session=<name>`.
- Before `stop`, `start`, `down`, or `cleanup --apply` calls the GCE mutation API, `gcolab` fetches the live instance metadata and verifies `labels["managed-by"] == "gcolab"`.
- Any pre-existing server in the user's GCP project (such as production services, proxies, or manually created VMs) will trigger `SafetyIsolationError` if targeted. **Never attempt to strip or bypass this check using raw `gcloud compute instances delete`.**

## 2. Four-Layer Anti-Runaway-Billing Protection & Deliverable Retention

1. **`autoDelete: true` on Boot Disks & Verified `404` Gate**: Prevents orphaned Persistent Disks / Hyperdisks after VM deletion; `gcolab run`, `gcolab down`, and `gcolab cleanup --apply` independently verify both VM `404` and boot disk `404` (exiting `4` and retaining local session tracking until verified).
2. **Spot Termination Action**: Defaults to `instanceTerminationAction: DELETE` so preempted Spot VMs do not leave billed disks behind.
3. **VM-Side `gcolab-autostop.timer`**: Checks every 60 seconds inside the VM once jobs, GPU utilization (<5%), and SSH sessions stay idle past `--idle` minutes. With `--down`, self-deletion requires a VM service account with deletion permission; the agent falls back to shutdown if deletion fails, so confirm disk cleanup via `gcolab down`.
4. **Hardware `maxRunDuration` (`--max-hours`)**: Enforced by Google Compute Engine on Spot VMs and compatible Standard families (`N1`, `N2`, `C2`, `C3`, `G2`, `A2`, `A3`). On `E2` Standard shapes (which require `onHostMaintenance: MIGRATE` on GCE), `gcolab` automatically sets `MIGRATE` and relies on the VM-side `gcolab-autostop.timer` (`--idle` + `--down`).
5. **Bounded Deliverable Protection on `gcolab run`**: If a workload exits `0` but a required `--download` / `outputs:` transfer fails, `gcolab run` returns exit code `3` and retains the VM for `--retain-on-download-error-mins` (default `30` min, with `--down` armed) so artifacts can be retrieved with `gcolab download` before deletion.

## 3. Networking, Resumable Transfer & SSH Host Keys on Windows

- **Default (`--ssh-mode direct` with Ephemeral External IP)**:
  - Gives the VM outbound internet access (`pip`, `uv`, `git`, Hugging Face) without requiring a paid Cloud NAT gateway.
  - Locks inbound TCP port `22` (`gcolab-allow-ssh` targeting tag `gcolab-ssh` only) to **your current `/32` public IP + Google IAP `35.235.240.0/20`** — never `0.0.0.0/0` unless `--allow-any-ip` is explicitly passed.
- **IAP Tunnel Mode (`--ssh-mode iap` or `--no-external-ip`)**:
  - Opens a temporary localhost TCP bridge via `gcloud compute start-iap-tunnel` and runs OpenSSH `scp.exe` / `ssh.exe` through IAP.
  - Note: `--no-external-ip` omits public NAT IPs entirely; outbound internet inside the VM (`pip install`, GitHub) requires **Cloud NAT** on the target VPC subnet.
- **Resumable Multi-Path Transfers & Stall Detection**:
  - Single-file uploads and downloads use Windows OpenSSH `scp.exe` (SFTP protocol) on the first attempt and automatically switch to resumable byte-offset SSH chunked streaming (`stream`) on retry or partial resume.
  - Directory `sync` and directory `download` stream disk-backed `.tar.gz` archives with separated `--connect-timeout` (`20s`), `--stall-timeout` (`90s` zero-progress watchdog), and `--timeout` (`3600s` ceiling), verifying SHA-256 before atomic rename/extraction.
- **Per-Instance SSH Host Key Pinning**:
  - `gcolab` pins `HostKeyAlias` in `~/.config/gcolab/known_hosts` to `gcolab-<project>-<zone>-<instance_name>` rather than the raw ephemeral IP, so recycled GCE public IPs across separate rentals do not cause false host-key collisions while preserving host-key verification for the lifetime of each VM.

## 4. Error Recovery & Coordination with `Google Cloud Developer` Plugin

| Symptom / Error | Root Cause | Action |
| :--- | :--- | :--- |
| `QUOTA_EXCEEDED` (e.g. `GPUS_ALL_REGIONS`, `NVIDIA_L4_GPUS`) | The required regional or global quota is insufficient. | Inspect the named quota and remaining capacity. Recalculate the total cost of feasible shapes, permitted regions, and Spot/Standard options that meet the task's needs, then choose the cheaper suitable option within the user's budget. Otherwise report the limiting quota and route a quota request through `https://console.cloud.google.com/iam-admin/quotas`. |
| `StockoutError` across all candidate zones | Temporary physical capacity exhaustion for the requested SKU. | Inspect capacity and compare total cost across compatible zones, shapes, and provisioning models. Select the cheaper feasible alternative within the user's authorized budget and constraints; stop after the bounded retry set is exhausted. |
| `gcolab run` exits `3` (`DELIVERABLE PROTECTION`) | Workload succeeded (`0`), but a required `--download` / `outputs:` transfer failed. | Re-run `gcolab download -s <session> <remote> <local>` (add `--method stream` if SCP had an SFTP issue), verify the local files, and immediately run `gcolab down -s <session> -o json`. |
| `gcolab run` / `down` / `cleanup` exits `4` (`TEARDOWN FAILED`) | Delete API call failed or post-delete `404` check for the VM, boot disk, or remaining session scan did not pass. | Session remains tracked in `sessions.json` (`TEARDOWN_FAILED` / `TEARDOWN_INCOMPLETE`). Re-run `gcolab down -s <session> -o json` (or `gcolab cleanup --apply -o json`) and confirm `teardown_verified: true`. |
| `Compute Engine API has not been used in project ... before or it is disabled` | `compute.googleapis.com` not yet enabled on the project. | Follow the `gcloud` skill guardrail: **ask the user for explicit permission first**, then run `gcloud services enable compute.googleapis.com --project=<PROJECT_ID> --quiet`. |
| Missing project / unlinked billing | First-time GCP setup. | Activate `google-cloud-recipe-onboarding`. |
| Service Account / IAM permission denied (`403`) | Missing `roles/compute.instanceAdmin.v1` or `roles/iap.tunnelResourceAccessor`. | Activate `google-cloud-recipe-auth` (never modify IAM policies autonomously without user authorization). |
