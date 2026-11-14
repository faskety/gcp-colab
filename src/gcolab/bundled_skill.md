---
name: gcp-colab-operator
description: >-
  Provision, sync, execute, monitor, and auto-destroy Google Cloud Compute Engine GPU/CPU servers from Windows, macOS, or Linux via the `gcolab` CLI, selecting Spot or Standard by estimated total cost. Use when the user wants to rent a GCP server or GPU (T4, L4, V100, A100, A100-80GB, H100), run Python/shell/notebook or SkyPilot-style Task YAML jobs on Google Cloud, sync local code directories, compare rental costs or capacity, or automatically tear down compute after runs. Hand off project creation, billing linkage, API enablement, and IAM policy administration to the `Google Cloud Developer` plugin skills (`google-cloud-recipe-onboarding`, `google-cloud-recipe-auth`, `gcloud`).
---

# GCP Colab Operator (`gcolab` — Windows, macOS, Linux)

Operate Google Cloud Compute Engine VMs with Colab-like simplicity and SkyPilot-style cross-zone failover, tagged job queues, SHA-256 deliverable verification, and idle autostop/autodown — **native Windows, macOS, and Linux clients** (Python + OpenSSH + Google Cloud SDK, with no background local daemon).

## Mental Model (Read First)

1. **Fire-and-forget CLI + VM-side systemd guard**: Each `gcolab` call authenticates via ADC (`google-auth` / `gcloud`), acquires an OS-level lock on `~/.config/gcolab/state.lock`, performs one operation, updates `sessions.json`, and exits. Inside every provisioned VM, `/opt/gcolab/daemon.py` (systemd timer) monitors running jobs, SSH sessions, and GPU utilization, stopping idle VMs even if the local laptop disconnects. With `--down`, it attempts self-deletion when the VM's service account permits it; use the GCE runtime deadline and verify deletion via `gcolab down`'s receipt.
2. **Default remote workspace is `/workspace`**: `gcolab sync`, `gcolab exec`, `gcolab run`, and `gcolab ssh -- <cmd>` execute in `/workspace`.
3. **Workspace-scoped session binding (`.gcolab/project.json`)**: `gcolab new --bind` or `gcolab bind -s <session>` binds the current project directory to a session (auto-ignored by git and `sync`) so multi-step or multi-agent commands inside that folder omit `-s` without risking cross-project global session collisions.
4. **Strict production isolation (`managed-by=gcolab`)**: Every resource created by `gcolab` carries GCP label `managed-by=gcolab`. `stop`, `start`, `down`, and `cleanup` verify this label on the live GCE API before mutating anything and **physically refuse to touch pre-existing/unmanaged GCP servers**.
5. **Never confuse consumer `colab` with `gcolab`**: `colab` bills personal Colab Compute Units; `gcolab` bills the target Google Cloud Project (`Compute Engine`).

## Provisioning Policy: Compare Total Cost

- For CPU and GPU rentals, including short login tests, **choose the feasible option with the lower estimated total cost**. Compare Spot and Standard for the same or equivalent hardware in permitted regions, respecting the user's budget, resource needs, timing, and availability requirements. Use Spot when it is cheaper and Standard when it is cheaper. Honor an explicit user choice of provisioning model.
- Check current prices from the provider's pricing source or account quote before provisioning. Include applicable discounts, estimated billed runtime (including startup), billing minimums, disk, external IP, and network charges. For work affected by preemption, include an evidence-based estimate of lost work and restart cost; state material uncertainty instead of inventing an interruption penalty. Keep a short login test's calculation proportionate to its small cost.
- Give a concise comparison with the pricing source, runtime assumption, and estimated total before choosing. The CLI's catalog, `capacity`, and teardown receipt hourly figures are reference estimates; verify current prices separately. When the task specifies a particular model, use current pricing to size that requested option appropriately.
- Set the CLI options to match the cost comparison: `gcolab new` selects Spot when `--no-spot` is omitted and Standard when it is supplied (`new` has no `--spot` flag). For `gcolab run`, pass `--spot` or `--no-spot`; set YAML `resources.use_spot` to the selected boolean. The CLI default is a technical default; make the rental decision from the calculation.
- If capacity, quota, or prices change, compare the feasible alternatives again and choose within the user's authorized scope and budget. Either provisioning model may be selected by that calculation. The CLI attempts at most eight zones per launch; bound retries and reconcile partially created resources so a single-server request has at most one live test VM.
- Verify that the live GCE instance's `scheduling.provisioningModel` matches the selected `SPOT` or `STANDARD` model (`gcolab new -o json` and `gcolab status -o json` report the live `provisioning_model`). The machine type name and local session flags are not a substitute for this cloud check.
- Use `--down` and a task-appropriate `--max-hours` for temporary rentals. Finish a successful login test by immediately deleting the VM; the timers provide a fallback and are not a reason to leave it running.

