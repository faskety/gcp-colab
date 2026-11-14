# `gcolab` — 跨平台 GCP 算力租用 CLI 与 Agent Skill

[English](README.md) | [简体中文](README.zh-CN.md)

通过 Python、OpenSSH 和 Google Cloud SDK，在 **Windows、macOS 或 Linux** 上管理 Google Cloud Compute Engine 的 CPU 与 GPU 会话。`gcolab` 提供类似 Colab 的会话工作流，支持跨可用区创建实例、带标签的任务队列、可恢复文件传输、产物交付校验和销毁收据。本地命令执行后即退出，空闲监控和自动关机由远端 Linux VM 负责。

## 支持的平台

| 本地系统 | OpenSSH 客户端 | Google Cloud CLI |
| --- | --- | --- |
| Windows | Windows OpenSSH Client（`ssh.exe`、`scp.exe`、`ssh-keygen.exe`） | `PATH` 中的 `gcloud.cmd` 或标准 SDK 安装目录 |
| macOS | 系统自带或 `PATH` 中的 OpenSSH | `PATH` 中的 `gcloud`、Homebrew 安装目录或 `~/google-cloud-sdk` |
| Linux | 发行版提供的 OpenSSH 客户端软件包 | `PATH` 中的 `gcloud`、系统 SDK 安装目录或 `~/google-cloud-sdk` |

三种本地系统使用相同的命令和 Task YAML 格式。创建的 VM 和远端命令运行在 Linux 上；从 macOS 或 Windows 工作时，需要为远端 Linux 目标编译本机二进制程序。

## 核心特性

- **可观测、可恢复、带摘要校验的双通道传输**：
  - 单文件优先走本机 OpenSSH 的 `scp` 客户端（SFTP 协议），异常或中断时自动切换为支持字节断点续传的 SSH 分块流（`stream`）；目录同步采用磁盘临时包流式传输，避免大文件占满内存。
  - 实时输出当前阶段（`pack` / `connect` / `send` / `receive` / `verify` / `commit`）、已传字节、速率与剩余时间；分离 `--connect-timeout`（连接超时）、`--stall-timeout`（传输无进展超时）与 `--timeout`（总时长上限）。
  - 上传与下载均先写入临时 `.part` 文件，核验字节数与 SHA-256 摘要一致后再原子落盘。
  - 上传保留 POSIX 文件权限，包括二进制程序和无扩展名脚本的可执行位；Shell 脚本设置为可执行，并将 CRLF 换行转换为远端 Linux 使用的格式。
- **产物安全取回纳入成功条件**：
  - `gcolab run` 区分必需产物（`--download` / YAML `outputs:`）与可选产物（`--optional-download` / YAML `optional_outputs:`）。
  - 主任务成功但必需产物未通过下载或 SHA-256 校验时，返回退出码 `3`，并保留 VM 供恢复产物。保留时长由 `--retain-on-download-error-mins` 控制，默认 `30` 分钟，同时启用自动销毁兜底。
- **项目级会话绑定与多 Agent 并发协调**：
  - 在项目目录执行 `gcolab new --bind` 或 `gcolab bind -s <session>` 会生成目录级 `.gcolab/project.json`，自动排除在 Git 与代码同步之外。同目录内后续命令可以使用绑定会话，无需反复传入 `-s`。
  - 本地 `sessions.json` 读写使用跨进程文件锁（`state.lock`），远端任务分配通过 `flock` 生成单调递增的任务 ID；支持 `--tag <任务标签>`、`--idempotency-key <幂等键>`，以及只拉取新增日志与状态变化的 `gcolab wait <标签或ID>`。
- **销毁校验与完整收据**：
  - `gcolab run`、`gcolab down` 与 `gcolab cleanup --apply` 等待删除操作完成，并独立向 GCP API 核验实例与启动盘均返回 `404`，且不存在同会话的残留托管实例。核验成功后才清理本地会话记录；销毁或核验失败时返回退出码 `4`，并保留会话供重试（`TEARDOWN_FAILED` / `TEARDOWN_INCOMPLETE`）。
  - 每次回收将核验状态、必需产物校验数、运行时长、参考费用估算及剩余资源写入 `~/.config/gcolab/history/<session>-receipt.json`。
- **托管资源隔离**：实例携带 `managed-by=gcolab` 标签；`stop`、`start`、`down` 和 `cleanup` 在修改资源前校验该标签，拒绝操作没有此标签的实例。

## 快速上手

下面的示例使用 Spot 实例。实际使用时，按任务的预计总费用选择 Spot 或 Standard；选择 Standard 时使用 `--no-spot`。

