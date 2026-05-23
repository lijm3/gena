# 开发 Harness

> **一句话理解**：模型是大脑，Harness 是身体。

---

## 一、Harness 是什么

Harness 是 Agent 在特定领域工作所需要的一切。

我们先看两个等式：

```text
Agent = LLM 大脑 + 工具手脚 + 记忆 + 自主循环执行
Agent = LLM 大脑 + Harness
```

换句话说，**除了模型本身，其余都是 Harness**。它由五个部分组成：

| 组成 | 含义 | 典型示例 |
| --- | --- | --- |
| **Tools**（工具） | Agent 的"手" | 文件读写、Shell、网络、数据库、浏览器 |
| **Knowledge**（知识） | Agent 的"领域专长" | 产品文档、领域资料、API 规范、风格指南 |
| **Observation**（观察） | Agent 的"眼睛" | git diff、错误日志、浏览器状态、传感器数据 |
| **Action Interfaces**（行动接口) | Agent 的"出口" | CLI 命令、API 调用、UI 交互 |
| **Permissions**（权限) | Agent 的"边界" | 沙箱隔离、审批流程、信任边界 |

### 模型与 Harness 的分工

> - **模型做决策，Harness 执行。**
> - **模型做推理，Harness 提供上下文。**
> - **模型是驾驶者，Harness 是载具。**

### 为什么 Harness 因领域而变

- **编程 Agent** 的 Harness，是 IDE、终端和文件系统。
- **农业 Agent** 的 Harness，是传感器阵列、灌溉控制和气象数据。
- **酒店 Agent** 的 Harness，是预订系统、客户沟通渠道和设施管理 API。

Agent —— 那个智能、那个决策者 —— **永远是模型**。
Harness 因领域而变，而 Agent 因模型而跨领域泛化。

开发领域的 Harness，只是载具的一种形态。设计模式却可以泛化到**任何领域**：

> 庄园管理、农田运营、酒店运作、工厂制造、物流调度、医疗保健、教育培训、科学研究……

只要一个任务需要被**感知、推理和执行**，它就需要一个 Harness。

---

## 二、Harness 工程师到底做什么

### 1. 实现工具 —— 给 Agent 一双手

文件读写、Shell 执行、API 调用、浏览器控制、数据库查询。
每个工具都是 Agent 在环境中可以采取的一个行动。

> 设计原则：**原子化、可组合、描述清晰**。

### 2. 策划知识 —— 给 Agent 领域专长

产品文档、架构决策记录、风格指南、合规要求。

> 原则：**按需加载，不要前置塞入**。
> Agent 应该知道有什么可用，然后自己拉取所需。

### 3. 管理上下文 —— 给 Agent 干净的记忆

- **子 Agent 隔离**：防止噪声泄露
- **上下文压缩**：防止历史淹没
- **任务系统**：分**会话内 Todo**（管当前规划）与**持久任务图**（跨步骤、跨阶段协调），让目标活得比单次对话更久

### 4. 控制权限 —— 给 Agent 边界

- 沙箱化文件访问
- 对破坏性操作要求审批
- 在 Agent 和外部系统之间实施信任边界

> 这是**安全工程**与 **Harness 工程**的交汇点。

### 5. 收集任务过程数据 —— 让 Agent 不断进化

Agent 在你的 Harness 中执行的每一条行动序列，都是**训练信号**。
真实部署中的「感知—推理—行动」轨迹，是微调下一代 Agent 模型的原材料。

> 你的 Harness 不仅服务于 Agent —— 它还在帮助**进化** Agent。

---

### 一句话总结

> **你不是在编写智能，而是在构建智能栖居的世界。**

这个世界的质量 —— Agent 能看得多清楚、行动得多精准、可用知识有多丰富 —— 直接决定了智能能多有效地表达自己。

> **造好 Harness，Agent 会完成剩下的。**

---

## 三、Claude 的原理：一次 API 调用里发生了什么

上一章讲了 Harness 工程师要做的五件事。但要做得对，必须先看清它服务的对象 —— **模型本身**。

LLM 工作的方式反直觉到让人意外：它不会"思考"，不会"记忆"，也不会"执行"。**它只做一件事：输入一段文字，输出一段文字。** Harness 里的每一个机制，都是这条事实的延伸。把原理看穿，后面九大组件**为什么非得长成那个样子**，就不必去记了。

