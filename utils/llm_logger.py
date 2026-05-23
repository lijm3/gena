"""
LLM 调用日志 - 进程级追加式 JSONL

设计:
  - 进程启动时懒加载,首次调用 log_call 时打开 .logs/llm_<ts>_<pid>.jsonl
  - 多 teammate 线程并发写同一文件,用 threading.Lock 串行化 append
  - 写盘失败不向上抛——日志是辅助设施,不能拖死 LLM 调用
  - 每行一个 JSON 对象,字段固定:ts / duration_ms / request / response (或 error)
  - 不写 Authorization / x-api-key 等敏感 header(LLMClient 已经只传 payload 进来)

每条日志记录 LLM 看到的完整上下文(system / messages / tools)以及它返回的完整内容,
用于事后复现某一轮决策时模型实际看到了什么。
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


LOG_DIR = Path.cwd() / ".logs"


class _LLMLogger:
    """进程级单例。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Optional[Path] = None
        self._seq = 0  # 调用序号,便于在长文件里定位某一轮

    def _ensure_path(self) -> Optional[Path]:
        """首次使用时创建目录与文件。返回 None 表示创建失败,调用方应静默跳过。"""
        if self._path is not None:
            return self._path
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._path = LOG_DIR / f"llm_{ts}_{os.getpid()}.jsonl"
        except Exception:
            return None
        return self._path

    def log_call(
        self,
        request: Dict[str, Any],
        response: Optional[Dict[str, Any]] = None,
        *,
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> None:
        """追加一条记录。response 与 error 二选一(error 仅在 LLM 调用抛异常时设置)。"""
        with self._lock:
            path = self._ensure_path()
            if path is None:
                return
            self._seq += 1
            entry: Dict[str, Any] = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "seq": self._seq,
                "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
                "request": request,
            }
            if error is not None:
                entry["error"] = error
            else:
                entry["response"] = response
            try:
                # ensure_ascii=False 让中文以原文落盘,grep 能直接搜中文关键字
                line = json.dumps(entry, ensure_ascii=False, default=str)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                # 写盘失败不抛——LLM 调用不能因为日志故障而失败
                pass


# 模块级单例
llm_logger = _LLMLogger()
