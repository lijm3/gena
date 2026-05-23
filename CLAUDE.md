# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

本仓库是一个**可扩展 Coding Agent 的参考实现**，由单文件 `s_full.py`（保留在仓库根作为历史快照，不要修改它来反向同步逻辑）按职责拆分而成，分为 `config / core / tools / managers / agents / utils` 六个子包。`main.py` 是 REPL 入口。

工作目录在运行时由 `Path.cwd()` 决定（见 `config/settings.py` 中的 `WORKDIR`），所以**必须在仓库根目录执行 `python main.py`**，否则 `.team / .tasks / skills / .transcripts` 等运行时目录会被建在错误位置。

## 常用命令

```bash
# 安装依赖（仓库未提供 requirements.txt，至少需要：）
pip install requests python-dotenv

# 配置环境变量（必须设置 ANTHROPIC_AUTH_TOKEN）
cp config/.env.example .env   # 若无示例文件，直接在仓库根创建 .env

# 启动交互式 REPL
python main.py
```

REPL 内置斜杠命令（在 `main.py` 中处理，**不是** LLM 工具）：

| 命令 | 作用 |
|------|------|
| `/tasks` | 列出 `.tasks/` 下所有持久化任务 |
| `/team` | 列出 `.team/config.json` 中的队友 |
| `/inbox` | 读取并清空 `lead` 的收件箱 |
| `/compact` | 强制对当前 history 跑一次 `auto_compact` |
| `/help` | 查看命令 |
| `q` / `exit` / 空行 | 退出 |

仓库**没有**测试套件、lint 配置或 CI；不要假设 `pytest` / `ruff` 等工具可用。

## 配置

所有配置经环境变量读取（`config/settings.py`）。关键变量：

- `ANTHROPIC_AUTH_TOKEN`（**必填**）— 同时作为 `Authorization: Bearer` 与 `x-api-key` 注入请求头
- `ANTHROPIC_BASE_URL`（默认 `https://cngpt.net`）— 网关请求路径固定为 `{base}/v1/messages`
- `MODEL_NAME`（默认 `gpt-5.4`）— 注意默认值不是真正的 Anthropic 模型，本项目面向**Anthropic Messages 协议兼容的第三方网关**
- `LLM_STREAM`（默认 `true`）— 部分网关只支持 SSE 流式
- `LLM_STREAM_PRINT`（默认 `false`）— 流式时把 text 增量直接打到 stdout
- 循环控制：`MAX_AGENT_ROUNDS=12`、`MAX_TOOL_CALLS=30`、`MAX_TOOL_CALLS_PER_ROUND=5`、`WALL_CLOCK_TIMEOUT=60`、`TOKEN_BUDGET=150000`、`TOKEN_THRESHOLD=100000`（触发 auto_compact）

## 架构关键点

### 1. LLM 客户端是 Anthropic Messages 协议（不是 OpenAI）

`core/llm_client.py` 直接对 `/v1/messages` 端点 POST，并自己实现了 SSE 解析（`_consume_stream` + `_parse_sse_event`），按 `content_block_start / _delta / _stop / message_delta` 累积 `content[]` 与 `stop_reason`，输出与非流式响应**结构等价**的 dict。

注意两个易踩的坑：
- **强制 UTF-8 解码**（`_ensure_utf8_response`）：很多网关返回 UTF-8 但不带 charset，requests 会按 ISO-8859-1 解码导致中文乱码。
- **工具调用入参以 `input_json_delta` 流式分片传入**，需在 `content_block_stop` 时把 `_json_buf` 合并 `json.loads`（见 `_merge_partial_json`）。

新增/修改 LLM 调用相关代码时，要保持非流式与流式两条分支的返回结构一致（必含 `content`、`stop_reason`）。

### 2. Agent 主循环 + 多层循环防护