## Local Host Setup

- Use Python 3.11 or newer, `uv`, OpenSSH (`ssh`, `scp`, and `ssh-keygen`), and the Google Cloud CLI. Install from the repository with `uv tool install --editable . --force` on all three systems.
- Windows uses the native OpenSSH Client and `gcloud.cmd`. macOS and Linux use executables on `PATH`; SDK discovery also checks common Homebrew, system, and `~/google-cloud-sdk` locations. `GCOLAB_GCLOUD_BIN` can point to a custom executable.
- ADC defaults to `%APPDATA%/gcloud/application_default_credentials.json` on Windows and `~/.config/gcloud/application_default_credentials.json` on macOS/Linux. Respect `GOOGLE_APPLICATION_CREDENTIALS` and `CLOUDSDK_CONFIG` overrides. Local gcolab state remains in `~/.config/gcolab` unless `GCOLAB_HOME` is set.
- The examples use single-line commands valid in PowerShell, bash, and zsh. Match any additional shell syntax to the local shell. The remote VM runs Linux on every client platform, so build uploaded executables for Linux.

## Step 0: Preflight & Bootstrap

Before provisioning compute in a new session, run:

```sh
gcolab doctor -o json
```

- **`credentials_valid: true` and `compute_api_enabled: true`**: Ready — proceed directly to the user's task.
- **Missing credentials (`credentials_valid: false`)**: Ask the user to run `gcloud auth application-default login` (interactive browser sign-in; follow `google-cloud-recipe-auth`).
- **No active project (`resolved_project: null`)**: Ask which GCP Project ID to use, or inspect active projects via the `gcloud` skill, then save with `gcolab config set project <PROJECT_ID>`.
- **New GCP user / billing unlinked / Compute API disabled**: Chain to `google-cloud-recipe-onboarding` and `gcloud` skills (never enable APIs or link billing without explicit user approval).

## Core Workflows

### Rental / SSH Smoke Test

For a request to rent, log in, and immediately cancel, compare the cost of small CPU instances and use the cheaper suitable option without uploading the local project. The following example applies when the calculation selects Spot; add `--no-spot` when it selects Standard:

```sh
gcolab new --project <PROJECT_ID> -s <UNIQUE_SESSION> --machine-type <SELECTED_MACHINE_TYPE> --disk-gb 10 --disk-type pd-standard --region <SELECTED_REGION> --idle 5 --down --max-hours 0.25 -o json
gcolab ssh --project <PROJECT_ID> -s <UNIQUE_SESSION> -- "uname -srm && whoami && nproc && free -m && df -h /workspace && systemctl is-active gcolab-autostop.timer"
gcolab down --project <PROJECT_ID> -s <UNIQUE_SESSION> -o json
```

Choose a machine compatible with the selected provisioning model, boot disk, and runtime deadline. Use the user's chosen region when specified. Check `provisioning_model` in the live output before reporting success. Run teardown immediately after the SSH check, including when login fails. `gcolab down -o json` waits for the GCE delete operation and independently verifies `instance_absent: true` (`404`) and `boot_disk_absent: true` (`404`), saving a receipt under `~/.config/gcolab/history/<session>-receipt.json`. Ephemeral external IPs are released with the instance. Remove a firewall rule created solely for this test only after confirming that no other VM uses its target tag; preserve pre-existing shared rules. Report the actual provisioning model, login result, and cleanup receipt.

### 1. One-Shot Deliverable-Gated Jobs (`gcolab run` — Preferred for Self-Contained Runs)

Validates the local target *before* allocating a VM, provisions the selected Spot or Standard option with cross-zone failover, syncs `--workdir`, runs setup + script/YAML, verifies required deliverables via SHA-256 (`--download` / YAML `outputs:`), and tears down the VM + boot disk with a full verification receipt:

```sh
# Example when the cost comparison selects a Spot L4; use --no-spot for Standard
gcolab run --gpu L4 --spot --workdir . --download /workspace/outputs:./outputs --optional-download /workspace/debug.log:./debug.log train.py --epochs 5

# Run a SkyPilot-compatible Task YAML (supports setup:, envs:, file_mounts:, outputs:, optional_outputs:, run:)
gcolab run task.yaml
```