下面的单行命令适用于 PowerShell、bash 和 zsh。任务示例中的 `train.py` 或 `build.sh` 需要由你提供。

```sh
# 1. 检查环境、凭证与项目
gcolab doctor

# 2. 运行任务、校验必需产物并销毁 VM
gcolab run --gpu L4 --spot --workdir . --download /workspace/outputs:./outputs --optional-download /workspace/run.log:./run.log train.py --epochs 5

# 3. 为多阶段或多 Agent 会话绑定工作目录
gcolab new -s build1 --cpus 8 --idle 30 --down --max-hours 4 --bind
gcolab sync .
gcolab exec -d --tag project-build --idempotency-key build-v1 -- bash build.sh
gcolab wait project-build
gcolab download /workspace/evidence ./evidence -o json
gcolab down -o json
```

## 安装与测试

环境要求：Python 3.11 或更新版本、[`uv`](https://docs.astral.sh/uv/getting-started/installation/)、OpenSSH（`ssh`、`scp` 和 `ssh-keygen`），以及 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)。

```sh
git clone https://github.com/faskety/gcp-colab.git
cd gcp-colab

# 从仓库全局安装或更新 CLI
uv tool install --editable . --force

# 运行单元测试套件
uv run --extra dev pytest -v
```

安装配套 skill 时，将整个 `skills/gcp-colab-operator/` 目录复制到 Agent 的技能目录，例如 `~/.codex/skills/` 或 `~/.agents/skills/`。

[CI 工作流](.github/workflows/tests.yml) 在原生 Windows、macOS 和 Linux runner 上，使用 Python 3.11、3.13 执行测试并安装构建后的 wheel。原生检查覆盖 SSH 密钥生成、OpenSSH 配置解析、进程锁、模拟云端接口的文件传输及 POSIX 权限处理，不创建云资源。

## 仓库内容与本机配置

- `src/gcolab/`：CLI、云资源管理、文件传输、远端任务与本地会话管理源码。
- [`skills/gcp-colab-operator/`](skills/gcp-colab-operator/SKILL.md)：配套 Agent Skill，包含技能正文、参考文档和 `agents/openai.yaml`。
- `src/gcolab/bundled_skill.md`：CLI 内置的技能正文，与配套 `SKILL.md` 保持一致。
- `tests/`、`pyproject.toml`、`uv.lock`：本地测试、包配置和依赖锁定文件。

Google Cloud SDK、OpenSSH，以及 `gcloud`、`google-cloud-recipe-auth`、`google-cloud-recipe-onboarding` 等通用 Google Cloud skills 由各自的安装渠道维护。Agent 使用的 `gcp-colab-operator` 安装副本以本仓库 `skills/` 下的内容为准。

凭证通过应用默认凭证（ADC）、Google Cloud CLI 或 `GOOGLE_APPLICATION_CREDENTIALS` 指向的外部文件提供。本机项目与会话设置默认保存在 `~/.config/gcolab/`，工作目录绑定保存在 `.gcolab/`。

按照 Google 的 [ADC 路径规则](https://cloud.google.com/docs/authentication/application-default-credentials)，Windows 上的默认 ADC 文件为 `%APPDATA%\gcloud\application_default_credentials.json`，macOS 和 Linux 上为 `~/.config/gcloud/application_default_credentials.json`。`CLOUDSDK_CONFIG` 可指定 SDK 配置目录，`GCOLAB_GCLOUD_BIN` 可指定可执行文件，`GCOLAB_HOME` 可指定本机会话目录。在 macOS 和 Linux 上，SSH 私钥与新写入的会话设置使用仅当前用户可读写的权限。

`.gitignore` 排除环境变量文件、云凭证、SSH 密钥、个人设置、会话与销毁收据、日志、下载数据、虚拟环境和缓存。仓库内需要保存的个人配置集中放在 `private/`，本机覆盖文件使用 `*.local.*` 命名。可提交的 `.env.example`、`.env.template` 只填写占位符；提交前检查暂存内容，避免把真实账号、项目标识、服务器地址或密钥写入源码、文档和示例。

## README 语言版本

| 语言 | 文件 |
| --- | --- |
| English（默认） | [README.md](README.md) |
| 简体中文 | [README.zh-CN.md](README.zh-CN.md) |

其他语言使用 `README.<语言代码>.md` 命名，例如 `README.ja.md`。新增完整译文后，在每份 README 的语言切换入口和表格中加入链接。各语言版本的命令示例、选项名称、文件路径和功能说明应保持一致。
