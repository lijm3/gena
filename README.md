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

### s02: 工具分发模式
- 统一的工具处理接口
- 工具名到处理函数的映射

### s03: Todo 管理系统
- 会话内短期任务清单
- 最多 20 个 todo 项
- 只允许一个 `in_progress` 状态

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

### s10: 关闭协议
- 优雅关闭
- 计划审批

### s11: 自动任务认领
- 空闲轮询
- 自动认领任务

## 配置

### 环境变量

复制 `.env.example` 到 `.env` 并填写配置：

```bash
cp config/.env.example .env
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
cd demo
python main.py
```

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