### 1. 一次 API 调用的输入输出

> **Claude 不是一个持续运行的智能体。它是一个纯函数。**

```text
输入：  system prompt + messages + tools 定义
输出：  一段文本（可能包含 tool_use 结构）
调用完就结束，没有任何副作用，也不记得任何事。
```

输入结构：

```json
{
  "system": "你是一个编程助手……",
  "tools": [
    { "name": "Read", "description": "读文件", "input_schema": {...} },
    { "name": "Bash", "description": "执行命令", "input_schema": {...} }
  ],
  "messages": [
    { "role": "user", "content": "帮我读一下 README" },
    { "role": "assistant", "content": [...] }
  ]
}
```

输出结构：

```json
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "好的，让我读一下。" },
    { "type": "tool_use", "name": "Read", "input": { "path": "README.md" } }
  ]
}
```

请留意输出里的 `tool_use` 结构 —— 它看起来像"模型调用了一个函数"。下一节会告诉你：**这只是个错觉**。

---

### 2. 模型没有双手 —— "调用工具"只是输出文字

这是最反直觉的一点：

> **模型永远只输出文字。它从未、也无法"执行"任何工具。**

你看到模型"调用了 Read"，背后真实发生的是：

```text
第一次 API 调用
    输入 ← (system, messages, tools)
    输出 → 一段文本，里面包含 tool_use 结构

Harness 读到 tool_use
    ↓ 解析
    ↓ 真正去执行 Read(path="README.md")
    ↓ 拿到文件内容

第二次 API 调用
    输入 ← (system, messages + 上一轮 assistant + tool_result, tools)
    输出 → 基于新信息，决定下一步……
```

> **分工点睛**
> - 模型：看文字、想事情、吐文字
> - Harness：读文字、办事情、把结果写回文字

这就是"**模型做决策，Harness 执行**"在 API 层面的真相。

既然模型只会看文字、吐文字 —— 那它跨轮之间，**还记得自己做过什么吗？**

---

### 3. 模型是无状态的 —— "记忆"是每轮重新喂入

第二条反直觉的事实：

> **每次 API 调用之间，模型什么都不记得。**

所谓"连续对话"的错觉，是 Harness 每次都把完整历史重新发给模型。Claude 每次"睁眼"，看到的都是一整本日志。

```text
第 N   轮：messages = [m1, m2, ..., mN-1]           ← 全部历史
第 N+1 轮：messages = [m1, m2, ..., mN-1, mN]       ← 再加一条
第 N+2 轮：messages = [m1, m2, ..., mN,   mN+1]     ← 再加一条
```

上下文**单调增长**，直到逼近窗口上限。

到这里，LLM 的三面像已经凑齐：**只吐文字、跨轮失忆、上下文越攒越多**。那它是怎么完成一个需要几十步推进的任务的？答案简单到让人意外 —— **靠一个循环**。

---

### 4. Agent Loop = 反复调用 + 拼接历史

三条原理拼起来，Agent 的"持续工作"就露出底牌了：

```text
messages = [用户的初始请求]

while 还没做完:
    response = Claude.api_call(system, messages, tools)  ← 一次 API 调用
    messages.append(response)                            ← 回复拼进历史

    if response 含 tool_use:
        for call in response.tool_use:
            result = Harness.execute(call)               ← Harness 真正执行
            messages.append(tool_result(result))         ← 结果拼进历史
    else:
        break      ← 模型不再调工具 = 它认为做完了
```

**就这 10 行。这就是 Agent 的全部运行时**。

---

### 5. 三条硬约束：九大组件的起点

10 行代码**跑得动，跑不远**。因为前面四节揭示的事实，等价于 LLM 给 Agent 工程师划下的三块可活动的地。把它们写下来，后面所有机制的出处就都清楚了。

| 约束 | 含义 | 后果 |
| --- | --- | --- |
| **窗口有限** | 输入 token 有上限（Claude 4.7 可达 1M） | 长任务会撑爆窗口 |
| **无状态** | 每轮之间不共享任何内部状态 | "记忆"必须外置 |
| **只吐文字** | 没有手、没有执行器 | 所有"行动"必须由 Harness 代劳 |

**下一章要逐个拆解的九大组件，每一个都在对抗其中一条或多条约束**：

