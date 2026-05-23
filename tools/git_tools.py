"""
Git 工具集 - Phase A (Read) + Phase B (Write-Local)

设计与决策见 docs/Git 工具支持方案.md。
本模块导出函数 = LLM 工具；私有的 `_run_git` / `_assert_in_repo` / `_safe_pathspec`
统一处理仓库检测、路径校验与 subprocess 编码。

## 实现状态

Phase A（Read，~/9）：
    git_status / git_current_branch / git_diff / git_log / git_show /
    git_blame / git_branch_list / git_remote_list / git_tag_list

Phase B（Write-Local，~/8）：
    git_add / git_unstage / git_stash_save / git_stash_list / git_stash_pop /
    git_branch_create / git_checkout_branch / git_commit

未实现（Phase C/D，按需启用）：
    git_amend_commit / git_revert / git_reset_soft / git_reset_mixed /
    git_tag_create / git_fetch / git_pull / git_push /
    git_restore_file / git_branch_delete

## 安全约定

1. 不走 shell=True；所有调用走 subprocess.run([...]) 列表形式
2. 路径参数都过 _safe_pathspec → safe_path
3. 分支名走 _validate_branch_name 白名单
4. commit message 长度 ≥ 3 字符
5. 永不传 --no-verify / --no-gpg-sign，强制走仓库已有钩子
6. _assert_in_repo 缓存在模块级，REPL 退出即清
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from config.settings import WORKDIR
from utils.loop_control import ToolResult
from utils.logging_setup import get_logger
from utils.path_utils import safe_path

log = get_logger(__name__)

_GIT_REPO_CACHE: Optional[bool] = None
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,128}$")
_PROTECTED_BRANCHES = {"main", "master", "develop"}
_DEFAULT_TIMEOUT = 30


# ============================================================
# 私有辅助
# ============================================================

def _run_git(args: List[str], timeout: int = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """统一的 git 调用入口。args 不含 shell 元字符；不走 shell=True。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(WORKDIR),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def _assert_in_repo() -> Optional[ToolResult]:
    """检查 WORKDIR 是否是 git 仓库；结果缓存。

    返回:
        None      → 是 git 仓库，放行
        ToolResult → 否，ERROR 给模型
    """
    global _GIT_REPO_CACHE
    if _GIT_REPO_CACHE is True:
        return None
    if _GIT_REPO_CACHE is False:
        return ToolResult(
            content=f"[ERROR] not a git repository at {WORKDIR}",
            changed=False, error=True,
        )
    try:
        r = _run_git(["rev-parse", "--is-inside-work-tree"], timeout=5)
    except FileNotFoundError:
        _GIT_REPO_CACHE = False
        return ToolResult(
            content="[ERROR] git binary not found in PATH",
            changed=False, error=True,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            content="[ERROR] git rev-parse timed out",
            changed=False, error=True,
        )
    _GIT_REPO_CACHE = (r.returncode == 0 and r.stdout.strip() == "true")
    if not _GIT_REPO_CACHE:
        return ToolResult(
            content=f"[ERROR] not a git repository at {WORKDIR}",
            changed=False, error=True,
        )
    return None


def _reset_repo_cache() -> None:
    """供测试用：清除 git-repo 检测缓存。"""
    global _GIT_REPO_CACHE
    _GIT_REPO_CACHE = None


def _safe_pathspec(paths: List[str]) -> List[str]:
    """对每个 path 走 safe_path；禁止 pathspec magic（`:` 前缀）。

    Raises:
        ValueError: 路径逃逸或含 pathspec magic
    """
    cleaned: List[str] = []
    for p in paths:
        if not isinstance(p, str) or not p:
            raise ValueError(f"无效的 pathspec: {p!r}")
        if p.startswith(":"):
            raise ValueError(f"pathspec magic 不被允许: {p!r}")
        # safe_path 会拒绝 .. 逃逸
        resolved = safe_path(p)
        # git 需要 forward-slash；用相对仓库根的 posix 路径
        rel = resolved.relative_to(WORKDIR)
        cleaned.append(rel.as_posix() if str(rel) != "." else ".")
    return cleaned


