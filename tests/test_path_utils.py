"""测试 utils/path_utils.safe_path"""
import pytest

from utils.path_utils import safe_path


class TestSafePath:
    def test_relative_inside_workdir(self, tmp_workdir):
        p = safe_path("foo/bar.txt")
        assert p.is_absolute()
        # tmp_workdir 本身可能也是符号链接（macOS tmp 常见）；用 resolve 后比较
        assert p.is_relative_to(tmp_workdir.resolve())

    def test_dot_path_stays_inside(self, tmp_workdir):
        p = safe_path(".")
        assert p == tmp_workdir.resolve() or p == tmp_workdir

    def test_path_escape_rejected(self, tmp_workdir):
        with pytest.raises(ValueError, match="逃逸"):
            safe_path("../outside.txt")

    def test_deep_path_escape_rejected(self, tmp_workdir):
        with pytest.raises(ValueError, match="逃逸"):
            safe_path("a/b/../../../../etc/passwd")

    def test_normal_subdir(self, tmp_workdir):
        p = safe_path("subdir/file.txt")
        assert "subdir" in str(p)
        # 实际不需要文件存在，safe_path 只做路径解析