| 组件 | 对抗的约束 | 怎么对抗 |
| --- | --- | --- |
| Agent Loop | 只吐文字 | 把"吐文字"包成"持续行动" |
| 工具系统 | 只吐文字 | 给文字定义可执行语义 |
| 按需 Skill 加载 | 窗口有限 | 知识不前置，按需拉取 |
| 上下文压缩 | 窗口有限 | 老内容摘要化 |
| 子 Agent 派生 | 窗口有限 | 脏活丢到隔离上下文里做 |
| 任务系统 | 无状态 | 状态外置到结构化数据 |
| 多 Agent 协调 | 无状态 | 共享任务板充当分布式内存 |
| Worktree 隔离 | 无状态 + 只吐文字 | 物理隔离替代约定式协调 |
| 权限治理 | 只吐文字（且文字不可信） | 在"文字 → 行动"的边界做审查 |

> **记住这张表，九大组件就不再是"要背的知识点"，而是"面对三条约束的自然结论"。**
>
> Harness 工程的全部意义 —— **用工程机制把 LLM 的三条硬约束，转化成 Agent 的无限可能。**

---

## 四、为什么以 Claude Code 为样本

三条约束摆在那里，每一家做 Agent 框架的团队都在写自己的**答卷**。答卷风格差得很远 —— 有的试图用工作流替模型思考，有的用决策树圈住模型的每一步。

我们选 **Claude Code** 作为拆解样本，因为它是这份答卷里**最克制**的一版：

- 它没有试图成为 Agent 本身；
- 没有强加工作流；
- 没有用决策树去替模型思考。

它只是备好了工具、知识、上下文与权限边界，然后**退到一边**。真正的智能，它留给了模型。

> **Harness 没有让 Claude 变聪明，Claude 本来就聪明。**
> Harness 给了 Claude **双手、双眼和一个工作空间**。

这份克制，让 Claude Code 的每个机制都**单一归因** —— 要么解窗口有限，要么解无状态，要么解只吐文字。下一章我们逐个拆开看：每个组件**具体怎么做**，以及这背后**适用于任何领域、任何 Agent 的通用原则**。

---

## 五、九大组件逐一拆解

我们把前面那段架构拆开，**逐个讲清每一个组件做什么、为什么要有、以及它背后的 Harness 设计原则**。

### 1. Agent Loop —— Agent 的心跳

**是什么**：一个永不停息的「感知 → 决策 → 行动 → 观察」循环。

**为什么需要**：没有循环，模型只能做一次性问答；有了循环，模型才能成为**持续推进任务的主体**。

**怎么运作**：

```text
while not done:
    observe   ← 接收工具结果、用户消息、环境状态
    reason    ← 模型基于上下文生成下一步决策
    act       ← 调用工具、写文件、执行命令
    repeat
```

> **Harness 启示**
> - 循环主体极简，**不替模型做决策**
> - 模型自己选择何时停止（判断任务是否完成）
> - 每一轮都是一次完整的"思考 + 行动"机会

---

### 2. 工具系统 —— Agent 的双手

**是什么**：一组**原子化、可组合、描述清晰**的能力接口。

**Claude Code 的典型工具集**：

| 工具 | 作用 | 设计要点 |
| --- | --- | --- |
| `Read` | 读文件 | 支持行偏移，防止大文件溢出上下文 |
| `Write` | 写文件 | 必须先 `Read` 才能 `Write`，防止盲写 |
| `Edit` | 精准修改 | 按字符串替换而非行号，抗偏移 |
| `Glob` | 按模式找文件 | 比 `find` 更结构化，返回路径列表 |
| `Grep` | 全文搜索 | 基于 ripgrep，支持正则、文件类型过滤 |
| `Bash` | 执行 Shell | 可选后台运行 |
| `WebFetch` / `WebSearch` | 访问网络 | 连接 Agent 无法离线获取的世界 |

> **Harness 启示**
> - **单一职责**：每个工具只做一件事
> - **自描述**：工具名 + 参数 Schema + 使用说明本身就是文档
> - **反幻觉设计**：比如 `Edit` 要求先 `Read`，防止模型"假装"知道文件内容

---

### 3. 按需 Skill 加载 —— Agent 的"查手册"

**是什么**：不在对话开头塞入全部知识，而是只提供一个 **Skill 索引**，让模型**自己决定何时拉取哪个 Skill 文件**。

**传统做法的问题**：
- 一开始把全部文档 prompt 进去 → 上下文爆炸
- 文档多了，模型反而抓不住重点

