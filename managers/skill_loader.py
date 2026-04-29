"""
技能加载器 - s05: 按需加载专业知识
"""
import re
from pathlib import Path
from typing import Dict, Any, Optional

from config.settings import SKILLS_DIR


class SkillLoader:
    """
    技能加载器 - Agent 的"知识库"
    
    特性:
        - 按需加载
        - 避免上下文爆炸
    """
    
    def __init__(self, skills_dir: Optional[Path] = None):
        """
        初始化技能加载器
        
        Args:
            skills_dir: 技能目录路径
        """
        self.skills: Dict[str, Dict[str, Any]] = {}
        self.skills_dir = skills_dir or SKILLS_DIR
        
        if self.skills_dir.exists():
            for f in sorted(self.skills_dir.rglob("SKILL.md")):
                text = f.read_text()
                
                # 解析 YAML 前置元数据
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}
    
    def descriptions(self) -> str:
        """返回所有技能的简短描述"""
        if not self.skills:
            return "(no skills)"
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())
    
    def load(self, name: str) -> str:
        """
        加载指定技能的完整内容
        
        Args:
            name: 技能名称
            
        Returns:
            包装在 XML 标签中的技能内容
        """
        s = self.skills.get(name)
        if not s:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"