- **Deliverable safety gate**: `--download` (and YAML `outputs:`) are **required deliverables**. If the workload exits `0` yet a required download fails or mismatches SHA-256, `gcolab run` exits with code `3` and **retains the VM for `--retain-on-download-error-mins` (default `30` min, with `--down` armed)** instead of deleting unretrieved results, printing the exact recovery command. Pass `--optional-download` (or YAML `optional_outputs:`) for non-critical logs.
- **Teardown & `404` verification gate**: `gcolab run`, `gcolab down`, and `gcolab cleanup --apply` require confirmed `404` absence for both the GCE instance and its boot disk (plus zero remaining session-scoped managed VMs) before returning `0` or removing the session from `sessions.json`. Any delete exception or incomplete `404` check exits with code `4` and preserves the session entry (`TEARDOWN_FAILED` / `TEARDOWN_INCOMPLETE`) so `gcolab down -s <session>` can immediately retry.
- Add `--keep` only when the user wants to inspect or reuse the VM afterward. See [Task YAML Specification](./references/yaml-spec.md) for all supported YAML fields.

### 2. Interactive & Multi-Agent Sessions (`new --bind` -> `sync` -> `exec --tag` -> `wait` -> `down`)

Use when iterating across multiple stages, coordinating subagents, or running long detached builds/trainings. This example assumes the cost comparison selected Spot; add `--no-spot` on `capacity` and `new` when Standard is selected:

```sh
# 1. Check live GCP Spot capacity scores (hourly figures are reference estimates)
gcolab capacity --gpu L4 --spot -o json

# 2. Provision and bind the current workspace directory (.gcolab/project.json) so subsequent commands omit -s
gcolab new -s exp1 --gpu L4 --idle 30 --down --max-hours 4 --bind -o json

# 3. Sync workspace & upload/download files (stages via .part, verifies SHA-256, auto-switches SCP <-> resumable SSH stream)
gcolab sync . --stall-timeout 90 --retries 2
gcolab upload ./large_input.bin /workspace/large_input.bin --method auto

# 4. Install dependencies on the VM
gcolab install -r requirements.txt

# 5. Submit detached tasks with human-readable tags and idempotency keys (prevents duplicate launches on retry)
gcolab exec -d --tag solver-build --idempotency-key solver-build-v1 -- bash build_all.sh

# 6. Wait for completion by tag while streaming ONLY incremental new log output (survives transient SSH blips)
gcolab wait solver-build -o json
gcolab queue -o json
gcolab logs solver-build --tail 50

# 7. Download & verify deliverables, then tear down and inspect the verification receipt
gcolab download /workspace/evidence ./evidence -o json
gcolab down -o json
```

### 3. Transfer Controls, Shell Commands, Stop/Start, and Orphan Cleanup

- **Transfer tuning**: `sync`, `upload`, `download`, `exec`, and `run` accept `--method {auto,scp,stream}`, `--connect-timeout 20`, `--stall-timeout 90` (aborts stalled sockets without cutting off large active transfers), `--timeout 3600`, `--retries 2`, and `--no-progress`.
- **One-shot shell command**: `gcolab ssh -s exp1 -- "nvidia-smi && df -h /workspace"` (never invoke bare `gcolab ssh` without `-- <cmd>` from an agent).
- **Pause vs Destroy**: `gcolab stop -s exp1` stops compute billing while preserving the boot disk (disk GB-month storage still bills) and resumes via `gcolab start -s exp1`. `gcolab down -s exp1` deletes both the VM and its `autoDelete` boot disk and verifies both return `404`.
- **Multi-agent state safety**: Local `sessions.json` updates and remote job ID allocations are protected by OS file locks (`state.lock` / `flock`). When subagents operate completely separate sessions outside a shared bound workspace, pass `-s <session>` explicitly or `--config <scratch_sessions.json>`.
- **Audit orphaned VMs across all GCP zones**:
  ```sh
  gcolab cleanup --dry-run -o json
  ```

## Guardrails & Troubleshooting

Read [Safety, Networking & Plugin Routing](./references/safety-and-routing.md) whenever:
- A provision attempt hits `QUOTA_EXCEEDED`, `PERMISSION_DENIED`, or multi-region `StockoutError`.
- Designing deadlines across machine families (`E2` vs `N1`/`N2`/`G2`/`A2`/`A3`) or troubleshooting large transfers.
- The user needs private-IP-only VMs (`--no-external-ip` + `--ssh-mode iap` over Cloud NAT).
- You need to route IAM, VPC, Cloud Storage, or Billing tasks back to `Google Cloud Developer` skills.