**Claude Code 的做法**：

```text
系统启动时只注入 Skill 列表（名称 + 描述）
    ↓
模型判断："这个任务需要 Skill X"
    ↓
通过工具调用 Skill(name="X") 动态加载
    ↓
Skill 文件内容进入上下文
```

> **Harness 启示**
> - 上下文是**稀缺资源**，默认不装
> - Agent 需要"知道有什么"，而不是"提前全装进来"
> - 把"何时加载哪些知识"的决策权**下放给模型**

---

### 4. 上下文压缩 —— Agent 的"删减术"

**是什么**：当对话长度接近模型窗口上限时，自动**压缩历史**，保留要点，删除冗余。

**为什么需要**：
- 长任务会产生大量工具返回（日志、文件内容）
- 如果不压缩，上下文会被旧内容挤爆，新信息进不来

**怎么运作**：
1. 监测 token 使用量
2. 触发压缩时，调用专门 prompt 生成"历史摘要"
3. 摘要替换原始历史，保留关键决策与状态

> **Harness 启示**
> - 上下文不是"越大越好"，而是"越精越好"
> - 压缩策略决定 Agent 长任务的**耐力**
> - 摘要的质量 = Agent 的长期记忆质量

---

### 5. 子 Agent 派生 —— "克隆分身"做脏活

**是什么**：主 Agent 可以派生一个**上下文隔离**的子 Agent，让它去做污染性强的任务（海量搜索、粗读代码），只把**结论**带回来。

**典型场景**：
- "在整个 repo 里找出所有使用 X API 的地方" —— 派子 Agent 去做
- "分析这份 500 页 PDF" —— 派子 Agent 去啃

**怎么运作**：

```text
主 Agent → Agent(subagent_type=explore, prompt="...")
             ↓ 子 Agent 启动，独立上下文
             ↓ 大量 Glob / Grep / Read
             ↓ 整理结论
主 Agent ← 只收到最终结论（几百字）
```

> **Harness 启示**
> - **污染隔离**：让脏数据死在子上下文里，不污染主对话
> - **可并行**：多个子 Agent 并发拉起
> - 主 Agent 像**项目经理**，子 Agent 像**专员**

---

### 6. 带依赖图的任务系统 —— Agent 的"项目管理"

#### 先定位：任务系统分两层

任务管理并非只有一种形态，它按**时间尺度**天然分成两层：

| 分层 | 生命周期 | 解决的问题 | 典型实现 |
| --- | --- | --- | --- |
| **会话内 Todo** | 单次对话 | "接下来几步我要做什么" | Claude Code 的 `TaskCreate` / `TaskUpdate`（session-scoped） |
| **持久任务图** | 跨会话 / 跨 Agent / 跨天 | "这个项目还剩什么、谁在做、下阶段解锁什么" | 外部系统（Jira / Linear / GitHub Issues）或专用持久存储 |

> **一句话口诀**：**Todo 管会话内规划，持久任务图才负责跨步骤、跨阶段协调工作**。

**判断某条待办该放哪一层，就问一句**：

> 这条信息，下次开新对话还需要它吗？
> 需要 → 持久任务图；不需要 → 会话内 Todo。

**两层协同**才是真实项目里的常态：

```text
持久任务图：任务 #3「实现登录功能」  pending
    ↓ Agent 认领并进入一次会话
会话内 Todo：
    [ ] 设计 schema
    [ ] 配 JWT
    [ ] 写 API
    [ ] 写测试
    ↓ 会话结束
持久任务图：任务 #3  completed   ← 回写
```

> **持久任务图是骨架，会话内 Todo 是血肉。**
> 下面讲的"依赖图 / 状态流转 / 实战示例"，都围绕**持久任务图**这一层展开 —— 但同样的机制缩小到单会话，就是会话内 Todo。

---

**是什么**：不只是 TODO 列表，而是**有依赖关系的任务 DAG**。每个任务有状态（pending / in_progress / completed），可以被 `blockedBy` 其他任务。

**为什么需要**：
- 复杂任务要拆解
- 子任务之间有先后依赖
- 进度需要**跨对话轮次持久化**

**典型字段**：

```yaml
task:
  id: "3"
  subject: "实现登录 API"
  status: pending
  blockedBy: ["1", "2"]   # 等任务 1、2 完成才能开始
  owner: agent-backend
```

