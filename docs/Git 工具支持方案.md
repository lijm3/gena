# Git 工具支持方案（厚方案）

> **实现状态（2026-05-23）**：Phase A + Phase B 落地完成（17 个工具）。Phase C/D 待办，scope 见 §14。
>
> 让模型在 gena 里像调用 `bash` 一样自然地用 git，但**不再走 `bash` 黑洞**——每个 git 操作是单独的 tool，签名带类型，安全策略在工具边界强制执行。
>
> 对照：薄方案 = `PreToolUse matcher=bash` 钩子审计危险 git 命令；厚方案 = 一组 `git_*` 工具 + 内置安全栏 + 配套钩子。两者可叠加。

## 1. 背景与定位

### 1.1 现状

- gena 目前没有 git 工具；模型用 git 必须走 `bash`
- `tools/base_tools.py::run_bash` 的黑名单（`rm -rf /` / `sudo` / `shutdown` / `reboot` / `> /dev/`）**不拦截任何 git 命令**
- `safe_path` 是文件操作防线，**bash 命令完全绕过**——模型可以 `git checkout -- .` 抹掉用户未提交改动，可以 `git push --force` 改写远端历史
- 模型已知道怎么用 git（训练数据里 git 高频出现），但 LLM 看到工具表里没有 git 工具时，往往要先 `bash("git status")` 再决定后续——少了一层语义上的"工具就是这件事"信号

### 1.2 设计参照

- **`tools/ssh_tools.py`**：仓库里已有的"成组的、有 allow-list 安全栏的、走 `ToolResult` 的"工具模式
- **`utils/path_utils.py::safe_path`**：路径逃逸防御
- **钩子机制**（`docs/钩子机制方案.md`）：`PreToolUse matcher=git_push` 等可叠加做策略注入

## 2. 目标与非目标

### 2.1 目标

- 给 Lead / Subagent / Teammate 提供**分级**的 git 工具集，每个工具是一个明确的"动词 + 受控参数"
- 在工具边界**硬阻断**最危险的操作（force-push、reset --hard、clean -fd 等），让模型即便想绕也绕不过去
- 保留 `bash` 作为逃生通道，但默认场景下不需要走它
- 与 `safe_path` / `ToolResult` / 钩子机制全部兼容
- 没有 git 仓库时，工具能识别并优雅返回错误，不污染主循环

### 2.2 非目标

- 不实现 `git rebase` / `git merge` 的交互式分支（这两种操作的冲突解决是协作式的，强行做成 tool 会引入海量边界）
- 不实现 `git filter-branch` / `git filter-repo` 等历史改写
- 不实现 `git clean` 工具（要清就 bash，让钩子审计）
- 不替换 `bash`；不强制走 `git_*`
- 不引入 GitPython 等额外依赖——全部用 `subprocess` + git CLI，与 gena 现有风格一致

## 3. 工具清单（按安全等级分级）

按风险升序：**Read** → **Write-Local** → **Write-Local-Mutating** → **Network** → **Destructive**。

### 3.1 Read 层（始终可用，零风险）

| 工具 | 等价 git 命令 | 备注 |
|---|---|---|
| `git_status` | `git status --porcelain=v2 --branch` | 输出结构化的当前状态 + 分支信息 |
| `git_current_branch` | `git rev-parse --abbrev-ref HEAD` | 单独拿出来是因为这个值高频被用做后续命令的参数 |
| `git_diff` | `git diff [--cached] [--stat] [-- <path>]` | 入参：`staged: bool`、`stat: bool`、`path: str?` |
| `git_log` | `git log --oneline -n <limit>` | 入参：`limit: int = 20`、`path: str?` |
| `git_show` | `git show [--stat] <ref>` | 入参：`ref: str`、`stat: bool` |
| `git_blame` | `git blame -L <range> -- <file>` | 入参：`path: str`（safe_path）、`line_start/line_end: int?` |
| `git_branch_list` | `git branch -a` | 列分支 |
| `git_remote_list` | `git remote -v` | 列 remote |
| `git_tag_list` | `git tag --list` | 列标签 |

### 3.2 Write-Local 层（修改 index / working tree，不联网）

