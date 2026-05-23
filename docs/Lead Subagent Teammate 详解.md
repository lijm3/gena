# Lead / Subagent / Teammate 三种 Agent 详解

> 编写日期:2026/05/21
> 适用版本:当前 `develop` 分支
> 来源:基于 `agents/main_agent.py`、`agents/subagent.py`、`managers/teammate_manager.py`、`main.py` 的实际代码梳理

本仓库存在三种语义完全不同的 Agent 角色,理解它们的区别是新人上手与扩展工具的前提。本文回答两个核心问题:

1. **三者有什么区别?**
2. **怎么触发 Teammate?**

---

## 一、一句话区分

| 角色 | 一句话 | 代码入口 |
|---|---|---|
| **Lead** | 与用户对话的主 Agent,REPL 那个 `>>` 后面的"你" | `agents/main_agent.py::MainAgent.agent_loop` |
| **Subagent** | Lead 派出去做隔离调研/任务的**一次性**子 Agent,用完即弃 | `agents/subagent.py::run_subagent` |
| **Teammate** | 跑在**后台线程**里的**持久化**协作者,自己认领任务 | `managers/teammate_manager.py::TeammateManager` |

---

## 二、详细对比

| 维度 | **Lead**(主 Agent) | **Subagent**(子 Agent) | **Teammate**(队友) |
|---|---|---|---|
| 数量 | 进程只有 1 个 | 按需派生,串行短任务 | 0~N 个,后台并行 |
| 线程模型 | 主线程同步阻塞 | 主线程同步调用,Lead 等它返回 | `threading.Thread(daemon=True)` 后台跑 |
| 持久化 | 无(history 在内存) | 无(用完丢弃) | **有**:`.team/config.json` 存配置;重启后状态可见 |
| 派生者 | 用户启动 | Lead 调 `task` 工具派生 | Lead 调 `spawn_teammate` 工具派生 |
| 与用户 | **直接对话** | 间接(摘要回 Lead) | 间接(通过 `MessageBus` 给 Lead 发信) |
| 工具来源 | `tools/tool_dispatcher.py`(统一) | **硬编码**在 `subagent.py` 里 | **硬编码**在 `teammate_manager.py` 里 |
| 工具集广度 | **最全**:bash/读写/task/load_skill/compress/background/task_*/spawn/send/inbox/...全套 | **窄**:bash/read_file/read_image(+ write/edit for general-purpose) | **中**:bash/读写 + send_message/idle/claim_task(无 task/spawn) |
| 工具档位 | 单一 | 两档:`Explore`(只读)/ `general-purpose`(可写) | 单一 |
| 循环防护 | **四道闸门齐全**:硬上限 + `BudgetController` + `LoopDetector` + `ProgressTracker` | `LoopDetector` + `ProgressTracker` + 30 轮硬上限(**无 BudgetController**) | **只有** 50 轮工作阶段硬上限(**无 LoopDetector / ProgressTracker / BudgetController**) |
| 压缩管道 | `microcompact` + `auto_compact` 每轮跑 | 无(短任务,跑不到压缩门槛) | `microcompact` + `auto_compact` + **压缩后身份重注入** |
| 状态机 | 单一循环 | 单一循环 | **工作阶段(≤50 轮)+ 空闲阶段(轮询 `IDLE_TIMEOUT/POLL_INTERVAL`)** 切换 |
| 任务自动认领 | 否 | 否 | **是**:空闲阶段扫 `TaskManager.find_claimable()` 自动 claim |
| 退出条件 | 用户输入 `q`/`exit` | 模型 `stop_reason ≠ tool_use` 或触发防护 | 收到 `shutdown_request` 消息 / 空闲超时 / 异常 |
| 身份 prompt | `WORKDIR` 上下文 + 工具说明 | 区分 Explore / general-purpose 两套 system prompt | `"You are '<name>', role: <role>, team: ..."`,压缩后会重注入 |

---

## 三、关键差异解释

### 3.1 为什么 Subagent 防护层比 Lead 少?

Subagent 是"用完即弃"的短任务,30 轮硬上限就足够兜底,不需要 `BudgetController` 那种 wall-clock + token 双预算——本质上 Lead 的预算包住了它。

### 3.2 为什么 Teammate 防护层比 Subagent 还少?

Teammate 是**长期运行**的状态机,工具循环只是它整个生命的一小段,大部分时间在"空闲阶段轮询任务"。如果套用 Subagent 的 `LoopDetector`,跑几天后正常情况下也会误触发(同一类巡检命令重复跑是常态)。**这是个有意识的取舍,不是疏忽**。

### 3.3 为什么三个 Agent 的工具表是三份独立硬编码?

这是 `CLAUDE.md` 反复强调的"痛点":新加一个工具(比如 SSH 那套)要同步改三处。

- **坏处**:冗余,容易漏改
- **好处**:三个 Agent 可以根据角色裁剪——Subagent 的 Explore 模式只读、Teammate 不给 `task/spawn`(避免无限派生),都靠"独立工具表"实现

新加工具时务必检查三处是否都同步:`tools/tool_dispatcher.py` / `agents/subagent.py` / `managers/teammate_manager.py`。

### 3.4 它们之间怎么通信?

```
       用户
        ↓ 输入
     [Lead] ──spawn_teammate──> [Teammate 1] ──┐
        │                       [Teammate 2] ──┤ MessageBus
        ├──task──> [Subagent](阻塞等返回)      │ (.gena.db)
        │                                       │
        └─────── inbox ←─ send_message ────────┘
```

- **Lead ↔ Subagent**:函数调用,Subagent 返回一个字符串摘要塞进 `tool_result`
- **Lead ↔ Teammate**:异步,通过 `MessageBus`(SQLite)发收件箱;Lead 每轮预处理时排空自己的 inbox
- **Teammate ↔ Teammate**:同上,也走 `MessageBus`
- **Lead/Teammate ↔ TaskManager**:共享 `.gena.db`,Teammate 在空闲阶段主动认领