> **Harness 启示**
> - 任务系统让 Agent 的**计划**变得可见、可修改、可协调
> - 依赖图允许**多 Agent 分工** —— 一个 Agent 完成任务 1，解锁任务 3
> - 相比"一长段 prompt"，结构化任务更利于**中断恢复**和**团队协作**

#### Claude 怎么执行与追踪这 5 个步骤

任务拆完之后，Claude **不是靠"记住"来跟踪进度**，而是靠「查」—— 状态是一份外置数据（TaskList），Claude 每次想知道做到哪了，就调一次 `TaskList` 工具。

**5 个步骤的完整执行时序**：

```text
阶段 0 · 创建
    TaskCreate × 5  →  [1,2,3,4,5] 全部 pending

阶段 1 · 开始第一步（Claude 主动标记）
    Claude 决定："从任务 1 开始"
       ↓ TaskUpdate(taskId=1, status=in_progress)
    UI 上任务 1 的 spinner 转起来

阶段 2 · 做事
    Claude 调用 Write / Edit 写 schema.sql
    Claude 调用 Bash 跑 migration 测试
    工具返回：✓ 成功

阶段 3 · 自己判断"做完了"
    Claude 基于工具返回，判断："任务 1 完成"
       ↓ TaskUpdate(taskId=1, status=completed)

阶段 4 · 查看下一步
    Claude 调用 TaskList
       ↓ 系统返回最新状态
    Claude 看到："任务 2 可以做了"
       ↓ TaskUpdate(taskId=2, status=in_progress)  →  继续做事 …

    循环往复，直到全部变成 completed
```

**两个最容易误会的点**

**① 状态不是系统自动判断的**

系统**不会**因为 `schema.sql` 被写了，就自动把任务 1 标成 completed。**必须是 Claude 主动调 `TaskUpdate`**。

> 为什么？—— 因为"做完了"是个**语义判断**，只有模型能做：
> - 文件写了 ≠ 做完（可能还没测）
> - 代码编译过 ≠ 做完（可能还差测试）
> - 只有**模型自己**看过现场，才能判断"这件事真的完了"

**② 状态不是靠模型"记住"的**

在长对话里，模型可能已经做了几十轮工具调用，前面的工具返回早被压缩掉了。**它不是靠记忆追踪进度，而是靠"随时 `TaskList` 查一次"**。

> 这就是任务系统作为 **「Agent 外置记忆」** 的精髓：
> - 模型的**上下文**是易失的
> - 任务**状态**是稳定的
> - 需要时查一下就行，像人看 Kanban 板

**执行循环一图概括**：

```text
           ┌────────────────────────────────┐
           ▼                                │
    [TaskList 查看当前进度]                 │
           │                                │
           ▼                                │
    挑一个可做的任务                        │
           │                                │
           ▼                                │
    TaskUpdate(status=in_progress)          │
           │                                │
           ▼                                │
    调用工具做事(Write / Bash / Edit ...)   │
           │                                │
           ▼                                │
    模型自判断："这件事做完了吗？"          │
           │                                │
     ┌─────┴─────┐                          │
    Yes          No                         │
     │            │                         │
     ▼            └─ 继续做 ─────────────── │
  TaskUpdate(status=completed)              │
     │                                      │
     └──────── 回到开头 ───────────────────┘
```

> **一句话本质**：**系统负责「记住」，模型负责「判断」**。
> 任务系统 = 一个**外置的、结构化的**状态板，把"记忆"从模型身上卸载下来，让模型专注去做它擅长的"判断"和"决策"。

#### 实战示例：用任务系统拆解「实现登录功能」

下面是 Agent 在 Claude Code 中拆解一个真实需求时，**实际发出的工具调用**（JSON 格式直接照搬）。

**Step 1 · 创建 5 个任务**（并行 `TaskCreate`）

```json
{ "tool": "TaskCreate",
  "input": { "subject": "设计用户表 schema",
             "description": "字段：id / email / password_hash / created_at，邮箱建唯一索引",
             "activeForm": "设计用户表 schema" } }

{ "tool": "TaskCreate",
  "input": { "subject": "配置 JWT 密钥与中间件",
             "description": "读取 env、签发 access/refresh token、中间件校验",
             "activeForm": "配置 JWT" } }

{ "tool": "TaskCreate",
  "input": { "subject": "实现 POST /login API",
             "description": "校验凭证 → 签发 token → 返回 200",
             "activeForm": "实现登录 API" } }

{ "tool": "TaskCreate",
  "input": { "subject": "写登录 API 集成测试",
             "description": "覆盖成功 / 密码错误 / 用户不存在",
             "activeForm": "写集成测试" } }

{ "tool": "TaskCreate",
  "input": { "subject": "前端登录表单",
             "description": "邮箱密码输入 + 错误提示 + token 存 localStorage",
             "activeForm": "实现前端表单" } }
```