| 工具 | 等价 git 命令 | 安全栏 |
|---|---|---|
| `git_add` | `git add -- <paths>` | 每个 path 经 `safe_path` 解析；空 paths 报错（不允许 `git add .` 隐式行为） |
| `git_unstage` | `git reset HEAD -- <paths>` | 同上 |
| `git_stash_save` | `git stash push -m <msg>` | message 必填，避免无名 stash 堆积 |
| `git_stash_list` | `git stash list` | 读 |
| `git_branch_create` | `git checkout -b <name>` | name 走白名单正则（仅字母数字 `/-_.`） |
| `git_checkout_branch` | `git checkout <branch>` | 入参 `branch: str`；**不接受 `--`**（避免 `git checkout -- file` 走这条路） |

### 3.3 Write-Local-Mutating 层（修改历史 / 状态，需要谨慎）

| 工具 | 等价 git 命令 | 安全栏 |
|---|---|---|
| `git_commit` | `git commit -m <msg>` | 自动 `git diff --cached --quiet` 检查 staged 非空；message 必填且 ≥ 3 字符；**禁止 `--amend`** |
| `git_amend_commit` | `git commit --amend [-m <msg>]` | 单独的工具，且要 `confirm=True` 参数；rebase 提交集除外 |
| `git_stash_pop` | `git stash pop` | 没有冲突时 pop；有冲突就返回 ERROR 让模型自查 |
| `git_revert` | `git revert --no-edit <ref>` | 不接受 `<ref>..<ref>` 范围 |
| `git_reset_soft` | `git reset --soft <ref>` | `confirm=True`；index 不动，HEAD 退 |
| `git_reset_mixed` | `git reset --mixed <ref>` | `confirm=True`；working tree 不动，index + HEAD 退 |
| `git_tag_create` | `git tag <name> [<ref>]` | name 白名单 |

> **不实现**：`git_reset_hard` —— 这是最常见的"丢失未提交工作"事故源。要这种行为，明确让模型走 `bash` + 让 `PreToolUse matcher=bash` 钩子审计（推荐方案）。

### 3.4 Network 层（影响远端 / 共享状态）

