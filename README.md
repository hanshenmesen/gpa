# GPA Replay Platform

> Record a real desktop workflow once, understand its intent, and safely replay
> it on another machine. / 录制一次真实桌面工作流，理解意图，并在另一台设备上安全复现。

[![CI](https://github.com/hanshenmesen/gpa/actions/workflows/ci.yml/badge.svg)](https://github.com/hanshenmesen/gpa/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-technical%20preview-7c5ce5.svg)](ROADMAP.md)

[Quick start](#safe-local-setup) · [Product design](DESIGN.md) ·
[Roadmap](ROADMAP.md) · [Contributing](CONTRIBUTING.md) ·
[Download](https://github.com/hanshenmesen/gpa/releases) ·
[macOS install guide](docs/install_macos_preview.md) ·
[Feedback program](docs/feedback_program.md) ·
[Report a bug](https://github.com/hanshenmesen/gpa/issues/new/choose) ·
[Join the discussion](https://github.com/hanshenmesen/gpa/discussions)

## Why GPA

Most desktop automation records coordinates. GPA records evidence, reconstructs
the user's goal, removes incidental actions, compares the destination machine,
and asks for fresh local approval before Replay. The result is a portable task
instead of a brittle macro.

GPA is designed for real work such as research collection, spreadsheet cleanup,
release audits and repeated browser/desktop operations. We are actively looking
for real workflows that fail, feel unsafe, or take too much manual effort. Your
failed reproduction is useful product evidence—not noise.

**Project status:** public technical preview. macOS is the verified execution
platform. Windows and Linux currently expose compatibility contracts but do not
yet provide equivalent native execution. GitHub release DMGs are unsigned and
intended for technical testers; source installation is the recommended path.

## Give feedback that changes the product

- Found a reproducible bug? Use the structured **Bug report** form.
- Have a real task GPA should reproduce? Submit a **Real-world workflow**.
- A Replay behaved differently on another machine? Submit a
  **Compatibility report** with redacted environment facts.
- Want to discuss product direction before filing work? Start a GitHub
  Discussion.

Never upload credentials, private screenshots, customer data, session cookies,
or unredacted recordings. See [SECURITY.md](SECURITY.md) for private reporting.

GPA is a local-first platform for recording desktop GUI workflows, turning
them into portable Replay plugins, inspecting their intent and permissions,
and running them through an explicit compatibility and safety preflight.

The project began as a reproduction of the Salesforce GUI Process Automation
paper (arXiv:2604.01676) and now adds Replay Studio, Replay Store, a live
Product Control Center, isolated run spaces, portable packages, and
cross-platform planning contracts.

The product direction is an installable desktop application with a Web-style
interface plus an independently operated cloud service. Accounts, community,
sync and coordination belong to the cloud; system permissions, recording and
desktop execution remain inside the signed application. See
`docs/desktop_cloud_product_plan.md` for the target boundaries and delivery plan.

The public preview now includes the first connected vertical slice: a signed-in
website user can pair a local Agent, see its heartbeat, save a catalog Replay,
send a short-lived compatibility-preflight proposal, and receive the result.
The proposal enters a local inbox and can only be imported as a non-executable
draft after confirmation on that device. Remote desktop execution is not
enabled by this connection.

> GPA can control the keyboard and pointer. Keep desktop automation disabled
> while browsing or inspecting untrusted Replay packages, and only arm a
> trusted Replay after reviewing its steps, variables, applications, and
> permissions.

## What it does

- Records mouse, keyboard, scrolling, clipboard, screenshot, and active-app
  context on macOS.
- Builds semantic workflow steps with configurable LLM assistance.
- Packages workflows as portable `.gpa-record.zip` Replay plugins.
- Keeps Store discovery and installation separate from desktop execution.
- Plans compatibility for macOS, Windows, and Linux and fails closed when a
  required capability is unavailable.
- Runs each Replay in an isolated Replay Space with its own plan, state, logs,
  artifacts, cancellation signal, and audit record.
- Executes deterministic semantic checkpoints (`wait`, `wait_for_text`,
  `assert_text`, `assert_url`, `set_clipboard`, and `assert_clipboard`) without
  spending model tokens, while reserving model calls for adaptive visual work.
- Aggregates real success rate, verified complexity, model cost, runtime health,
  and recent runs in `/control`.
- Exposes a local Web console, a CLI, and an MCP server.

## Replay lifecycle

```text
record -> preview -> parse intent -> save -> publish or install
  -> inspect -> plan -> preflight -> arm -> run -> stop or complete -> feedback
```

Installation never starts a Replay. Desktop input requires a local client
lease, a compatible plan, an expiring arm token, and enabled safety gates.

## Architecture

```text
Public website (ChatGPT sign-in + D1)
  account · library · catalog · devices · preflight proposals
                         ^
                         | outbound HTTPS, revocable device token
                         v
Desktop Agent (native WebView + loopback-only service)
  local inbox -> compatibility check -> local confirmation -> draft import
       |                |                     |
   recorder        environment gate      Replay engine
                                                |
                                      macOS platform adapter
```

The website cannot call the loopback API or mint local desktop authority. The
desktop Agent does not accept arbitrary cloud actions: the preview allowlist is
limited to `replay.prepare`, and recording or execution stays behind the
existing local lease, arm token, permission and emergency-stop gates.

The macOS execution path is the currently verified implementation. Windows
and Linux have compatibility planning contracts but still need native runner
implementations and end-to-end validation.

Structured model calls go through the `JSONLLMProvider` contract. The default
provider uses OpenAI-compatible Chat Completions; another hosted or local
provider can be injected without changing recording or execution code.

## Requirements

- Python 3.10 or newer
- macOS for recording and verified desktop replay
- Accessibility and Input Monitoring permission for the terminal that runs GPA
- An OpenAI-compatible API key for LLM-assisted build and recovery

The MCP server requires Python 3.10 or newer and MCP SDK 2.x.

Visual parsing is optional and downloads several larger local models.

## Safe local setup

For the packaged technical preview, download the latest GitHub prerelease and
follow the [macOS installation and checksum guide](docs/install_macos_preview.md).
The unsigned preview requires a one-time **Open Anyway** choice; never disable
Gatekeeper globally.

This installs the local development package without enabling desktop input:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
gpa-desktop
```

The native GPA window opens automatically. Use `gpa-web` only for legacy
browser-based development. Add `GPA_LLM_API_KEY` to `.env` before using
LLM-assisted workflow building or recovery.

To install the optional local visual stack:

```bash
python -m pip install -e ".[visual]"
gpa download-models
```

The launcher starts the desktop application in safe, lightweight mode by
default: no visual-model download and no desktop input.

```bash
./start.sh
./start.sh --check
```

For a trusted local replay session on macOS, opt in explicitly:

```bash
./start.sh --enable-desktop
./start.sh --enable-desktop --visual
./start.sh --web
```

Review the Replay plan before enabling desktop input. `--visual` installs and
preloads the larger local models; it is not required for Store browsing.

The maintained Store task `github_release_readiness_audit` is a real public
GitHub release-readiness audit rather than a synthetic case. It opens and
checks ten source files, verifies URL and clipboard postconditions, and then
visits the repository Actions page. Its current benchmark is 124/124 verified
steps in 96 seconds with zero model calls or model tokens, which is 4.28× the
previous 29-step real-task baseline.

## Build the macOS application

Install the desktop and packaging extras, then build the application and DMG:

```bash
python -m pip install -e ".[desktop,package]"
./scripts/build_macos_app.sh
```

The build writes `dist/GPA.app`, `artifacts/GPA-macOS.dmg`, and a SHA-256
checksum. Without `GPA_MACOS_SIGNING_IDENTITY` it is an ad-hoc-signed developer
artifact for local testing only. Public distribution additionally requires an
Apple Developer ID certificate and `GPA_MACOS_NOTARY_PROFILE`; the same script
then verifies Gatekeeper acceptance and submits with `notarytool`.

Run `gpa-release-preflight desktop` before a public build. Apple signing
credentials stay in Keychain and are never stored in the repository.

## Run GPA Cloud

The public API is a separate deployment and cannot import recording or desktop
execution drivers. It uses PostgreSQL, forced tenant row-level security,
checksummed migrations and an external OIDC identity provider:

```bash
python -m pip install -e ".[cloud]"
gpa-cloud-migrate
gpa-cloud
```

Use `gpa-release-preflight cloud` before deployment. Container configuration,
backup/restore commands and required secret names are documented in
`deploy/cloud/README.md`. The API validates asymmetric OIDC bearer tokens; GPA
does not implement or store user passwords.

## Model configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GPA_LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API endpoint |
| `GPA_LLM_MODEL` | `gpt-5.5` | Workflow analysis and replay decisions |
| `GPA_LLM_TEXT_MODEL` | `GPA_LLM_MODEL` | Optional model override for text-only workflow analysis |
| `GPA_LLM_VISION_MODEL` | `GPA_LLM_MODEL` | Optional model override for screenshot-aware replay decisions |
| `GPA_LLM_API_KEY` | unset | API credential |
| `GPA_STORAGE_DIR` | `storage` in a source checkout; OS user-data directory when installed | Shared Web, CLI, and MCP runtime data |
| `GPA_MODELS_CACHE_DIR` | `~/.cache/gpa/models` | Local model cache |
| `GPA_PORT` | `8765` | Loopback Web console port |
| `GPA_ENABLE_DESKTOP_AUTOMATION` | `0` | Permit trusted recording and Replay desktop input |
| `GPA_PROTECTED_INPUT_APPS` | `ChatGPT,Codex` | Comma-separated apps that must never receive automated keyboard or paste input |
| `GPA_ALLOW_PROTECTED_INPUT_APPS` | `0` | Explicit opt-in to target protected apps; keep disabled for normal use |
| `GPA_RECORDING_INPUT_BACKEND` | `quartz` on macOS, `pynput` elsewhere | Raw recording input capture; the launcher forces Quartz on macOS to avoid background TextInputSources translation |
| `GPA_PRELOAD_VISUAL_MODELS` | `0` | Warm local visual models at server start |
| `GPA_REQUIRE_VISUAL_WARMUP` | `0` | Require visual warmup before full startup |
| `GPA_REPLAY_AGENT_FIRST` | `0` | Prefer model decisions before recorded actions |
| `GPA_VERIFY_FINAL_STATE` | `GPA_REPLAY_AGENT_FIRST` | Verify and reconcile the visible business outcome; preflight Save/Submit to prevent duplicates |
| `GPA_UI_PARSE_CACHE_SIZE` | `16` | In-process screenshot graph cache entries (`0` disables it) |

Any OpenAI-compatible provider can be used without code changes. For example,
an API aggregator can use one economical model for recording analysis and a
stronger visual model only when a screenshot is attached:

```dotenv
GPA_LLM_API_KEY=your-local-key
GPA_LLM_BASE_URL=https://api.zhizengzeng.com/v1
GPA_LLM_MODEL=qwen3.7-plus
GPA_LLM_TEXT_MODEL=qwen3.7-plus
GPA_LLM_VISION_MODEL=qwen3.8-max
GPA_REPLAY_AGENT_FIRST=1
GPA_VERIFY_FINAL_STATE=1
```

Keep real credentials in `.env`, which is git-ignored; never commit them.

To compare live models against the same workflow-cleanup and screenshot safety
cases, run the credential-safe smoke benchmark (two requests per model):

```bash
GPA_LLM_BENCH_MODELS=qwen3.6-plus,qwen3.7-plus \
  .venv/bin/python scripts/benchmark_llm.py
```

Local visual parsing uses:

- `Salesforce/GPA-GUI-Detector` for GUI element detection
- `openai/clip-vit-base-patch32` for icon embeddings
- `intfloat/multilingual-e5-small` for text embeddings
- Apple Vision through `ocrmac`, with optional EasyOCR fallback

## CLI

```bash
gpa list
gpa show WORKFLOW
gpa record NAME
gpa run WORKFLOW --var key=value --metrics
gpa export WORKFLOW
gpa import PACKAGE
gpa download-models
gpa mcp-serve
gpa-desktop
gpa-cloud
gpa-cloud-migrate
gpa-release-preflight all
gpa-web
```

`gpa mcp-serve` is discovery-only by default. Running a Replay through MCP
requires both `GPA_ENABLE_DESKTOP_AUTOMATION=1` and the explicit command
`gpa mcp-serve --allow-execution`. MCP protocol output stays isolated on
stdout; launcher diagnostics are written to stderr.

## Project layout

```text
gpa/core/          screenshot parsing, UI graphs, matching, and precheck
gpa/recording/     event capture and workflow construction
gpa/execution/     safety gates, recovery, and desktop execution
gpa/replay/        Replay domain, intent, compatibility, and Spaces
gpa/community/     portable packages and local Store repository
gpa/storage/       workflow persistence and legacy-format compatibility
gpa/integration/   CLI, MCP server, and benchmarks
demo_web/          Replay Studio, Replay Store, Control Center, and local HTTP server
tests/             unit and local API regression tests
docs/              architecture and optimization notes
```

Replay 的可移植证据由共享服务统一生成：录制结束时保存主机、运行时、浏览器、屏幕与安全权限快照，发布时只补齐缺失的客户端字段；运行前再与目标主机比较。缺少平台身份时结论是 `unknown`，不会被当作兼容；Safe Web 与桌面 Replay 使用各自独立的运行门禁。

`gpa/replay/gate.py` 是最终复现决策的单一策略入口：它合并工作流质量、环境差异、录屏与理解契约以及 Safe Web 能力，生成稳定的 `decision_id`。浏览器页面必须携带自己的 `client_id` 完成心跳、授权与启动，多个页面不会互相覆盖环境或复用授权令牌。

录制构建采用“语义意图 + 确定性清理”两层策略。模型负责理解任务、合并同一目标上的低层动作并排除明显回退；确定性层会拒绝无原始事件支撑的动作、拆开跨目标或不可原子执行的合并，并把原始动作数、最终步骤数、合并数和噪声数写入 `provenance.recording_analysis`。

## Development

Install development tools and run the test suite:

```bash
python -m pip install -e ".[dev]"
python -m compileall -q gpa demo_web scripts
python -m ruff check .
node scripts/verify_frontend.js
python -m coverage run -m unittest discover -s tests -p "test_*.py"
python -m coverage report
python -m build
```

Tests must not emit real desktop input. CI runs with desktop automation
disabled, visual-model loading is replaced with test doubles, and total test
coverage may not fall below 60%. The floor is intentionally a regression gate;
it should rise as the remaining CLI and visual parser paths gain tests.

See [DESIGN.md](DESIGN.md) for product and interaction decisions and
[docs/replay_architecture.md](docs/replay_architecture.md) for the domain and
execution boundaries.
