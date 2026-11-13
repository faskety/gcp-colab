# `gcolab` Task YAML Specification

`gcolab run task.yaml` accepts a SkyPilot-compatible YAML schema tailored for Google Cloud Compute Engine. All fields are optional except `run` (or `setup`).

## Full Example

```yaml
name: batch-infer-l4

# Local directory synced to /workspace (respects .gitignore; never uploads .env, .gcolab, or credential JSONs)
workdir: .

resources:
  # SkyPilot-style accelerator syntax (<GPU>:<COUNT>) or plain alias (T4, L4, V100, A100, A100-80GB, H100)
  accelerators: L4:1
  # Example: cost comparison selects Spot; set false when Standard is cheaper and suitable
  use_spot: true
  disk_size: 120
  # Optional pinning (omit to let gcolab rank candidate GCP zones):
  # infra: gcp/us-central1
  # region: asia-east1
  # zone: asia-east1-a
  # instance_type: g2-standard-12
  idle_minutes: 30
  autodown: true
  max_hours: 4
  retain_on_download_error_mins: 30

envs:
  HF_HOME: /workspace/.cache/huggingface
  BATCH_SIZE: "8"

# Extra files outside workdir copied before setup/run with SHA-256 staging (REMOTE_PATH: LOCAL_PATH)
file_mounts:
  /workspace/configs/prod.yaml: ./configs/prod.yaml

# Required deliverables verified via SHA-256 before task counts as succeeded (REMOTE_PATH: LOCAL_PATH)
outputs:
  /workspace/outputs: ./outputs

# Optional best-effort artifacts (missing/failed does not fail the run)
optional_outputs:
  /workspace/logs/profile.json: ./logs/profile.json

setup: |
  pip install -r requirements.txt

run: |
  python3 infer.py --batch-size "$BATCH_SIZE" --out /workspace/outputs
```

## Field Reference

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `name` | `string` | `run-<hex>` | Session name slug (`gcolab-<name>-<id>` on GCE). |
| `workdir` | `string` | `null` | Relative (to YAML file) or absolute Windows folder synced to `/workspace`. |
| `resources.accelerators` | `string` | `null` | `<GPU>` or `<GPU>:<COUNT>`. Omit for CPU VM (`e2-standard-4` default, or set `resources.cpus`). |
| `resources.cpus` | `int` | `4` | CPU count (`2, 4, 8, 16, 32`) when no GPU is requested. |
| `resources.instance_type` | `string` | auto | Explicit GCE machine type (e.g. `g2-standard-16`, `n2-standard-16`, `c4-standard-8`). Gen 4 shapes auto-select `hyperdisk-balanced`. |
| `resources.use_spot` | `bool` | `true` | Set from the current total-cost comparison: `true` for Spot or `false` for Standard. Respect an explicit user choice and verify that live GCE `provisioningModel` matches. |
| `resources.disk_size` | `int` | `100` (GPU) / `60` (CPU) | Boot disk size in GB (`autoDelete: true` always enforced). |
| `resources.infra` | `string` | auto | `gcp/<region>` or `gcp/<region>/<zone>`. |
| `resources.max_hours` | `float` | `6.0` (`run`) | Hardware `maxRunDuration` deadline enforced by GCP Compute Engine (`E2` Standard uses VM-side autostop timer). |
| `resources.retain_on_download_error_mins` | `int` | `30` | Bounded idle retention window (with `--down` armed) if `run` exits `0` yet a required `outputs` download fails. |
| `envs` | `map<str,str>` | `{}` | Exported into both `setup` and `run` environments. |
| `file_mounts` | `map<str,str>` | `{}` | `remote_path: local_path` uploaded and SHA-256 verified prior to `setup`. |
| `outputs` | `map<str,str>` | `{}` | Required deliverables (`remote_path: local_path`) downloaded and SHA-256 verified before teardown. |
| `optional_outputs` | `map<str,str>` | `{}` | Optional best-effort artifacts (`remote_path: local_path`) collected without failing the run if absent. |
| `setup` | `string` | `null` | Shell script run before `run` in `/workspace`. |
| `run` | `string` | `null` | Main shell script run in `/workspace`; `gcolab run` exits `0` only when the script, required `outputs` SHA-256 verification (exit `3` on failure), and VM + boot disk `404` teardown verification (exit `4` on failure) all succeed. |