| 工具 | 等价 git 命令 | 安全栏 |
|---|---|---|
| `git_fetch` | `git fetch [<remote>]` | 只读网络，任意时刻安全 |
| `git_pull` | `git pull --ff-only [<remote>] [<branch>]` | **默认 `--ff-only`**；非 ff 直接报错让模型决定 rebase / merge |
| `git_push` | `git push <remote> <branch>` | **当前分支** + **指定远端**；`--force` / `--force-with-lease` 必须 `force=True` 参数 + `GENA_ALLOW_FORCE_PUSH=true` 环境变量 + 远端分支名 ∉ {main, master, release/*} |

### 3.5 Destructive 层（高风险，逐项 opt-in）

| 工具 | 等价 git 命令 | 安全栏 |
|---|---|---|
| `git_restore_file` | `git restore -- <file>` 或 `git checkout HEAD -- <file>` | `confirm=True` 必填；返回前先打印将要丢弃的内容预览（前 200 字符） |
| `git_branch_delete` | `git branch -D <name>` | `confirm=True`；禁止 main/master/develop |

**不提供**的危险操作（要做请走 `bash` + 钩子审计）：

- `git reset --hard`
- `git clean -fd`
- `git filter-branch` / `git filter-repo`
- `git push --mirror`
- `git checkout <branch> --force`

## 4. 安全策略

### 4.1 工具边界 = 第一道防线

每个工具签名是受控的，不存在"传一个 `args: list[str]` 让模型自由发挥"的口子。比如 `git_push`：

```python
def git_push(remote: str = "origin", branch: Optional[str] = None,
             force: bool = False) -> ToolResult:
    """正常推送当前分支到 remote/branch。

    force=True 仅在所有条件满足时才允许：
      1. 环境变量 GENA_ALLOW_FORCE_PUSH=true
      2. 目标分支不在保护列表 {main, master, release/*, develop}
      3. 工具自己用 --force-with-lease（不是 --force）
    任一不满足 → ToolResult(error=True, content="...")
    """
```

### 4.2 路径参数走 `safe_path`

所有接受文件路径的工具（`git_add` / `git_unstage` / `git_blame` / `git_restore_file` / `git_diff -- <path>` / `git_log -- <path>`）都把每个路径过 `utils/path_utils.safe_path`，越界 → ERROR。

> 注意：`safe_path` 校验"绝对路径不出 WORKDIR"，但 git 的 pathspec 可以是模式（`*.py`、`tests/`）。处理思路：
> - 含通配符的 pathspec → 解析为 glob 列表，每条逐一校验
> - 单个目录 / 文件路径 → 直接 safe_path
> - 不允许 pathspec magic（`:(exclude)`、`:!*`）

### 4.3 不走 `shell=True`

参考 `tools/ssh_tools.py` 的做法，全部用 `subprocess.run([...])` 列表形式：

```python
def _run_git(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """统一的 git 调用入口。args 不能含 shell 元字符。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(WORKDIR),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
```

不走 shell 就杜绝了"模型把元字符塞进参数"的注入面。

### 4.4 环境前置：是不是 git 仓库

第一次调用任意 `git_*` 工具时，先跑一次 `git rev-parse --is-inside-work-tree`；不是 git 仓库 → 返回固定 ERROR，告诉模型"WORKDIR 不是 git 仓库"。结果缓存进进程（无需每次都查）。

### 4.5 不允许 `--no-verify` / 跳过钩子

所有 commit / push 调用**不传** `--no-verify` / `--no-gpg-sign` / `-c commit.gpgsign=false`，强制走仓库已有的 pre-commit / commit-msg / pre-push 钩子。模型想绕过 → 走 `bash`，让 `PreToolUse matcher=bash` 钩子审计。

### 4.6 ToolResult 状态信号

| 操作类型 | `changed` | `done` | `error` |
|---|---|---|---|
| Read 工具，正常返回 | `False` | `False` | `False` → NO_CHANGE |
| 写操作改了 index / HEAD | `True` | — | — → CHANGED |
| `git_commit` 成功 | `True` | `True` | — → DONE \| CHANGED |
| 工具内部安全栏拦截 | — | — | `True` → ERROR |
| `git_pull` 已经是最新 | `False` | — | — → NO_CHANGE |

这样模型从 `[NO_CHANGE]` 前缀就能立刻知道"我已经是最新了，不用再跑一次"，能显著降低重复调用。

### 4.7 与钩子机制叠加

工具边界是硬栏，**钩子是软栏**——同一道操作上两条防线。典型组合：

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "git_push", "command": "python hooks/audit_git_push.py" },
      { "matcher": "git_amend_commit", "command": "python hooks/audit_git_amend.py" },
      { "matcher": "git_restore_file", "command": "python hooks/audit_git_restore.py" }
    ]
  }
}
```

`audit_git_push.py` 可以做项目特定的策略（比如"周五不允许 push 到 main"），与工具内置的"never force-push to main" 互补。

## 5. 模块结构与 API

### 5.1 文件位置

```
tools/git_tools.py        # 所有 git_* 函数 + _run_git 私有辅助
tools/tool_dispatcher.py  # 注册 git_* 进 LLM 工具表
tests/test_git_tools.py   # 单元测试
hooks/audit_git_push.py   # 示例：push 审计
hooks/audit_git_amend.py  # 示例：amend 审计
```

### 5.2 `tools/git_tools.py` 内部约定

- 模块级 `_GIT_REPO_CACHE: Optional[bool] = None` 缓存"是否 git 仓库"
- 私有 `_run_git(args, timeout=30) -> CompletedProcess`
- 私有 `_assert_in_repo() -> Optional[ToolResult]`（返回 ERROR ToolResult 表示阻断；返回 None 表示放行）
- 私有 `_safe_pathspec(paths: list[str]) -> list[str]`
- 私有 `_classify_change(stdout, stderr) -> tuple[bool, bool, bool]`（changed/done/error 推断）

每个 public 函数：

```python
def git_status() -> ToolResult:
    if (err := _assert_in_repo()):
        return err
    r = _run_git(["status", "--porcelain=v2", "--branch"])
    if r.returncode != 0:
        return ToolResult(content=r.stderr.strip(), error=True, changed=False)
    return ToolResult(content=r.stdout, changed=False)  # NO_CHANGE 信号
```

### 5.3 工具 schema 样例

```python
{
    "name": "git_status",
    "description": "Show git status in porcelain=v2 format with branch tracking info.",
    "input_schema": {"type": "object", "properties": {}}
},
{
    "name": "git_commit",
    "description": "Create a new commit from staged changes. Fails if nothing is staged. Does NOT amend.",
    "input_schema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"]
    }
},
{
    "name": "git_push",
    "description": "Push current branch. force=true requires GENA_ALLOW_FORCE_PUSH=true and a non-protected branch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "remote": {"type": "string", "default": "origin"},
            "branch": {"type": "string"},
            "force": {"type": "boolean", "default": false}
        }
    }
}
```

## 6. ToolDispatcher / Subagent / Teammate 接入

### 6.1 ToolDispatcher（Lead 全集）

- `tools/tool_dispatcher.py::get_handler` 与 `get_tools` 同步加入全部 git 工具
- 与 `ssh_*` 工具一样作为单独成组的工具，保持文件结构清晰

### 6.2 Subagent 分级

| `agent_type` | 给的 git 工具 |
|---|---|
| `Explore` | Read 层 9 个 |
| `general-purpose` | Read 层 + `git_add` + `git_commit` + `git_stash_*` + `git_branch_create` |

**不给** subagent：Network 层、Destructive 层、`git_amend_commit`、`git_reset_*`、`git_revert`、`git_branch_delete`、`git_checkout_branch`。

原因：subagent 是"用完即弃"的子任务，给它 push 权限相当于让短任务影响远端历史，得不偿失。

### 6.3 Teammate 分级

| 给/不给 | 工具 |
|---|---|
| 给 | Read 层 9 个 + `git_add` + `git_commit` + `git_branch_create` |
| 不给 | Network 层（push 是 lead 与用户对话的产物，不是后台行为）+ Destructive 层 + amend / reset / revert |

Teammate 的可观测性比 lead 差（跑在后台线程），所以给的越少越好。需要 push 时 `send_message("lead", "please push branch foo")` 走 inbox。

## 7. 与现有机制的交互

| 机制 | 交互 |
|---|---|
| `safe_path` | 所有路径参数必须过 |
| `safe_subprocess_run` | 不直接用（它走 `shell=True`），git 工具自己用 `subprocess.run([...])` 列表形式 |
| `ToolResult` 状态前缀 | 严格映射：写 = CHANGED；读 = NO_CHANGE；fast-forward 已是最新 = NO_CHANGE |
| `LoopDetector` | 模型对同一个 `git_status` 重复调多次会触发 loop detector → 自动收敛。`git_status` 是常见"模型反复确认状态"的源头，loop guard 接住正好 |
| `ProgressTracker` | 写工具的输出差异化（含 commit hash、文件列表）→ 不会被识别为无进展 |
| 钩子机制 | PreToolUse/PostToolUse 都能用 `matcher=git_*` 精确审计；推荐配合 `audit_git_push.py` |
| `MAX_TOOL_CALLS` | 普通限额；git 调用计入 |
| Hooks 的 `HOOK_OUTPUT_STATUS` | git 写工具产出 CHANGED，让审计脚本能筛 |

## 8. 边界场景

| 场景 | 行为 |
|---|---|
| WORKDIR 不是 git 仓库 | 首次调用任意 git 工具 → ERROR `not a git repository at <WORKDIR>`，结果缓存 |
| 有未解决的 merge 冲突 | `git_status` 正常列出；其他写工具检测 `.git/MERGE_HEAD` 存在 → ERROR 让模型先解决 |
| Detached HEAD | `git_current_branch` 返回 `HEAD`；`git_push` 在 detached 状态拒绝；`git_branch_create` 允许（建议路径） |
| 没装 git 二进制 | `_run_git` 抛 `FileNotFoundError` → 转 ERROR `git binary not found` |
| 远端凭据缺失 | `git_push` 返回 git 自己的 stderr（含 `Authentication failed`），不做额外包装 |
| pre-commit hook 失败 | `git_commit` 返回 ERROR + 完整 stderr；**不重试、不 `--no-verify`** |
| 提交信息含 shell 元字符 | 因为不走 shell，原样传递；git 自己处理 |
| 用户在 `.gitignore` 里加了正在编辑的文件 | `git_add` 会被 git 自身拒绝，stderr 透传给模型 |
| 跨平台换行（CRLF 警告） | git 自己输出 warning 到 stderr；ToolResult 把 stderr 拼到 content 末尾 |

## 9. 测试计划

`tests/test_git_tools.py`：

每个测试用 `tmp_path` 起一个独立临时 git 仓库（`git init` + 配置 user.name / user.email），跑工具校验行为。

最小覆盖：

- `git_status` 在新仓库 / 有未追踪文件 / 有 staged 文件三种状态下的输出
- `git_add` 校验 `safe_path`：传 `../escape.txt` 必须 ERROR
- `git_commit` 在 staged 非空 / 空两种情况
- `git_commit` 的 message 长度校验
- `git_branch_create` 名称白名单（拒绝 `bad name`、`foo;rm`）
- `git_push` 安全栏：`force=True` 但环境变量未设 → ERROR
- `git_push` 安全栏：`force=True` + 环境变量已设 + 目标 = main → 仍 ERROR
- `git_reset_soft` 没传 `confirm=True` → ERROR
- `git_restore_file` 没传 `confirm=True` → ERROR
- 不是 git 仓库时所有工具返回固定 ERROR
- 没装 git 时 `_run_git` 优雅降级（用 mock 模拟 `FileNotFoundError`）

不测：`git_push` / `git_fetch` / `git_pull` 的真实网络行为（用 stub 远端）。

## 10. 分期落地

### Phase A —— Read 层（一周内可上线）

1. `tools/git_tools.py` 框架 + `_run_git` + `_assert_in_repo` + 缓存
2. 9 个 Read 工具
3. `ToolDispatcher` 接入 + subagent Explore 同步
4. `tests/test_git_tools.py` 覆盖 Read 层
5. 文档：`CLAUDE.md` 加一节"Git 工具"

### Phase B —— Write-Local 层

1. `git_add` / `git_unstage` / `git_stash_*` / `git_branch_create` / `git_checkout_branch`
2. `git_commit`（含 staged 检查、message 校验）
3. subagent general-purpose + teammate 同步
4. 测试补全

### Phase C —— Mutating + Network 层

1. `git_amend_commit` / `git_revert` / `git_reset_soft` / `git_reset_mixed` / `git_tag_create`
2. `git_fetch` / `git_pull` / `git_push`（含安全栏）
3. 示例钩子：`hooks/audit_git_push.py` + `hooks/audit_git_amend.py`
4. `GENA_ALLOW_FORCE_PUSH` 环境变量文档化

### Phase D —— Destructive 层

1. `git_restore_file` / `git_branch_delete`（都要 `confirm=True`）
2. 钩子示例：`hooks/audit_git_restore.py`

### 不做：rebase / merge / clean

明确文档化"这三类操作请走 `bash`"，并配套钩子审计示例。

## 11. 风险与权衡

| 风险 | 缓解 |
|---|---|
| 工具表膨胀（gena 已有 30+ 工具，再加 20+ git 工具） | 分级展示——可考虑只把 Read 层 + Write-Local 层默认开启，Mutating / Network 层走 `load_skill` 风格按需暴露 |
| 模型仍可走 `bash` 绕过安全栏 | 钩子审计 + system prompt 引导"优先用 `git_*`" |
| 跨平台行为差异（Windows git 行尾、路径分隔符） | 工具内部统一用 forward-slash 路径；stderr 透传原文，不解析 |
| 缓存"是否 git 仓库"导致用户后期 `git init` 不生效 | 缓存只活在进程；REPL 退出即清。如需重置加 `git_refresh_repo_state` 工具，但 v1 不做 |
| `git_pull --ff-only` 太严格，用户做开发时常被拒 | 文档明示；非 ff 让模型选择 `git_fetch` + `git_status` 排查后用 bash 做 rebase/merge |
| 模型把 ERROR 当 NO_CHANGE 读了导致循环 | ToolResult 的 `[ERROR]` 前缀已经显式；LoopDetector 仍会兜底 |
| force-push 的安全栏被环境变量绕过 | 环境变量是"我有意为之"的明确表态，且 main/master/release 仍硬禁；最后一道防线是用户的 git server 端 |

## 12. 验收（Phase A）

- 新加的 Read 工具不需要 `.claude/.claude_trusted` 等额外开关，开箱即用
- 在非 git 仓库目录跑 `python main.py`，模型调 `git_status` 拿到 `[ERROR] not a git repository ...`，不崩
- LoopDetector 能在模型连续调 3 次 `git_status` 时触发 force-conclusion（已有行为，仅验证）
- `pytest tests/test_git_tools.py` 全绿
- `python main.py` 烟囱测试：模型一次 `git_status` + `git_diff` + 自然描述，不调 bash

## 13. 后续可选

- **Git LFS 工具**：`git_lfs_track` / `git_lfs_status`
- **Submodule 工具**：`git_submodule_status` / `git_submodule_update`（只读优先）
- **gh CLI 工具**：`gh_pr_create` / `gh_pr_view` / `gh_issue_list` —— 严格说不属于 git，但与 git 工作流紧密耦合，可考虑作为后续 `tools/gh_tools.py`
- **Worktree 工具**：`git_worktree_list` / `git_worktree_add`
- **配置热加载**：允许运行时切换 `GENA_ALLOW_FORCE_PUSH` 等环境变量

## 附录 A：工具数量速览

| 层 | 工具数 | 累计 |
|---|---|---|
| Read | 9 | 9 |
| Write-Local | 6 | 15 |
| Write-Local-Mutating | 7 | 22 |
| Network | 3 | 25 |
| Destructive | 2 | 27 |

合计 **27 个 git 工具**。Lead 全开；subagent Explore 只开 9 个；subagent general-purpose 开 16 个；teammate 开 12 个。

## 附录 B：薄方案 vs 厚方案对比

| 维度 | 薄方案（仅钩子审计） | 厚方案（本方案） |
|---|---|---|
| 实现工作量 | 1 个 `hooks/audit_git.py` + 一段正则 | 1 个 `tools/git_tools.py`（~600 行）+ 测试 + 文档 |
| 模型体验 | 仍然走 `bash`，认知负担与 LLM 内的 git 知识对齐 | 工具表里看到 `git_*`，调用是"动词级"的，规划更自然 |
| 安全栏 | 软性（钩子里正则匹配，可能漏） | 硬性（工具签名不暴露危险参数） |
| 与 `safe_path` 集成 | 无 | 有 |
| 状态信号（NO_CHANGE / CHANGED） | 无 | 有，对 LoopDetector / ProgressTracker 友好 |
| 测试 | 1 个钩子脚本测试 | 完整工具单元测试 |
| 可扩展性 | 加新模式 = 改正则 | 加新工具 = 加函数 + schema |
| 推荐场景 | 临时 / 已有 bash 工作流深 / 不希望增加工具表 | 长期 / 想让模型有"git 是受控操作"的语义 |

**结论**：两者不互斥。厚方案上线后，仍然建议保留 `PreToolUse matcher=bash` 钩子审计危险 git 命令，作为最后一道防线。

## 14. 实现状态（Phase A + B 已完成；Phase C/D 待办）

### 14.1 已落地（17 个工具）

**Phase A —— Read（9）**：`git_status` / `git_current_branch` / `git_diff` / `git_log` / `git_show` / `git_blame` / `git_branch_list` / `git_remote_list` / `git_tag_list`

**Phase B —— Write-Local（8）**：`git_add` / `git_unstage` / `git_commit` / `git_stash_save` / `git_stash_list` / `git_stash_pop` / `git_branch_create` / `git_checkout_branch`

**接入分布**：
| 角色 | 工具 |
|---|---|
| Lead（`ToolDispatcher`） | 全部 17 个 |
| Subagent Explore | Read 9 个 |
| Subagent general-purpose | Read 9 + `git_add` / `git_commit` / `git_stash_save` / `git_stash_list` / `git_stash_pop` / `git_branch_create` = **15 个**（不给 `unstage` / `checkout_branch` —— 长事务决策应回 lead） |
| Teammate | Read 9 + `git_add` / `git_commit` / `git_branch_create` = **12 个** |

**安全栏（已实施）**：
- 不走 `shell=True`；所有 `_run_git(args=[...])` 列表形式
- 路径参数都过 `_safe_pathspec`（含 pathspec magic 拒绝）
- 分支名走 `_BRANCH_NAME_RE = ^[A-Za-z0-9._\-/]{1,128}$` 白名单
- `git_commit` 强制 staged 非空 + message ≥ 3 字符 + 检测 MERGE_HEAD
- 永不传 `--no-verify` / `--no-gpg-sign`
- `_GIT_REPO_CACHE` 缓存仓库检测；非 git 仓库统一返回 ERROR
- `git_show` 拒绝含 shell 元字符的 ref

**测试**：`tests/test_git_tools.py` 37 个测试全绿；全套从 99 涨到 **136**。

**Teammate 修复**：之前 `content_text = str(output)` 在 ToolResult 上输出 dataclass repr（SSH 工具早已存在的 bug，git 工具引入时一并修），现在改为对 ToolResult 调 `to_llm_format()`。

### 14.2 Phase C 待办（Mutating + Network）

| 工具 | 关键安全栏 |
|---|---|
| `git_amend_commit` | `confirm=True` 必填；rebase 中拒绝 |
| `git_revert <ref>` | 不接受 `<ref>..<ref>` 范围 |
| `git_reset_soft <ref>` | `confirm=True` 必填 |
| `git_reset_mixed <ref>` | `confirm=True` 必填 |
| `git_tag_create <name> [<ref>]` | name 走白名单 |
| `git_fetch [<remote>]` | 无需特殊安全栏 |
| `git_pull [<remote>] [<branch>]` | 默认 `--ff-only`；非 ff 直接报错 |
| `git_push <remote> <branch> [force]` | **三重门**：`force=True` 参数 + `GENA_ALLOW_FORCE_PUSH=true` 环境变量 + 目标分支 ∉ `_PROTECTED_BRANCHES`；实际用 `--force-with-lease` 而非 `--force` |

**配套（Phase C 落地时一起做）**：
- `hooks/audit_git_push.py` 示例
- `hooks/audit_git_amend.py` 示例
- `GENA_ALLOW_FORCE_PUSH` 环境变量在 `config/settings.py` 文档化（不需要新增常量；直接 `os.environ.get` 即可）

### 14.3 Phase D 待办（Destructive）

| 工具 | 安全栏 |
|---|---|
| `git_restore_file <path> confirm=True` | 必须 confirm；返回前打印即将丢弃内容的前 200 字符预览 |
| `git_branch_delete <name> confirm=True` | 必须 confirm；目标 ∉ `_PROTECTED_BRANCHES` |

**配套**：`hooks/audit_git_restore.py` 示例

### 14.4 不实现（明确放弃）

走 `bash` + `PreToolUse matcher=bash` 钩子审计：
- `git reset --hard`
- `git clean -fd`
- `git rebase`（含 interactive）
- `git merge`（冲突解决是协作式的）
- `git filter-branch` / `git filter-repo`
- `git push --mirror`

### 14.5 续做提示（给下一个会话）

1. **从哪里改起**：`tools/git_tools.py` 模块底部追加 Phase C/D 函数 —— 风格参考已有的 `git_commit` / `git_branch_create`
2. **测试搭好了**：`tests/test_git_tools.py::git_repo` fixture 可直接复用，新工具的测试照 Phase B 的样式加
3. **ToolDispatcher 同步**：`tools/tool_dispatcher.py::get_handler` 与 `get_tools` 都要加
4. **subagent / teammate**：Phase C 的 `fetch` / `pull` 可以下发到 subagent general-purpose；`push` / `reset_*` / `revert` / `amend` 只给 lead；Phase D 全部只给 lead
5. **环境变量**：`GENA_ALLOW_FORCE_PUSH` 是 lazy 读 —— `os.environ.get("GENA_ALLOW_FORCE_PUSH", "").lower() in ("1", "true", "yes")`
6. **保护分支列表**：`_PROTECTED_BRANCHES` 常量已经在 `tools/git_tools.py` 顶部，Phase C 的 `git_push` 直接复用
7. **示例钩子**：`hooks/audit_git_*.py` 写法参考 `hooks/audit_bash.py` —— 它们都是 PreToolUse 阻断风格
