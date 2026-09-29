# JevCodex

JevCodex 是一个基于 [OpenAI Codex CLI](https://github.com/openai/codex) 的非官方实验性分支。它在 Codex TUI 的**每个新用户回合开始前**调用 TypeSafe Jev，通过 OpenRouter Decisions API 在受限的模型与 reasoning effort 配置之间进行路由。

这个项目特别处理状态化路由中的一个问题：`keep_current` 与当前具体配置可能代表同一个最终动作。因此候选项会去重，Jev 返回的概率会按最终执行动作聚合，模型降级还需要通过额外的安全门控。

> [!IMPORTANT]
> 本项目不是 OpenAI、TypeSafe 或 OpenRouter 的官方产品。生成任务仍由你登录的 Codex 账户执行；`OPENROUTER_API_KEY` 只用于 Jev 路由决策。请勿把 API key、Codex 登录文件、个人配置或 `.env` 文件提交到仓库。

## Jev 路由功能

- 在每个新用户 turn 的原生开始边界路由，不打断正在运行的工具循环；
- 使用 OpenRouter 原生 Decisions API 和 `~typesafe/jev-latest`；
- 根据 Codex 当前可用模型动态构造 Luna/Sol + medium/high 配置；
- 保留 `keep_current`，同时移除与当前配置语义重复的候选项；
- 将 Jev 返回概率按最终执行动作聚合；
- 降级采用三取二门控：confidence ≥ `0.80`、被选动作 probability ≥ `0.65`、前两名 probability margin ≥ `0.25`；
- 上下文超过默认 20,000 token 时阻止降级，避免不经济的 prompt-cache 重建；
- Jev 超时、不可用或返回非法结果时 fail-open，继续使用当前配置；
- 支持 auto、shadow、手动暂停、固定 profile、路由解释和端到端 benchmark。

## 架构

```text
用户提交新 turn
       │
       ▼
Codex TUI 收集当前模型、effort、prompt 和上下文规模
       │
       ▼
持久 Python sidecar ──► OpenRouter Decisions API ──► Jev
       │
       ▼
候选去重 → 最终动作概率聚合 → 升/降级安全策略
       │
       ├── Jev 异常：保留当前配置（fail-open）
       │
       ▼
Codex 使用最终配置执行该 turn 及其内部工具循环
```

Rust TUI 负责生命周期、策略和模型应用；[`router`](./router) 中的 Python sidecar 负责调用 Jev，并通过 stdin/stdout 上的 NDJSON 与 TUI 通信。

## 快速开始

### 前置条件

- Rust 工具链和 Python 3.10+；
- 能够正常使用的 Codex 登录；
- 一个用于 Jev 决策的 OpenRouter API key。

完整的上游构建要求见 [`docs/install.md`](./docs/install.md)。

### 1. 克隆并安装 sidecar

```powershell
git clone https://github.com/Beginner-666/JevCodex.git
cd JevCodex
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .\router
```

macOS/Linux 使用 `source .venv/bin/activate` 激活虚拟环境。

### 2. 设置 OpenRouter key

```powershell
$env:OPENROUTER_API_KEY = "your-openrouter-api-key"
```

也可以参考 [`.env.example`](./.env.example) 管理本地环境变量。所有 `.env*` 文件默认被 Git 忽略，只有不含密钥的 `.env.example` 例外。

### 3. 配置自动路由

将 [`router/config.toml.example`](./router/config.toml.example) 中的 `[auto_router]` 配置合并到 `~/.codex/config.toml`，不要覆盖现有 Codex 登录和个人设置。

如果虚拟环境中的 Python 不在 Codex 启动时的 `PATH`，请只在本机配置中设置 `sidecar_command` 的绝对路径，不要把个人路径提交到仓库。

### 4. 构建和启动

```powershell
cd codex-rs
cargo build -p codex-cli --release
.\target\release\codex.exe
```

macOS/Linux 的二进制为 `./target/release/codex`。无需单独启动 sidecar；TUI 会启动并持有一个持久 sidecar 进程。

## TUI 命令

```text
/autoroute status
/autoroute on
/autoroute off
/autoroute auto
/autoroute explain
/autoroute pin luna medium
/autoroute pin luna high
/autoroute pin sol medium
/autoroute pin sol high
```

- `/autoroute explain` 只读取当前会话内最近一次决策，不会额外调用 Jev；
- 在 `/model` 中手动选择模型会暂停自动路由，`/autoroute auto` 可恢复；
- prompt 中明确指定模型的指令优先于 Jev；
- shadow 模式记录决策但不应用切换。

## 路由语义

如果当前配置是 `sol_medium`，`keep_current` 和显式选择 `sol_medium` 最终都会执行同一个动作：

```text
P(final = sol_medium)
  = P(keep_current) + P(sol_medium)
```

当前 profile 默认不会与 `keep_current` 同时作为语义重复候选项；策略层也会对响应中所有等价标签按最终动作合并概率。

当前实现是 **user-turn-level routing**：每个新用户 turn 路由一次。一个 turn 内部的多次模型调用和工具循环继续使用该 turn 开始时选定的配置；本项目目前不宣称实现逐内部模型调用的 step-level routing。

## 测试

```powershell
$env:PYTHONPATH = ".\router"
python -m unittest discover -s router\tests -v

cd codex-rs
cargo test -p codex-tui auto_router
```

## Benchmark

路由器提供路由层 benchmark，以及在隔离工作区运行多 turn 编码任务的端到端 benchmark。先用 dry-run 检查运行规模，不会发起 API 请求：

```powershell
$env:PYTHONPATH = ".\router"
python -m jev_codex_router.e2e_benchmark --codex <path-to-codex> --dry-run
```

实际运行：

```powershell
$env:OPENROUTER_API_KEY = "your-openrouter-api-key"
$env:PYTHONPATH = ".\router"
python -m jev_codex_router.e2e_benchmark `
  --codex <path-to-codex> `
  --output .\e2e-report.json `
  --artifacts .\e2e-artifacts
```

默认 quick suite 只有三个任务，是 smoke/pilot benchmark，不能用于证明长期成功率或成本优势。完整配置、断点恢复、单 case 重跑和 token 统计口径见 [`router/README.md`](./router/README.md)。

## 隐私与限制

- `OPENROUTER_API_KEY` 只从运行时环境读取，sidecar 不把它写入协议或报告；
- 默认记录 prompt hash 而不是完整 prompt，可选 preview 只在当前 TUI 内存中保留；
- benchmark artifacts 可能包含 prompt、命令输出、diff 和本机路径，未经审查不要发布；
- Jev、Luna、Sol 的服务版本、价格和延迟可能变化；
- `previous_turn_status`、diff、工具调用和失败计数尚未全部接入生产请求；
- 相同模型路径不保证产生相同 Agent 执行轨迹或 token；
- 本仓库不提交大型预编译二进制。

更多安全说明见 [`SECURITY.md`](./SECURITY.md)。

## 关键目录

```text
codex-rs/tui/src/auto_router/   路由生命周期、profile、协议和策略
router/                         Python Jev sidecar、测试与 benchmark
router/examples/e2e/            端到端 quick suite
```

本仓库的公开源码快照基于 OpenAI Codex commit `29f056c`。自动路由修改集中在上述目录、配置 schema 和 TUI turn 边界集成中；需要同步上游时，可添加 `https://github.com/openai/codex.git` 为 `upstream` 并按基准 commit 审阅差异。项目包含并修改 OpenAI Codex 源码，继续遵循仓库中的 [Apache License 2.0](./LICENSE)。

---

## Upstream Codex README

以下是上游 Codex 的基本说明。完整官方文档以 OpenAI Codex 仓库与官方文档网站为准。

<p align="center"><strong>Codex CLI</strong> is a coding agent from OpenAI that runs locally on your computer.
<p align="center">
  <img src="https://github.com/openai/codex/blob/main/.github/codex-cli-splash.png" alt="Codex CLI splash" width="80%" />
</p>
</br>
If you want Codex in your code editor (VS Code, Cursor, Windsurf), <a href="https://developers.openai.com/codex/ide">install in your IDE.</a>
</br>If you want the desktop app experience, run <code>codex app</code> or visit <a href="https://chatgpt.com/codex?app-landing-page=true">the Codex App page</a>.
</br>If you are looking for the <em>cloud-based agent</em> from OpenAI, <strong>Codex Web</strong>, go to <a href="https://chatgpt.com/codex">chatgpt.com/codex</a>.</p>

---

## Quickstart

### Installing and running Codex CLI

Run the following on Mac or Linux to install Codex CLI:

```shell
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Run the following on Windows to install Codex CLI:

```shell
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
```

The standalone installers download from `https://releases.openai.com/codex` by default and fall back to GitHub Releases if a metadata or asset download is unavailable. To force GitHub Releases, set `CODEX_INSTALLER_USE_RELEASES_OPENAI_COM` to `false` (`0` and `no` are also accepted):

```shell
curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_INSTALLER_USE_RELEASES_OPENAI_COM=false sh
```

```powershell
$env:CODEX_INSTALLER_USE_RELEASES_OPENAI_COM='false'; irm https://chatgpt.com/codex/install.ps1 | iex
```

Codex CLI can also be installed via the following package managers:

```shell
# Install using npm
npm install -g @openai/codex
```

```shell
# Install using Homebrew
brew install --cask codex
```

Then simply run `codex` to get started.

<details>
<summary>You can also go to the <a href="https://github.com/openai/codex/releases/latest">latest GitHub Release</a> and download the appropriate binary for your platform.</summary>

Each GitHub Release contains many executables, but in practice, you likely want one of these:

- macOS
  - Apple Silicon/arm64: `codex-aarch64-apple-darwin.tar.gz`
  - x86_64 (older Mac hardware): `codex-x86_64-apple-darwin.tar.gz`
- Linux
  - x86_64: `codex-x86_64-unknown-linux-musl.tar.gz`
  - arm64: `codex-aarch64-unknown-linux-musl.tar.gz`

Each archive contains a single entry with the platform baked into the name (e.g., `codex-x86_64-unknown-linux-musl`), so you likely want to rename it to `codex` after extracting it.

</details>

### Using Codex with your ChatGPT plan

Run `codex` and select **Sign in with ChatGPT**. We recommend signing into your ChatGPT account to use Codex as part of your Plus, Pro, Business, Edu, or Enterprise plan. [Learn more about what's included in your ChatGPT plan](https://help.openai.com/en/articles/11369540-codex-in-chatgpt).

You can also use Codex with an API key, but this requires [additional setup](https://developers.openai.com/codex/auth#sign-in-with-an-api-key).

## Docs

- [**Codex Documentation**](https://developers.openai.com/codex)
- [**Contributing**](./docs/contributing.md)
- [**Installing & building**](./docs/install.md)
- [**Open source fund**](./docs/open-source-fund.md)

This repository is licensed under the [Apache-2.0 License](LICENSE).