def _validate_branch_name(name: str) -> Optional[str]:
    """返回 None 表示合法；否则返回错误描述。"""
    if not isinstance(name, str) or not name:
        return "分支名不能为空"
    if not _BRANCH_NAME_RE.match(name):
        return f"分支名含非法字符: {name!r}（仅允许字母数字 . _ - /）"
    if name.startswith("/") or name.endswith("/"):
        return f"分支名不能以 / 起止: {name!r}"
    if ".." in name:
        return f"分支名不能含 ..: {name!r}"
    return None


def _wrap_git_result(
    r: subprocess.CompletedProcess,
    *,
    changed: bool,
    done: bool = False,
    success_content: Optional[str] = None,
) -> ToolResult:
    """把 git CompletedProcess 包成 ToolResult。

    returncode != 0 → ERROR + (stderr || stdout)
    returncode == 0 → success_content（默认 stdout）+ stderr 追加为 warning（如果非空）
    """
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip() or f"git exited with code {r.returncode}"
        return ToolResult(content=msg, changed=False, error=True)
    body = success_content if success_content is not None else (r.stdout or "")
    if r.stderr and r.stderr.strip():
        body = f"{body}\n[git warning]\n{r.stderr.strip()}" if body else r.stderr.strip()
    return ToolResult(content=body or "(no output)", changed=changed, done=done)


def _has_merge_conflict() -> bool:
    """检测是否处于未解决的 merge 冲突状态。"""
    return (WORKDIR / ".git" / "MERGE_HEAD").exists()


# ============================================================
# Phase A —— Read 层（9 个）
# ============================================================

def git_status() -> ToolResult:
    """`git status --porcelain=v2 --branch`。"""
    if (err := _assert_in_repo()):
        return err
    r = _run_git(["status", "--porcelain=v2", "--branch"])
    return _wrap_git_result(r, changed=False)


def git_current_branch() -> ToolResult:
    """返回当前分支名（detached HEAD 时返回 'HEAD'）。

    用 symbolic-ref 而非 rev-parse --abbrev-ref，让空仓库（无任何 commit）也能返回。
    """
    if (err := _assert_in_repo()):
        return err
    r = _run_git(["symbolic-ref", "--short", "HEAD"])
    if r.returncode == 0:
        return ToolResult(content=r.stdout.strip() or "HEAD", changed=False)
    # detached HEAD → symbolic-ref 失败；回退 rev-parse 拿短 hash
    r2 = _run_git(["rev-parse", "--short", "HEAD"])
    if r2.returncode == 0:
        return ToolResult(content=f"HEAD (detached at {r2.stdout.strip()})", changed=False)
    return ToolResult(content=(r.stderr or "").strip() or "git failed", changed=False, error=True)


def git_diff(staged: bool = False, stat: bool = False, path: Optional[str] = None) -> ToolResult:
    """`git diff [--cached] [--stat] [-- <path>]`。

    Args:
        staged: True → 看 staged 改动（--cached）
        stat:   True → 输出 --stat 摘要
        path:   可选的路径过滤（走 safe_path）
    """
    if (err := _assert_in_repo()):
        return err
    args = ["diff"]
    if staged:
        args.append("--cached")
    if stat:
        args.append("--stat")
    if path:
        try:
            args += ["--", *_safe_pathspec([path])]
        except ValueError as e:
            return ToolResult(content=f"[ERROR] {e}", changed=False, error=True)
    r = _run_git(args)
    return _wrap_git_result(r, changed=False)


def git_log(limit: int = 20, path: Optional[str] = None) -> ToolResult:
    """`git log --oneline -n <limit> [-- <path>]`。limit 限制 1-500。"""
    if (err := _assert_in_repo()):
        return err
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return ToolResult(content="[ERROR] limit 必须是 1-500 之间的整数", changed=False, error=True)
    args = ["log", "--oneline", f"-n{limit}"]
    if path:
        try:
            args += ["--", *_safe_pathspec([path])]
        except ValueError as e:
            return ToolResult(content=f"[ERROR] {e}", changed=False, error=True)
    r = _run_git(args)
    return _wrap_git_result(r, changed=False)


