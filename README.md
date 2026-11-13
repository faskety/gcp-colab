# `gcolab` — 原生 Windows 版 GCP 算力租用 CLI 与 Agent Skill

不依赖 WSL、不跑本地常驻守护进程，直接在原生 Windows（Python + Windows 内置 OpenSSH + Google Cloud SDK）上提供 `google-colab-cli` 的会话手感与 `SkyPilot` 的跨可用区寻源、带标签任务队列、可恢复文件传输、产物交付门禁及完整销毁收据。

## 核心特性

- **可观测、可恢复、带摘要校验的双通道传输**：
  - 单文件优先走 Windows 原生 `scp.exe`（SFTP 协议），异常或中断时自动切换为支持字节断点续传的 SSH 分块流（`stream`）；目录同步采用磁盘临时包流式传输，避免大文件占满内存。
  - 实时输出当前阶段（`pack` / `connect` / `send` / `receive` / `verify` / `commit`）、已传字节、速率与剩余时间；分离 `--connect-timeout`（连接超时）、`--stall-timeout`（无进展卡死超时）与 `--timeout`（总时长上限）。
  - 所有上传与下载均先落临时 `.part` 文件，核验字节数与 `SHA-256` 一致后再原子落盘。
- **产物安全取回纳入成功条件**：
  - `gcolab run` 区分必需产物（`--download` / YAML `outputs:`）与可选产物（`--optional-download` / YAML `optional_outputs:`）。
  - 主任务成功但必需产物未通过下载或 `SHA-256` 校验时，返回退出码 `3`，并默认进入有期限保留状态（`--retain-on-download-error-mins 30` 且挂好 `--down` 自动销毁兜底），避免成果未取回就把机器删掉。
- **项目级会话绑定与多 Agent 并发协调**：
  - 支持在项目目录执行 `gcolab new --bind` 或 `gcolab bind -s <session>` 生成目录级 `.gcolab/project.json`（自动排除在 Git 与代码同步之外），同目录内后续命令无需反复手传 `-s`，也不会串到其他项目。
  - 本地 `sessions.json` 读写带跨进程文件锁（`state.lock`），远端任务分配带 `flock` 单调序号锁；支持 `--tag <任务标签>`、`--idempotency-key <幂等键>` 以及只拉取新增日志与状态变化的 `gcolab wait <标签或ID>`。
- **销毁与 `404` 核验纳入退出码门禁，完整出具销毁收据**：
  - `gcolab run`、`gcolab down` 与 `gcolab cleanup --apply` 不仅等待删除操作完成，还独立向 GCP API 核验实例 `404`、启动盘 `404` 且无剩余同会话托管资源后才算回收成功并清理本地会话记录；任一销毁报错或核验未通过均返回退出码 `4`，并将本地会话保留为待重试状态（`TEARDOWN_FAILED` / `TEARDOWN_INCOMPLETE`）。
  - 每次回收自动汇总核验状态、必需产物校验数、运行时长、参考费用估算及剩余资源，落盘至 `~/.config/gcolab/history/<session>-receipt.json`。
- **生产服务器物理隔离**：所有实例强制携带 `managed-by=gcolab` 标签；`stop` / `start` / `down` / `cleanup` 在调接口前强制校验该标签，拒绝触碰项目内任何非 `gcolab` 创建的服务器。

## 快速上手

```powershell
# 1. 环境、凭证与项目自检
gcolab doctor

# 2. 一键跑完即验即焚（必需产物校验通过才算成功，销毁后输出完整收据）
gcolab run --gpu L4 --spot --workdir . `
  --download /workspace/outputs:./outputs `
  --optional-download /workspace/run.log:./run.log `
  train.py --epochs 5

# 3. 项目级绑定与多阶段/多 Agent 协作会话
gcolab new -s build1 --cpus 8 --idle 30 --down --max-hours 4 --bind
gcolab sync .
gcolab exec -d --tag project-build --idempotency-key build-v1 -- bash build.sh
gcolab wait project-build
gcolab download /workspace/evidence ./evidence -o json
gcolab down -o json
```

## 安装与测试

```powershell
# 在仓库根目录执行，全局安装更新到 ~/.local/bin/gcolab.exe
uv tool install --editable . --force

# 运行单元测试套件
uv run --extra dev pytest -v
```

## 仓库内容与本机配置

- `src/gcolab/`：CLI、云资源管理、文件传输、远端任务与本地会话管理源码。
- `skills/gcp-colab-operator/`：配套 Agent Skill，包含 `SKILL.md`、参考文档和 `agents/openai.yaml`。
- `src/gcolab/bundled_skill.md`：CLI 内置的技能正文，与配套 `SKILL.md` 保持一致。
- `tests/`、`pyproject.toml`、`uv.lock`：本地测试、包配置和依赖锁定文件。

Google Cloud SDK、Windows OpenSSH，以及 `gcloud`、`google-cloud-recipe-auth`、`google-cloud-recipe-onboarding` 等通用 Google Cloud skills 由各自的安装渠道维护。Agent 使用的 `gcp-colab-operator` 安装副本以本仓库 `skills/` 下的内容为准。

凭证通过 ADC、Google Cloud CLI 或 `GOOGLE_APPLICATION_CREDENTIALS` 指向的外部文件提供。本机项目与会话设置默认保存在 `~/.config/gcolab/`，工作目录绑定保存在 `.gcolab/`。

`.gitignore` 排除环境变量文件、云凭证、SSH 密钥、个人设置、会话与销毁收据、日志、下载数据、虚拟环境和缓存。仓库内需要保存的个人配置集中放在 `private/`，本机覆盖文件使用 `*.local.*` 命名。可提交的 `.env.example`、`.env.template` 只填写占位符；提交前检查暂存内容，避免把真实账号、项目标识、服务器地址或密钥写入源码、文档和示例。