`agents/main_agent.py::MainAgent.agent_loop` 是核心。每一轮执行前会经过四道闸门，触发任意一个都会调用 `_force_conclusion()`（注入 `<loop-guard>` 提示，禁用工具，让模型给出最终回答）：

1. **硬上限**：`MAX_AGENT_ROUNDS` / `MAX_TOOL_CALLS` / `MAX_TOOL_CALLS_PER_ROUND`
2. **预算控制**（`utils/loop_control.py::BudgetController`）：wall-clock 超时 + token 预算
3. **重复调用检测**（`LoopDetector`）：基于 `(tool_name, normalized_input)` 的 MD5 指纹，捕获 N 次相同调用以及 ABAB 交替模式
4. **无进展检测**（`ProgressTracker`）：连续 N 轮工具结果文本相似度（基于 token 集合 Jaccard）超阈值即收敛

新增工具或修改循环逻辑时，要保留这四道闸门的语义。`ToolResult`（`utils/loop_control.py`）通过 `[DONE | CHANGED | NO_CHANGE | ERROR]` 状态前缀给模型可读的"是否产生变更"信号，**所有基础工具应当返回 `ToolResult` 而非裸字符串**，否则进展检测和模型自我抑制都会变弱。

### 3. 预处理顺序（每轮 LLM 调用前）

`MainAgent._preprocess` 严格按以下顺序：
1. 微压缩（`microcompact`，把除最近 3 个之外的旧 `tool_result` content 改为 `[cleared]`，原地修改）
2. token 估算超 `TOKEN_THRESHOLD` → 触发 `auto_compact`（保存完整 transcript 到 `.transcripts/`，再让 LLM 生成摘要）
3. 排空 `BackgroundManager` 的通知队列，作为 `<background-results>` user message 注入
4. 排空 `lead` 的收件箱（`bus.read_inbox`），作为 `<inbox>` user message 注入

注意 `read_inbox` **读后清空**——它既是预处理步骤也是 LLM 工具，调用任意一边都会消耗收件箱。

### 4. 工具分发统一在 `tools/tool_dispatcher.py`

`ToolDispatcher.get_handler(name)` 返回 lambda；`get_tools()` 返回提供给 LLM 的 schema。**两者必须同步维护**——添加新工具要同时改两个方法。

`compress` 工具是个特例：handler 只返回字符串，真正的压缩动作由 `MainAgent._execute_tools` 检测 `block["name"] == "compress"` 时设置 `manual_compress=True`，回到主循环后调用 `auto_compact` 并立即结束本轮。

子 Agent（`agents/subagent.py::run_subagent`）有**独立的工具集和 handler 表**，不复用 `ToolDispatcher`：
- `agent_type="Explore"`：只有 `bash` + `read_file`（只读模式）
- `agent_type="general-purpose"`：再加 `write_file` + `edit_file`
- 内循环硬编码 30 轮上限，**没有** loop detection / progress / budget 防护

如果给主 Agent 加了重要的新工具，要判断是否需要同步加到 subagent；反之亦然。

### 5. 持久化的状态文件

运行时会在 `WORKDIR` 下生成（已被 `.gitignore` 部分覆盖，注意不要把它们提交）：

- `.tasks/task_<id>.json` — `TaskManager` 持久任务，支持 `blockedBy` 依赖；`status="completed"` 时会扫描所有任务移除对自身的依赖；`status="deleted"` 直接删文件
- `.team/config.json` — 队友配置；`.team/inbox/<name>.jsonl` — 各 Agent 收件箱（追加写、读时清空）
- `.transcripts/transcript_<ts>.jsonl` — 每次 `auto_compact` 前的完整历史快照
- `skills/` — 按需加载的技能文档（`SkillLoader.load(name)`）

### 6. 队友（teammate）= 后台线程上的独立 LLM 循环