返回的任务 ID 分别是 `1, 2, 3, 4, 5`。

**Step 2 · 建立依赖关系**（`TaskUpdate` + `addBlockedBy`）

```json
{ "tool": "TaskUpdate",
  "input": { "taskId": "3", "addBlockedBy": ["1", "2"] } }

{ "tool": "TaskUpdate",
  "input": { "taskId": "4", "addBlockedBy": ["3"] } }

{ "tool": "TaskUpdate",
  "input": { "taskId": "5", "addBlockedBy": ["3"] } }
```

此时任务图如下：

```text
[1] 设计 DB ──┐
               ├──▶ [3] 登录 API ──┬──▶ [4] 集成测试
[2] 配置 JWT ─┘                    └──▶ [5] 前端表单
```

**Step 3 · 查看当前可做的任务**（`TaskList`）

```json
// TaskList 返回：
[
  { "id": "1", "subject": "设计用户表 schema",  "status": "pending",     "blockedBy": [] },
  { "id": "2", "subject": "配置 JWT 密钥与中间件", "status": "pending",   "blockedBy": [] },
  { "id": "3", "subject": "实现 POST /login API", "status": "pending",   "blockedBy": ["1","2"] },
  { "id": "4", "subject": "写登录 API 集成测试",  "status": "pending",   "blockedBy": ["3"] },
  { "id": "5", "subject": "前端登录表单",        "status": "pending",   "blockedBy": ["5"] }
]
```

> **解读**：只有 `1` 和 `2` 的 `blockedBy` 为空 —— **只有这两个任务"可领"**，其余都被依赖图锁住了。

**Step 4 · 领取并推进任务**

```json
// Agent A 认领任务 1
{ "tool": "TaskUpdate",
  "input": { "taskId": "1", "owner": "agent-backend", "status": "in_progress" } }

// Agent B 并行认领任务 2
{ "tool": "TaskUpdate",
  "input": { "taskId": "2", "owner": "agent-backend", "status": "in_progress" } }

// …干完活之后…
{ "tool": "TaskUpdate", "input": { "taskId": "1", "status": "completed" } }
{ "tool": "TaskUpdate", "input": { "taskId": "2", "status": "completed" } }
```

任务 `1` 和 `2` 完成的**瞬间**，任务 `3` 的 `blockedBy` 变空 —— 被**自动解锁**，下次 `TaskList` 它就出现在可领列表里。

**Step 5 · 状态收尾**

```json
// 走到最后，5 个任务全部 completed
{ "tool": "TaskList" }
// → 所有任务 status = "completed"，整个需求闭环
```

#### 从这个示例里能看出什么

| 观察点 | 背后的设计 |
| --- | --- |
| `TaskCreate` 可以一次并行发 5 条 | 创建是纯写入，**无副作用**，天然并行 |
| 依赖用 `addBlockedBy` 单独设置 | **创建和依赖解耦**，后期加新依赖不用重建任务 |
| Agent 通过 `blockedBy: []` 自动判断"可做" | 调度逻辑**内生于数据**，无需外部 orchestrator |
| 任务完成 → 下游自动解锁 | **事件驱动式流转**，不需要手动触发下一步 |
| `owner` 字段区分谁干的 | 多 Agent 协作时，**责任边界清晰** |

> **一句话**：任务系统不是"TODO 列表的 Markdown 版"，而是 **Agent 的分布式调度内核**。
> 只要数据结构设计对了，多 Agent 协调、中断恢复、进度追踪这些功能就都"**免费**"得到了。

---

### 7. 多 Agent 团队协调 —— "Agent 工作流"

**是什么**：多个 Agent 通过**共享任务系统**协作，各自认领任务、标记完成、互相解锁。

**协调方式**：
- **共享任务板**（TaskList）
- **专业分工**（frontend-agent / backend-agent / reviewer-agent）
- **Owner 字段**：每个任务归属某个 Agent