def git_show(ref: str, stat: bool = False) -> ToolResult:
    """`git show [--stat] <ref>`。"""
    if (err := _assert_in_repo()):
        return err
    if not isinstance(ref, str) or not ref or any(c in ref for c in " \t\n;|&"):
        return ToolResult(content=f"[ERROR] 非法 ref: {ref!r}", changed=False, error=True)
    args = ["show"]
    if stat:
        args.append("--stat")
    args.append(ref)
    r = _run_git(args)
    return _wrap_git_result(r, changed=False)


def git_blame(path: str, line_start: Optional[int] = None, line_end: Optional[int] = None) -> ToolResult:
    """`git blame [-L start,end] -- <path>`。"""
    if (err := _assert_in_repo()):
        return err
    try:
        clean_path = _safe_pathspec([path])[0]
    except ValueError as e:
        return ToolResult(content=f"[ERROR] {e}", changed=False, error=True)
    args = ["blame"]
    if line_start is not None and line_end is not None:
        if not (isinstance(line_start, int) and isinstance(line_end, int)):
            return ToolResult(content="[ERROR] line_start/line_end 必须是整数", changed=False, error=True)
        if line_start < 1 or line_end < line_start:
            return ToolResult(content="[ERROR] 行范围非法", changed=False, error=True)
        args += ["-L", f"{line_start},{line_end}"]
    args += ["--", clean_path]
    r = _run_git(args)
    return _wrap_git_result(r, changed=False)


def git_branch_list() -> ToolResult:
    """`git branch -a`。"""
    if (err := _assert_in_repo()):
        return err
    r = _run_git(["branch", "-a"])
    return _wrap_git_result(r, changed=False)


def git_remote_list() -> ToolResult:
    """`git remote -v`。"""
    if (err := _assert_in_repo()):
        return err
    r = _run_git(["remote", "-v"])
    return _wrap_git_result(r, changed=False)


def git_tag_list() -> ToolResult:
    """`git tag --list`。"""
    if (err := _assert_in_repo()):
        return err
    r = _run_git(["tag", "--list"])
    return _wrap_git_result(r, changed=False)


# ============================================================
# Phase B —— Write-Local 层（8 个）
# ============================================================

def git_add(paths: List[str]) -> ToolResult:
    """`git add -- <paths>`。空列表会被拒绝（避免 git add . 的隐式行为）。"""
    if (err := _assert_in_repo()):
        return err
    if not isinstance(paths, list) or not paths:
        return ToolResult(content="[ERROR] paths 必须是非空列表", changed=False, error=True)
    try:
        clean = _safe_pathspec(paths)
    except ValueError as e:
        return ToolResult(content=f"[ERROR] {e}", changed=False, error=True)
    r = _run_git(["add", "--", *clean])
    return _wrap_git_result(
        r, changed=True,
        success_content=f"已 stage: {', '.join(clean)}",
    )


def git_unstage(paths: List[str]) -> ToolResult:
    """`git reset HEAD -- <paths>` —— 把指定文件从 index 移回 working tree。"""
    if (err := _assert_in_repo()):
        return err
    if not isinstance(paths, list) or not paths:
        return ToolResult(content="[ERROR] paths 必须是非空列表", changed=False, error=True)
    try:
        clean = _safe_pathspec(paths)
    except ValueError as e:
        return ToolResult(content=f"[ERROR] {e}", changed=False, error=True)
    r = _run_git(["reset", "HEAD", "--", *clean])
    return _wrap_git_result(
        r, changed=True,
        success_content=f"已 unstage: {', '.join(clean)}",
    )


