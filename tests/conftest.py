"""
pytest 全局 fixture。

关键挑战：项目用 `from config.settings import DB_PATH` 这种模块级常量绑定，
import 后改 settings.DB_PATH 不会传播到已经 import 的 task_manager / message_bus。
解决：每个测试 monkeypatch 直接改 task_manager / message_bus 模块内的 DB_PATH 引用。
"""
import os
import sys
from pathlib import Path

import pytest

# 让测试能 import 仓库根的模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 避免 settings 在 import 时因为 ANTHROPIC_AUTH_TOKEN 缺失而炸（虽然 settings 本身只在 get_auth_token 才检查）
os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token-not-used")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """
    给 TaskManager / MessageBus 提供隔离的临时 SQLite 库。
    每个测试单独建库，互不污染。
    """
    db = tmp_path / "test.db"
    # 直接 patch 已 import 模块里的常量副本
    monkeypatch.setattr("managers.task_manager.DB_PATH", db)
    monkeypatch.setattr("managers.message_bus.DB_PATH", db)
    return db


@pytest.fixture
def tmp_workdir(tmp_path, monkeypatch):
    """
    把所有路径常量重定向到临时目录。供 path_utils / SkillLoader / compression 等测试用。
    """
    monkeypatch.setattr("config.settings.WORKDIR", tmp_path)
    monkeypatch.setattr("config.settings.TASKS_DIR", tmp_path / ".tasks")
    monkeypatch.setattr("config.settings.TEAM_DIR", tmp_path / ".team")
    monkeypatch.setattr("config.settings.INBOX_DIR", tmp_path / ".team" / "inbox")
    monkeypatch.setattr("config.settings.SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr("config.settings.TRANSCRIPT_DIR", tmp_path / ".transcripts")
    # path_utils.safe_path 用的是从 config.settings 重新导入的 WORKDIR；下面这行是关键
    monkeypatch.setattr("utils.path_utils.WORKDIR", tmp_path)
    return tmp_path
