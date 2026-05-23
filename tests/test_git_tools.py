"""
Git 工具单元测试 - Phase A + B。

每个测试用 tmp_path 起独立仓库（避免互相污染），通过 monkeypatch 把 config.settings.WORKDIR
重定向到临时目录，再 reload git_tools 模块拿到新 WORKDIR 引用。

要点：
- git_tools 在 import 时就 from config.settings import WORKDIR 抓了引用，
  所以 monkeypatch config.settings.WORKDIR 后还要 monkeypatch tools.git_tools.WORKDIR
  和 utils.path_utils.WORKDIR（safe_path 也用了这个常量）
- _GIT_REPO_CACHE 是模块级，每个测试 setUp 时调 _reset_repo_cache() 重置
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token-not-used")


# ============================================================
# fixtures
# ============================================================

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """直接调 git 命令（测试搭建用，不走 git_tools）。"""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch):
    """初始化一个空仓库，配置好 user.* 与默认分支，返回路径。"""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    # 重定向所有 WORKDIR 引用
    monkeypatch.setattr("config.settings.WORKDIR", tmp_path)
    monkeypatch.setattr("utils.path_utils.WORKDIR", tmp_path)
    import tools.git_tools as gt
    monkeypatch.setattr(gt, "WORKDIR", tmp_path)
    gt._reset_repo_cache()
    return tmp_path


@pytest.fixture
def not_a_repo(tmp_path: Path, monkeypatch):
    """一个空目录（非 git 仓库）。"""
    monkeypatch.setattr("config.settings.WORKDIR", tmp_path)
    monkeypatch.setattr("utils.path_utils.WORKDIR", tmp_path)
    import tools.git_tools as gt
    monkeypatch.setattr(gt, "WORKDIR", tmp_path)
    gt._reset_repo_cache()
    return tmp_path


def _commit_file(repo: Path, name: str, content: str = "x\n") -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")


# ============================================================
# _assert_in_repo
# ============================================================

def test_not_a_repo_returns_error(not_a_repo):
    from tools.git_tools import git_status
    r = git_status()
    assert r.error is True
    assert "not a git repository" in r.content


def test_repo_detected_and_cached(git_repo):
    from tools.git_tools import git_status, _GIT_REPO_CACHE  # noqa
    r = git_status()
    assert r.error is False
    import tools.git_tools as gt
    assert gt._GIT_REPO_CACHE is True


# ============================================================
# Phase A —— Read 层
# ============================================================

def test_git_status_clean(git_repo):
    from tools.git_tools import git_status
    r = git_status()
    assert r.error is False
    # porcelain v2 输出至少有 branch.head 行
    assert "branch.head" in r.content


def test_git_status_with_changes(git_repo):
    from tools.git_tools import git_status
    (git_repo / "new.txt").write_text("hi", encoding="utf-8")
    r = git_status()
    assert "new.txt" in r.content


def test_git_current_branch(git_repo):
    from tools.git_tools import git_current_branch
    r = git_current_branch()
    assert r.error is False
    assert r.content.strip() == "main"


def test_git_diff_empty(git_repo):
    from tools.git_tools import git_diff
    _commit_file(git_repo, "a.txt")
    r = git_diff()
    assert r.error is False
    # 没有未 staged 改动 → 空
    assert r.content.strip() in ("", "(no output)")


def test_git_diff_unstaged(git_repo):
    from tools.git_tools import git_diff
    _commit_file(git_repo, "a.txt")
    (git_repo / "a.txt").write_text("changed\n", encoding="utf-8")
    r = git_diff()
    assert "changed" in r.content or "a.txt" in r.content


def test_git_diff_staged_flag(git_repo):
    from tools.git_tools import git_diff
    _commit_file(git_repo, "a.txt")
    (git_repo / "a.txt").write_text("staged-change\n", encoding="utf-8")
    _git(git_repo, "add", "a.txt")
    r = git_diff(staged=True)
    assert "staged-change" in r.content or "a.txt" in r.content


def test_git_diff_path_safe(git_repo):
    """git_diff 的 path 走 safe_path，路径逃逸被拒。"""
    from tools.git_tools import git_diff
    r = git_diff(path="../escape")
    assert r.error is True


def test_git_log(git_repo):
    from tools.git_tools import git_log
    _commit_file(git_repo, "a.txt")
    _commit_file(git_repo, "b.txt")
    r = git_log(limit=10)
    assert r.error is False
    assert "add a.txt" in r.content
    assert "add b.txt" in r.content


def test_git_log_limit_validation(git_repo):
    from tools.git_tools import git_log
    assert git_log(limit=0).error
    assert git_log(limit=501).error
    assert git_log(limit="x").error  # type: ignore


def test_git_show(git_repo):
    from tools.git_tools import git_show, git_log
    _commit_file(git_repo, "a.txt")
    rl = git_log(limit=1)
    sha = rl.content.split()[0]
    r = git_show(ref=sha)
    assert r.error is False
    assert "a.txt" in r.content


def test_git_show_rejects_shell_meta(git_repo):
    from tools.git_tools import git_show
    assert git_show(ref="HEAD; rm -rf /").error
    assert git_show(ref="HEAD | cat").error


def test_git_blame(git_repo):
    from tools.git_tools import git_blame
    _commit_file(git_repo, "a.txt", "hello\nworld\n")
    r = git_blame(path="a.txt")
    assert r.error is False
    assert "hello" in r.content or "world" in r.content


def test_git_blame_line_range(git_repo):
    from tools.git_tools import git_blame
    _commit_file(git_repo, "a.txt", "L1\nL2\nL3\n")
    r = git_blame(path="a.txt", line_start=1, line_end=2)
    assert r.error is False
    assert "L1" in r.content and "L2" in r.content


def test_git_branch_list(git_repo):
    from tools.git_tools import git_branch_list
    _commit_file(git_repo, "a.txt")
    r = git_branch_list()
    assert r.error is False
    assert "main" in r.content


def test_git_remote_list_empty(git_repo):
    from tools.git_tools import git_remote_list
    r = git_remote_list()
    assert r.error is False  # 没 remote 但不报错


def test_git_tag_list_empty(git_repo):
    from tools.git_tools import git_tag_list
    r = git_tag_list()
    assert r.error is False


# ============================================================
# Phase B —— Write-Local 层
# ============================================================

def test_git_add_safe_pathspec(git_repo):
    from tools.git_tools import git_add
    (git_repo / "f.txt").write_text("x", encoding="utf-8")
    r = git_add(["f.txt"])
    assert r.error is False
    assert r.changed is True


def test_git_add_rejects_escape(git_repo):
    from tools.git_tools import git_add
    r = git_add(["../escape.txt"])
    assert r.error is True


def test_git_add_rejects_pathspec_magic(git_repo):
    from tools.git_tools import git_add
    r = git_add([":!*.py"])
    assert r.error is True


def test_git_add_rejects_empty_list(git_repo):
    from tools.git_tools import git_add
    r = git_add([])
    assert r.error is True


def test_git_unstage(git_repo):
    from tools.git_tools import git_add, git_unstage, git_status
    _commit_file(git_repo, "a.txt")  # baseline commit
    (git_repo / "b.txt").write_text("new", encoding="utf-8")
    git_add(["b.txt"])
    r = git_unstage(["b.txt"])
    assert r.error is False
    # 验证 b.txt 现在是 untracked
    st = git_status()
    assert "b.txt" in st.content


def test_git_commit_no_staged(git_repo):
    from tools.git_tools import git_commit
    r = git_commit("nothing to commit")
    assert r.error is True
    assert "staged" in r.content


def test_git_commit_message_too_short(git_repo):
    from tools.git_tools import git_add, git_commit
    (git_repo / "f.txt").write_text("x", encoding="utf-8")
    git_add(["f.txt"])
    r = git_commit("xx")  # < 3 字符
    assert r.error is True


def test_git_commit_success(git_repo):
    from tools.git_tools import git_add, git_commit
    (git_repo / "f.txt").write_text("x", encoding="utf-8")
    git_add(["f.txt"])
    r = git_commit("add f.txt")
    assert r.error is False
    assert r.changed is True
    assert r.done is True


def test_git_commit_rejects_during_merge_conflict(git_repo):
    """有 .git/MERGE_HEAD 时拒绝 commit。"""
    from tools.git_tools import git_commit
    (git_repo / ".git" / "MERGE_HEAD").write_text("fake")
    r = git_commit("trying during merge")
    assert r.error is True
    assert "merge" in r.content.lower()


def test_git_stash_save_no_changes(git_repo):
    from tools.git_tools import git_stash_save
    _commit_file(git_repo, "a.txt")
    r = git_stash_save("nothing")
    # 没东西可 stash 时 git 输出 "No local changes" + 返回 0；我们给 NO_CHANGE
    assert r.error is False
    assert r.changed is False


def test_git_stash_save_and_pop(git_repo):
    from tools.git_tools import git_stash_save, git_stash_list, git_stash_pop
    _commit_file(git_repo, "a.txt")
    (git_repo / "a.txt").write_text("modified\n", encoding="utf-8")
    s = git_stash_save("wip")
    assert s.error is False and s.changed is True
    lst = git_stash_list()
    assert "wip" in lst.content
    p = git_stash_pop()
    assert p.error is False


def test_git_stash_save_requires_message(git_repo):
    from tools.git_tools import git_stash_save
    assert git_stash_save("").error


def test_git_branch_create(git_repo):
    from tools.git_tools import git_branch_create, git_current_branch
    _commit_file(git_repo, "a.txt")
    r = git_branch_create("feature/x")
    assert r.error is False
    assert git_current_branch().content.strip() == "feature/x"


def test_git_branch_create_rejects_bad_name(git_repo):
    from tools.git_tools import git_branch_create
    _commit_file(git_repo, "a.txt")
    assert git_branch_create("bad name").error           # 空格
    assert git_branch_create("foo;rm").error             # 分号
    assert git_branch_create("/bad").error               # 起 /
    assert git_branch_create("bad/").error               # 止 /
    assert git_branch_create("a..b").error               # ..
    assert git_branch_create("").error                   # 空


def test_git_checkout_branch(git_repo):
    from tools.git_tools import git_branch_create, git_checkout_branch, git_current_branch
    _commit_file(git_repo, "a.txt")
    git_branch_create("feature/y")
    r = git_checkout_branch("main")
    assert r.error is False
    assert git_current_branch().content.strip() == "main"


def test_git_checkout_branch_rejects_dash(git_repo):
    """不允许 `git checkout -` 简写。"""
    from tools.git_tools import git_checkout_branch
    r = git_checkout_branch("-")
    assert r.error is True


def test_git_checkout_branch_rejects_path_like(git_repo):
    """分支名通过白名单正则，含 `--` 自然被拒。"""
    from tools.git_tools import git_checkout_branch
    r = git_checkout_branch("-- file.txt")
    assert r.error is True


# ============================================================
# 状态信号（CHANGED / NO_CHANGE / DONE）
# ============================================================

def test_read_tools_produce_no_change_status(git_repo):
    from tools.git_tools import git_status, git_log, git_branch_list
    _commit_file(git_repo, "a.txt")
    for tool in (git_status, git_log, git_branch_list):
        r = tool() if tool is not git_log else git_log(limit=5)
        assert r.changed is False, f"{tool.__name__} 应该是 NO_CHANGE"
        # to_llm_format 应包含 NO_CHANGE
        assert "NO_CHANGE" in r.to_llm_format()


def test_git_commit_produces_done_changed_status(git_repo):
    from tools.git_tools import git_add, git_commit
    (git_repo / "f.txt").write_text("x", encoding="utf-8")
    git_add(["f.txt"])
    r = git_commit("add f.txt")
    fmt = r.to_llm_format()
    assert "DONE" in fmt and "CHANGED" in fmt