`managers/teammate_manager.py::TeammateManager._loop` 在 `threading.Thread(daemon=True)` 中跑一个**工作阶段（最多 50 轮）+ 空闲阶段（轮询 `IDLE_TIMEOUT/POLL_INTERVAL` 次）**的状态机：
- 空闲阶段轮询 `.tasks/`，自动认领 `pending && !owner && !blockedBy` 的任务
- 收到 `shutdown_request` 类型消息时设状态为 `shutdown` 并退出
- 历史很短时会做"身份重注入"（在 messages 头部插入 `<identity>` 块），降低自动认领后的角色漂移

队友的工具集是**硬编码**的（不走 `ToolDispatcher`），新增/修改工具时要同步这里。

### 7. 安全检查

- `utils/path_utils.py::safe_path` 用 `Path.is_relative_to(WORKDIR)` 防路径逃逸；所有文件工具必须经它解析，不要直接 `Path(p)`。
- `tools/base_tools.py::run_bash` 有黑名单（`rm -rf /` / `sudo` / `shutdown` / `reboot` / `> /dev/`）；扩展时不要绕过。
- `utils/encoding_utils.py::safe_subprocess_run` 是 Windows 中文编码的兼容封装（项目主要在 Windows 环境运行），新加的 subprocess 调用应走它。

### 8. 钩子机制（Hooks）

`managers/hook_manager.py` 提供工具/会话级生命周期事件钩子，参考 `tests/hook.py` 的协议（subprocess + exit code 0/1/2 + JSON stdout）。Phase 1 支持三个事件：`SessionStart` / `PreToolUse` / `PostToolUse`，已在 `MainAgent` / `subagent` / `TeammateManager` 三处工具循环都接入。

启用条件（**任一不满足都静默跳过，零开销**）：
1. 工作区受信：`.claude/.claude_trusted` 文件存在（或 HookManager 以 `sdk_mode=True` 构造）
2. 配置存在：`.claude/hooks.json` 至少注册了一个事件（回退路径 `.hooks.json`）

退出码协议：
- `0` 继续（stdout 若为合法 JSON，可携带 `updatedInput` / `additionalContext` / `permissionDecision`）
- `1` 阻断（仅 `PreToolUse`；其他事件退化为注入）
- `2` 注入 message（stderr 即注入内容）
- 其它视作错误，按 0 处理

环境变量传给钩子脚本：`HOOK_EVENT` / `HOOK_AGENT_ROLE`（`lead` / `subagent` / teammate 名）/ `HOOK_TOOL_NAME` / `HOOK_TOOL_INPUT`（JSON）/ `HOOK_ROUND` / `HOOK_CALL_INDEX`，PostToolUse 另带 `HOOK_TOOL_OUTPUT` / `HOOK_OUTPUT_STATUS`（`DONE` / `CHANGED` / `NO_CHANGE` / `ERROR` / `UNKNOWN`）/ `HOOK_DURATION_MS` / `HOOK_ERROR`。

注意：
- 钩子被阻断的工具调用**仍计入** `MAX_TOOL_CALLS` / `LoopDetector`，防止用钩子绕过 loop guard。
- 钩子默认 30s 硬超时，超时按 exit 0 处理。
- 钩子任何异常都不会向上抛——HookManager 内 except 兜底。
- 详见 `docs/钩子机制方案.md`；示例配置 `.claude/hooks.json.example`，示例脚本 `hooks/`。

## 写代码时的约定

- **中文**：现有代码注释、docstring、用户可见的 `print` 都用中文，保持一致。
- **`s_full.py` 是历史快照**，不要在它里面改 bug；功能修改一律落在 `agents/managers/tools/utils/core` 下对应模块。
- 仓库无测试，**修改后请至少跑一次 `python main.py`** 进行简单冒烟（`/help` → 一条对话 → `q`），确认 import 和 LLM 调用未坏。
- 新加的工具：① 加到 `ToolDispatcher.get_handler` 与 `get_tools`；② 决定是否同步到 `subagent.py` 与 `teammate_manager.py`；③ 优先返回 `ToolResult` 而不是裸 string。
