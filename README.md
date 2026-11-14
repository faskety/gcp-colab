# `gcolab` — Native Windows CLI and Agent Skill for GCP Compute

[English](README.md) | [简体中文](README.zh-CN.md)

Manage Google Cloud Compute Engine CPU and GPU sessions directly from Windows with Python, Windows OpenSSH, and the Google Cloud SDK. `gcolab` combines a Colab-like session workflow with cross-zone provisioning, tagged job queues, resumable file transfers, verified artifact delivery, and teardown receipts. It runs without WSL or a persistent local daemon.

## Features

- **Observable, resumable transfers with integrity checks**:
  - Single-file transfers start with native Windows `scp.exe` using SFTP and automatically fall back to a resumable SSH byte stream (`stream`) after failures or interruptions. Directory transfers stream a temporary archive from disk to keep memory usage bounded.
  - Progress reports include the current stage (`pack` / `connect` / `send` / `receive` / `verify` / `commit`), bytes transferred, transfer rate, and estimated time remaining. Separate controls cover connection timeout (`--connect-timeout`), stalled transfers (`--stall-timeout`), and total transfer duration (`--timeout`).
  - Uploads and downloads use temporary `.part` files and verify the byte count and SHA-256 digest before committing the final file atomically.
- **Verified artifact delivery**:
  - `gcolab run` distinguishes required outputs (`--download` / YAML `outputs:`) from optional outputs (`--optional-download` / YAML `optional_outputs:`).
  - If a workload succeeds but a required output cannot be downloaded or fails SHA-256 verification, the command returns exit code `3`. The VM is retained for a bounded recovery period, controlled by `--retain-on-download-error-mins` (default: `30`), with automatic teardown armed so results can be recovered.
- **Workspace session binding and concurrent agent coordination**:
  - `gcolab new --bind` or `gcolab bind -s <session>` creates a workspace-level `.gcolab/project.json`, which is excluded from Git and code synchronization. Subsequent commands in that workspace can select the bound session without repeating `-s`.
  - Local `sessions.json` updates use an inter-process file lock (`state.lock`), and remote job allocation uses `flock` to assign monotonically increasing IDs. Jobs support `--tag <label>`, `--idempotency-key <key>`, and incremental logs and status updates through `gcolab wait <tag-or-id>`.
- **Verified teardown and receipts**:
  - `gcolab run`, `gcolab down`, and `gcolab cleanup --apply` wait for deletion and independently verify that both the VM and its boot disk return `404`, with no remaining managed instances for the session. Local session records are cleared only after verification succeeds. Deletion or verification failures return exit code `4` and retain the session for retry (`TEARDOWN_FAILED` / `TEARDOWN_INCOMPLETE`).
  - Each teardown writes a receipt to `~/.config/gcolab/history/<session>-receipt.json`, including verification status, required artifact verification counts, runtime, a reference cost estimate, and remaining resources.
- **Managed resource isolation**: Instances carry the `managed-by=gcolab` label. `stop`, `start`, `down`, and `cleanup` verify this label before changing resources and refuse to operate on instances without it.

## Quick start

The examples below use Spot instances. Choose Spot or Standard based on the estimated total cost for your workload; use `--no-spot` when selecting Standard.

```powershell
# 1. Check the environment, credentials, and project
gcolab doctor

# 2. Run a workload, verify required outputs, and tear down the VM
gcolab run --gpu L4 --spot --workdir . `
  --download /workspace/outputs:./outputs `
  --optional-download /workspace/run.log:./run.log `
  train.py --epochs 5

# 3. Bind a workspace for a multi-stage or multi-agent session
gcolab new -s build1 --cpus 8 --idle 30 --down --max-hours 4 --bind
gcolab sync .
gcolab exec -d --tag project-build --idempotency-key build-v1 -- bash build.sh
gcolab wait project-build
gcolab download /workspace/evidence ./evidence -o json
gcolab down -o json
```

## Installation and tests

Requirements: Python 3.11 or newer, `uv`, Windows OpenSSH, and the Google Cloud SDK.

```powershell
git clone https://github.com/faskety/gcp-colab.git
Set-Location gcp-colab

# Install or update the global CLI from the repository
uv tool install --editable . --force

# Run the unit test suite
uv run --extra dev pytest -v
```

## Repository layout and local configuration

- `src/gcolab/`: CLI, cloud resource management, transfers, remote jobs, and local session management.
- [`skills/gcp-colab-operator/`](skills/gcp-colab-operator/SKILL.md): The companion Agent Skill, including its instructions, reference documentation, and `agents/openai.yaml`.
- `src/gcolab/bundled_skill.md`: The skill text bundled with the CLI, kept in sync with the companion `SKILL.md`.
- `tests/`, `pyproject.toml`, and `uv.lock`: Local tests, package configuration, and locked dependencies.

The Google Cloud SDK, Windows OpenSSH, and general Google Cloud skills such as `gcloud`, `google-cloud-recipe-auth`, and `google-cloud-recipe-onboarding` are maintained through their own installation channels. Installed copies of `gcp-colab-operator` use this repository's `skills/` directory as their source.

Credentials are supplied through Application Default Credentials (ADC), the Google Cloud CLI, or an external file referenced by `GOOGLE_APPLICATION_CREDENTIALS`. Local project and session settings default to `~/.config/gcolab/`; workspace bindings are stored in `.gcolab/`.

`.gitignore` excludes environment files, cloud credentials, SSH keys, personal settings, sessions and teardown receipts, logs, downloaded data, virtual environments, and caches. Keep personal configuration inside `private/` or use `*.local.*` filenames for local overrides. Committed `.env.example` and `.env.template` files must contain placeholders only. Review staged changes before committing to keep real accounts, project identifiers, server addresses, and secrets out of source code, documentation, and examples.

## README translations

| Language | File |
| --- | --- |
| English (default) | [README.md](README.md) |
| 简体中文 | [README.zh-CN.md](README.zh-CN.md) |

Additional translations use `README.<language-code>.md`, such as `README.ja.md`. Add each available translation to the language selector and table in every README. Keep command examples, option names, file paths, and documented behavior consistent across languages.
