# Local Memory System / 本地记忆系统

Local Memory System is a local-first memory workbench for people who use AI assistants across recurring projects. It stores source materials as ordinary files, builds auditable project memory from those materials, and prepares bounded task context that another assistant can read without silently mixing projects, stale conclusions, or unverified history.

本地记忆系统面向长期使用 AI 助手处理多个项目的人：把原始资料保存为普通本地文件，在资料之上形成可追溯的项目记忆，并为每次任务准备有边界的上下文，避免不同项目、过期结论和未经确认的历史被混在一起。

## What It Is For / 适用场景

- Personal or team project memory that must remain readable outside any one AI tool.
- Repeated business, product, research, delivery, or operations work where source evidence matters.
- Preparing compact task context for assistants while keeping original files, derived knowledge, corrections, and saved context packages separate.
- Testing local-first memory workflows before integrating more automation or model processing.

- 个人或团队的项目记忆，需要脱离具体 AI 工具也能直接读取。
- 商务、产品、研究、交付、运维等长期反复推进、且需要保留来源依据的工作。
- 给 AI 助手准备精简任务上下文，同时区分原始资料、加工知识、人工纠正和已保存上下文包。
- 在接入更多自动化或模型处理前，先验证本地优先的记忆工作流。

## Purpose / 服务目标

The system is designed to serve accountable AI-assisted work. Its first priority is not to make an assistant "remember everything", but to make memory explicit, inspectable, correctable, and bounded by project, source, version, and authorization.

这个系统服务的是“可追溯、可纠正、可控边界”的 AI 协作。它的首要目标不是让助手无差别记住一切，而是让记忆的来源、版本、项目范围和授权状态都清楚可查。

## Goals / 目标

- Keep real source files local and tool-independent.
- Preserve immutable source versions instead of overwriting history.
- Separate raw materials, derived knowledge, saved task contexts, model settings, and steward queue state.
- Make stale or blocked knowledge visible instead of presenting it as current truth.
- Require explicit project-level authorization before model processing of sensitive or real materials.
- Provide a small, inspectable Python and browser implementation that can be extended in controlled phases.

- 原始资料保留在本地普通文件中，不绑定特定 AI 工具。
- 资料追加版本，不覆盖旧版本。
- 原始资料、加工知识、任务上下文、模型配置和管家队列状态分开保存。
- 过期、待复核或阻断的知识明确展示，不伪装成当前事实。
- 真实资料进入模型处理前，需要按项目显式授权。
- 提供一个小而可检查的 Python + 浏览器实现，便于后续按阶段扩展。

## Current Capabilities / 当前能力

- Local vaults with `vault.json`, `INDEX.md`, immutable `originals/`, and project knowledge folders.
- Literal source search and project-scoped record listing.
- Bounded task-context preview and saved context packages.
- CSV table inspection, CSV source-evidence packages, and read-only evidence reuse checks.
- Optional model profile management through local runtime files and macOS Keychain.
- Optional limited steward queue for model-assisted extraction, with source and authorization checks.
- Browser UI plus CLI client.

- 本地资料库：包含 `vault.json`、`INDEX.md`、不可变 `originals/` 和项目知识目录。
- 按项目进行原始资料字面检索和记录列表查看。
- 有字节预算的任务上下文预览与保存。
- CSV 表格查看、CSV 来源证据包、已保存证据包只读核验复用。
- 可选模型配置管理，运行状态保存在本地目录，凭据可进入 macOS Keychain。
- 可选有限管家队列，在来源和授权核验后进行模型辅助摘录。
- 浏览器界面和 CLI 客户端。

## Quick Start / 快速开始

Requirements: macOS or another POSIX-like environment with Python 3.9+.

运行要求：macOS 或类 POSIX 环境，Python 3.9+。

```sh
git clone git@github.com:pzhcyh/local-memory-system.git
cd local-memory-system
./start.command
```

Open <http://127.0.0.1:4191/>. The default launcher creates local demo and empty vaults under `.local-data/`, and runtime state under `.local-state/`. These directories are ignored by Git.

打开 <http://127.0.0.1:4191/>。默认启动脚本会在 `.local-data/` 下创建演示库和空库，在 `.local-state/` 下保存运行状态；这些目录不会提交到 Git。

To start a specific vault directly:

也可以直接指定自己的资料库：

```sh
python3 -m memory_service.server --init --port 4191 \
  --vault "work=/absolute/path/to/your/vault"
```

Use the CLI after the server is running:

服务运行后可使用 CLI：

```sh
python3 memoryctl.py --vault work status
python3 memoryctl.py --vault work search --project "Example" --query "keyword"
python3 memoryctl.py --vault work context --project "Example" --query "what should I know?" --max-bytes 4096
```

## Data Boundary / 数据边界

This repository is published without private trial data, evidence archives, model credentials, local runtime state, or personal vaults. The application creates new local data only when you explicitly start a vault or import material.

本仓库发布版不包含私有试用数据、验收证据归档、模型凭据、本地运行状态或个人资料库。只有在你明确启动资料库或导入资料时，程序才会在本机创建新的本地数据。

## Safety Notes / 安全说明

- The web service binds to `127.0.0.1`.
- Imported materials are displayed as text and are not executed as HTML or scripts.
- The in-memory browser token is a local session guard, not a multi-user authentication system.
- Source checksums support integrity checks, not cryptographic authorship proof.
- Model integrations are optional and should be authorized per project before processing real material.

- Web 服务只绑定 `127.0.0.1`。
- 导入资料按文本显示，不执行其中的 HTML 或脚本。
- 浏览器内存令牌只是本机会话保护，不是多用户身份系统。
- 来源校验值用于完整性检查，不等同于带密钥的作者证明。
- 模型集成是可选能力；处理真实资料前应按项目授权。

## Development / 开发

Run the Python tests:

运行 Python 测试：

```sh
python3 -m unittest discover tests
```

Check the browser JavaScript:

检查浏览器脚本语法：

```sh
node --check web/app.js
```

## License / 许可证

MIT License. See [LICENSE](LICENSE).