---

## 四、如何触发 Teammate

REPL 里**没有**直接 spawn 队友的斜杠命令——`/team` 只是看现有队友列表,`/tasks` 看任务,**真正派生队友只能通过自然语言让 Lead 调 `spawn_teammate` 工具**。

### 4.1 三条触发路径

#### 路径 1:派生**新**队友——直接说

让 Lead 看出"这是个适合后台跑的长期角色",它就会调 `spawn_teammate(name, role, prompt)`。例子:

```text
>> 帮我开一个叫 ops 的队友,负责盯 staging 的日志,发现 5xx 错误就告诉我

>> 派一个测试工程师队友 qa,持续跑 pytest,失败就汇报

>> spawn 一个 reviewer 队友,扫 .tasks 里的代码评审任务并自动认领
```

**关键词**:"开一个/派一个/spawn 一个 + **名字** + **角色描述** + **持续做什么**"——只要让模型理解是"长期后台角色"而不是"一次性调研",它就会用 `spawn_teammate`(而不是 `task`)。

#### 路径 2:给**已有**队友派活——用 `send_message`

队友已经 spawn 过(`/team` 能看到名字),要给它派新任务:

```text
>> 让 ops 去检查一下 nginx 配置

>> 告诉 qa 把 tests/test_image.py 重跑一遍
```

Lead 会调 `send_message(to="ops", content="...")`,消息进入 ops 的收件箱,ops 下一轮工作时读到并响应。

#### 路径 3:**被动激活**——`task_create` + 队友自动认领

不指定收件人,只创建任务,让空闲队友自己抢:

```text
>> 建一个任务:把今天的部署日志归档,谁有空就做
```

Lead 调 `task_create(subject="...", description="...")` → 任务进 `.gena.db` → 任何空闲(idle 阶段)的队友扫到无 owner 任务时会 `try_claim` 抢走(原子操作,只有一个抢到)。

### 4.2 各路径的工具流

| 用户意图 | Lead 调的工具 | 队友状态变化 |
|---|---|---|
| 派生新队友 | `spawn_teammate(name, role, prompt)` | 新线程启动,直接进入工作阶段(50 轮) |
| 给已有队友派活 | `send_message(to=name, content)` | 若 idle,收件箱有消息后 resume 为 working |
| 公共池任务 | `task_create(subject, description)` | 空闲队友轮询(`POLL_INTERVAL=5s`)扫到后 `try_claim` 抢 |

### 4.3 直接验证(不通过 LLM)

如果想绕过 Lead 直接测队友,可以在 Python 里手工调:

```python
# REPL 之外快速验证
from managers.message_bus import MessageBus
from managers.task_manager import TaskManager
from managers.teammate_manager import TeammateManager

bus = MessageBus()
tm = TaskManager()
mgr = TeammateManager(bus, tm)
mgr.spawn("ops", "运维", "持续盯日志,发现异常告诉 lead")
```

实际 REPL 使用时,**全靠对 Lead 用自然语言**——Lead 的 system prompt 里有完整的工具表(`spawn_teammate / send_message / task_create / list_teammates / broadcast / shutdown_request`),它会自己挑合适的。

---

## 五、常见使用场景对照

| 场景 | 应该用哪个? | 理由 |
|---|---|---|
| "帮我查一下代码里所有用到 LLMClient 的地方" | **Subagent (Explore)** | 一次性调研,只读 |
| "把 tests/ 下所有失败的测试改 fix 掉" | **Subagent (general-purpose)** | 一次性写入,Lead 等结果 |
| "持续监控构建机,失败立刻告诉我" | **Teammate** | 长期后台,事件驱动 |
| "今天有多个 bug 需要修,你和 backend 队友分工" | **Teammate** + `task_create` | 多个并行任务,自动分配 |
| "解释一下这段代码" | **Lead 直接答** | 不需要工具调用,模型自己即可 |
| "看一下这张截图里的报错" | **Lead** + `read_image` | 简单单步操作 |

---

## 六、扩展点

- **新加工具**:三处工具表都要同步(参考 `CLAUDE.md` "新加的工具"约定)
- **新加 Agent 角色**:目前只有 Lead/Subagent/Teammate 三种,新角色应该归到这三种之一(比如"评审 agent" → 一个名为 `reviewer` 的 Teammate),而不是发明第四种
- **Subagent 与 Teammate 的权限边界**:Teammate 工具集默认比 Subagent 窄(没有 `task`/`spawn_teammate`,避免无限派生);新工具默认采用同样原则——危险/高权限工具优先给 Lead,Subagent 次之,Teammate 最后

---

## 七、参考代码位置

| 概念 | 文件:行 |
|---|---|
| Lead 主循环 | `agents/main_agent.py::MainAgent.agent_loop` |
| Lead 工具表 | `tools/tool_dispatcher.py::ToolDispatcher.get_tools` |
| Subagent 主循环 | `agents/subagent.py::run_subagent` |
| Subagent 工具表 | `agents/subagent.py` 内 `sub_tools` 列表 |
| Teammate 状态机 | `managers/teammate_manager.py::TeammateManager._loop_inner` |
| Teammate 工具表 | `managers/teammate_manager.py` 内 `tools` 列表 |
| 四道防护闸门 | `utils/loop_control.py`(`LoopDetector` / `ProgressTracker` / `BudgetController`) |
| 消息总线 | `managers/message_bus.py::MessageBus` |
| 任务管理 | `managers/task_manager.py::TaskManager` |
| REPL 入口与斜杠命令 | `main.py::main` |