def git_commit(message: str) -> ToolResult:
    """`git commit -m <message>`。

    自动检查 staged 非空；message 必填且 ≥3 字符；**禁止 --amend**。
    走仓库已有的 pre-commit / commit-msg 钩子（不传 --no-verify）。
    """
    if (err := _assert_in_repo()):
        return err
    if not isinstance(message, str) or len(message.strip()) < 3:
        return ToolResult(
            content="[ERROR] commit message 必填且至少 3 个非空字符",
            changed=False, error=True,
        )
    if _has_merge_conflict():
        return ToolResult(
            content="[ERROR] 检测到未解决的 merge 冲突（.git/MERGE_HEAD 存在），先用 bash 解决冲突",
            changed=False, error=True,
        )
    # 检查 staged 非空：git diff --cached --quiet 退出 0 = 无 staged
    check = _run_git(["diff", "--cached", "--quiet"])
    if check.returncode == 0:
        return ToolResult(
            content="[ERROR] 没有 staged 改动；先用 git_add",
            changed=False, error=True,
        )
    r = _run_git(["commit", "-m", message])
    if r.returncode != 0:
        return ToolResult(
            content=(r.stderr or r.stdout or "").strip() or "commit 失败",
            changed=False, error=True,
        )
    # 抓新 commit 的 hash + summary
    h = _run_git(["log", "-1", "--oneline"])
    head_line = h.stdout.strip() if h.returncode == 0 else ""
    out = (r.stdout or "").strip()
    body = f"{head_line}\n{out}" if head_line else out
    return ToolResult(content=body or "commit 成功", changed=True, done=True)


def git_stash_save(message: str) -> ToolResult:
    """`git stash push -m <message>`。message 必填，避免无名 stash 堆积。"""
    if (err := _assert_in_repo()):
        return err
    if not isinstance(message, str) or len(message.strip()) < 1:
        return ToolResult(content="[ERROR] stash message 必填", changed=False, error=True)
    r = _run_git(["stash", "push", "-m", message])
    if r.returncode != 0:
        return ToolResult(
            content=(r.stderr or r.stdout or "").strip() or "stash 失败",
            changed=False, error=True,
        )
    out = (r.stdout or "").strip()
    # 没东西可 stash 时 git 输出 "No local changes to save"
    if "No local changes" in out:
        return ToolResult(content=out, changed=False)
    return ToolResult(content=out or "已 stash", changed=True, done=True)


def git_stash_list() -> ToolResult:
    """`git stash list`。"""
    if (err := _assert_in_repo()):
        return err
    r = _run_git(["stash", "list"])
    return _wrap_git_result(r, changed=False)


def git_stash_pop() -> ToolResult:
    """`git stash pop`。有冲突时 git 自身返回非零，原样透传 stderr 让模型自查。"""
    if (err := _assert_in_repo()):
        return err
    r = _run_git(["stash", "pop"])
    if r.returncode != 0:
        return ToolResult(
            content=(r.stderr or r.stdout or "").strip() or "stash pop 失败（可能有冲突）",
            changed=False, error=True,
        )
    return ToolResult(content=(r.stdout or "已 pop").strip(), changed=True, done=True)


def git_branch_create(name: str) -> ToolResult:
    """`git checkout -b <name>` —— 从当前 HEAD 拉新分支并切换过去。"""
    if (err := _assert_in_repo()):
        return err
    if (msg := _validate_branch_name(name)):
        return ToolResult(content=f"[ERROR] {msg}", changed=False, error=True)
    r = _run_git(["checkout", "-b", name])
    if r.returncode != 0:
        return ToolResult(
            content=(r.stderr or r.stdout or "").strip() or "创建分支失败",
            changed=False, error=True,
        )
    return ToolResult(
        content=(r.stdout or r.stderr or f"已创建并切换到 {name}").strip(),
        changed=True, done=True,
    )


def git_checkout_branch(branch: str) -> ToolResult:
    """`git checkout <branch>` —— 切换到已存在的分支。

    不接受 `--`、文件路径或 ref 表达式（避免 git checkout -- file 走这条）。
    """
    if (err := _assert_in_repo()):
        return err
    if (msg := _validate_branch_name(branch)):
        return ToolResult(content=f"[ERROR] {msg}", changed=False, error=True)
    if branch == "-":
        return ToolResult(content="[ERROR] 不支持 - 简写；明确写出分支名", changed=False, error=True)
    r = _run_git(["checkout", branch])
    if r.returncode != 0:
        return ToolResult(
            content=(r.stderr or r.stdout or "").strip() or "切换分支失败",
            changed=False, error=True,
        )
    return ToolResult(
        content=(r.stdout or r.stderr or f"已切到 {branch}").strip(),
        changed=True, done=True,
    )
