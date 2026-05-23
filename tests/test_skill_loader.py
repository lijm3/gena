"""测试 managers/skill_loader"""
from managers.skill_loader import SkillLoader


class TestSkillLoader:
    def _write_skill(self, root, name, meta_lines, body):
        d = root / name
        d.mkdir(parents=True)
        meta = "\n".join(meta_lines)
        (d / "SKILL.md").write_text(f"---\n{meta}\n---\n{body}", encoding="utf-8")

    def test_empty_dir(self, tmp_path):
        sl = SkillLoader(skills_dir=tmp_path)
        assert sl.descriptions() == "(no skills)"
        assert "Unknown skill" in sl.load("anything")

    def test_loads_skill_with_metadata(self, tmp_path):
        self._write_skill(
            tmp_path, "echo",
            ["name: echo", "description: prints stuff"],
            "Use bash echo to print.",
        )
        sl = SkillLoader(skills_dir=tmp_path)
        desc = sl.descriptions()
        assert "echo" in desc
        assert "prints stuff" in desc

        body = sl.load("echo")
        assert "Use bash echo" in body
        assert "<skill" in body

    def test_unknown_skill(self, tmp_path):
        self._write_skill(
            tmp_path, "echo",
            ["name: echo", "description: x"],
            "body",
        )
        sl = SkillLoader(skills_dir=tmp_path)
        out = sl.load("nope")
        assert "Unknown skill" in out
        assert "echo" in out  # 列出可用 skills

    def test_skill_without_frontmatter(self, tmp_path):
        d = tmp_path / "noframe"
        d.mkdir()
        (d / "SKILL.md").write_text("just body, no frontmatter", encoding="utf-8")
        sl = SkillLoader(skills_dir=tmp_path)
        # 应使用目录名作为 fallback name
        assert "noframe" in sl.skills
