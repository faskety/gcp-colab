# `gcolab` — Cross-platform CLI and Agent Skill for GCP Compute

[English](README.md) | [简体中文](README.zh-CN.md)

Manage Google Cloud Compute Engine CPU and GPU sessions from **Windows, macOS, or Linux** with Python, OpenSSH, and the Google Cloud SDK. `gcolab` combines a Colab-like session workflow with cross-zone provisioning, tagged job queues, resumable file transfers, verified artifact delivery, and teardown receipts. Each command runs locally and exits; the remote Linux VM handles idle monitoring and automatic shutdown.

## Supported platforms

| Local system | OpenSSH client | Google Cloud CLI |
| --- | --- | --- |
| Windows | Windows OpenSSH Client (`ssh.exe`, `scp.exe`, `ssh-keygen.exe`) | `gcloud.cmd` on `PATH` or a standard SDK installation |
| macOS | System OpenSSH or an installation on `PATH` | `gcloud` on `PATH`, a Homebrew installation, or `~/google-cloud-sdk` |
| Linux | Your distribution's OpenSSH client package | `gcloud` on `PATH`, a system SDK installation, or `~/google-cloud-sdk` |

All three hosts use the same commands and Task YAML format. The provisioned VM and remote commands run on Linux. Compile native binaries for that remote Linux target when working from macOS or Windows.

## Features

- **Observable, resumable transfers with integrity checks**:
  - Single-file transfers start with the host's OpenSSH `scp` client using SFTP and automatically fall back to a resumable SSH byte stream (`stream`) after failures or interruptions. Directory transfers stream a temporary archive from disk to keep memory usage bounded.
  - Progress reports include the current stage (`pack` / `connect` / `send` / `receive` / `verify` / `commit`), bytes transferred, transfer rate, and estimated time remaining. Separate controls cover connection timeout (`--connect-timeout`), stalled transfers (`--stall-timeout`), and total transfer duration (`--timeout`).
  - Uploads and downloads use temporary `.part` files and verify the byte count and SHA-256 digest before committing the final file atomically.
  - Uploads preserve POSIX file permission bits, including executable binaries and scripts without filename extensions. Shell scripts are made executable and their CRLF line endings are normalized for the remote Linux environment.
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

The single-line commands below work in PowerShell, bash, and zsh. Provide your own `train.py` or `build.sh` for the workload examples.

```sh
# 1. Check the environment, credentials, and project
gcolab doctor

# 2. Run a workload, verify required outputs, and tear down the VM
gcolab run --gpu L4 --spot --workdir . --download /workspace/outputs:./outputs --optional-download /workspace/run.log:./run.log train.py --epochs 5

# 3. Bind a workspace for a multi-stage or multi-agent session
gcolab new -s build1 --cpus 8 --idle 30 --down --max-hours 4 --bind
gcolab sync .
gcolab exec -d --tag project-build --idempotency-key build-v1 -- bash build.sh
gcolab wait project-build
gcolab download /workspace/evidence ./evidence -o json
gcolab down -o json
```

## Installation and tests

Requirements: Python 3.11 or newer, [`uv`](https://docs.astral.sh/uv/getting-started/installation/), OpenSSH (`ssh`, `scp`, and `ssh-keygen`), and the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install).

```sh
git clone https://github.com/faskety/gcp-colab.git
cd gcp-colab

# Install or update the global CLI from the repository
uv tool install --editable . --force

# Run the unit test suite
uv run --extra dev pytest -v
```

Install the companion skill by copying the entire `skills/gcp-colab-operator/` directory into your agent's skill directory, such as `~/.codex/skills/` or `~/.agents/skills/`.

The [CI workflow](.github/workflows/tests.yml) runs the test suite and installs the built wheel on native Windows, macOS, and Linux runners using Python 3.11 and 3.13. Native checks exercise SSH key generation, OpenSSH configuration parsing, process locks, file transfers with mocked cloud endpoints, and POSIX permission handling. These checks do not provision cloud resources.

## Repository layout and local configuration

- `src/gcolab/`: CLI, cloud resource management, transfers, remote jobs, and local session management.
- [`skills/gcp-colab-operator/`](skills/gcp-colab-operator/SKILL.md): The companion Agent Skill, including its instructions, reference documentation, and `agents/openai.yaml`.
- `src/gcolab/bundled_skill.md`: The skill text bundled with the CLI, kept in sync with the companion `SKILL.md`.
- `tests/`, `pyproject.toml`, and `uv.lock`: Local tests, package configuration, and locked dependencies.

The Google Cloud SDK, OpenSSH, and general Google Cloud skills such as `gcloud`, `google-cloud-recipe-auth`, and `google-cloud-recipe-onboarding` are maintained through their own installation channels. Installed copies of `gcp-colab-operator` use this repository's `skills/` directory as their source.

Credentials are supplied through Application Default Credentials (ADC), the Google Cloud CLI, or an external file referenced by `GOOGLE_APPLICATION_CREDENTIALS`. Local project and session settings default to `~/.config/gcolab/`; workspace bindings are stored in `.gcolab/`.

Following Google's [ADC locations](https://cloud.google.com/docs/authentication/application-default-credentials), local ADC files default to `%APPDATA%\gcloud\application_default_credentials.json` on Windows and `~/.config/gcloud/application_default_credentials.json` on macOS and Linux. `CLOUDSDK_CONFIG` selects a custom SDK configuration directory. `GCOLAB_GCLOUD_BIN` selects a custom executable, and `GCOLAB_HOME` selects the local session directory. On macOS and Linux, private SSH keys and newly written session settings use owner-only permissions.

`.gitignore` excludes environment files, cloud credentials, SSH keys, personal settings, sessions and teardown receipts, logs, downloaded data, virtual environments, and caches. Keep personal configuration inside `private/` or use `*.local.*` filenames for local overrides. Committed `.env.example` and `.env.template` files must contain placeholders only. Review staged changes before committing to keep real accounts, project identifiers, server addresses, and secrets out of source code, documentation, and examples.

## README translations

| Language | File |
| --- | --- |
| English (default) | [README.md](README.md) |
| 简体中文 | [README.zh-CN.md](README.zh-CN.md) |

Additional translations use `README.<language-code>.md`, such as `README.ja.md`. Add each available translation to the language selector and table in every README. Keep command examples, option names, file paths, and documented behavior consistent across languages.