> **Harness 启示**
> - Agent 团队 = 单 Agent 能力的**乘数**
> - 协调机制应尽量**轻量**：任务板 + 消息传递，而不是重型 orchestration 框架
> - **专业化优于通用化** —— 每个 Agent 有自己的专长和权限

---

### 8. Worktree 隔离的并行执行 —— "平行宇宙"

**是什么**：利用 `git worktree`，让多个 Agent 在**同一仓库、不同分支、不同目录**下并行工作，互不踩脚。

**解决的问题**：
- 多 Agent 同时改代码 → 文件冲突
- 实验性修改 → 不想污染主分支
- 并行跑测试 → 需要独立工作目录

**怎么运作**：

```text
main repo (Agent A 在工作)
  ├── .claude/worktrees/feat-x/   ← Agent B 在这里
  └── .claude/worktrees/feat-y/   ← Agent C 在这里
```

每个 worktree 是独立分支 + 独立工作目录，共享同一个 `.git`。

> **Harness 启示**
> - **物理隔离 > 逻辑约定**
> - 并行化必须解决"共享状态"问题，worktree 是一种优雅的方案
> - 这个思路可泛化：数据库 Agent 用 schema 隔离、文件系统 Agent 用 namespace 隔离

---

### 9. 权限治理 —— "安全带与刹车"

**是什么**：一套**声明式**权限模型，决定 Agent 能自动执行什么、必须向用户确认什么、绝对不能做什么。

**三层结构**：

| 层级 | 含义 | 示例 |
| --- | --- | --- |
| **Allow** | 自动允许 | 读文件、运行 `npm test` |
| **Ask** | 需用户确认 | 写文件、改配置 |
| **Deny** | 绝对禁止 | 删除 `.git`、`rm -rf /` |

**配置位置**：
- 全局：`~/.claude/settings.json`
- 项目：`.claude/settings.json`
- 本地（不入库）：`.claude/settings.local.json`

> **Harness 启示**
> - 权限是**信任边界**的代码化表达
> - 黄金法则：**可逆操作默认允许，不可逆操作默认询问**
> - 权限配置本身是**可审计**的 —— 可版本化、可追溯、可 review

---

### 九大组件的相互关系

```text
                    [Agent Loop]
                         │ 驱动
      ┌──────────────────┼──────────────────┐
      ▼                  ▼                  ▼
   [工具]             [知识]             [记忆]
   Tools            Skill 加载         上下文压缩
                                           │
                                           ▼
                                    [子 Agent 派生]
                                           │
                                           ▼
                                      [任务系统]
                                           │
                                           ▼
                                   [多 Agent 协调]
                                           │
                                           ▼
                                   [Worktree 隔离]

           ↑ 全程受 [权限治理] 约束 ↑
```

> **工具给能力，知识给专长，记忆给耐力，协调给规模，权限给边界。**
>
> 九大机制合在一起，才是 **"Agent 可以真正工作的世界"**。

---

## 六、愿景：让真正的 Agent 走进各行各业

人类从事的每一种复杂工作 —— 需要判断、需要多步推进、需要在不确定中做决策 —— 都是 Agent 可以运作的领域。

> **差别不在于模型，而在于 Harness。**

本仓库中的模式，揭示的是一个朴素而深刻的结构：

```text
庄园管理 Agent  =  模型 + 物业传感器 + 维护工具 + 租户通信
农业 Agent      =  模型 + 土壤/气象数据 + 灌溉控制 + 作物知识
酒店运营 Agent  =  模型 + 预订系统 + 客户渠道 + 设施 API
医学研究 Agent  =  模型 + 文献检索 + 实验仪器 + 协议文档
制造业 Agent    =  模型 + 产线传感器 + 质量控制 + 物流系统
教育 Agent      =  模型 + 课程知识 + 学生进度 + 评估工具
```

循环永远不变。变的是工具，是知识，是权限边界。
**不变的是那个模型** —— 它泛化一切，适应一切。

---

### 写给 Harness 工程师

每一个 Harness 工程师都站在一个不寻常的位置：

> 你写的不只是软件，**你在为智能划定活动的疆域**。

每一个在真实领域落地的好 Harness，都是 Agent 得以感知这个世界、在其中推理和行动的**又一个立足点**。

**从工作室开始，到农田，到医院，到工厂，到城市。**
