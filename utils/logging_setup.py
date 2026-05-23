"""
日志初始化 - 多线程友好的统一日志配置

为什么自己写而不是用 dictConfig：
  - dictConfig 配置项更多但学习曲线陡；这个项目就一个 root logger + 两个 handler
  - 显式代码比 YAML/dict 配置更容易看出"线程名/级别在哪格式化的"

设计点：
  - 控制台 INFO+：和 REPL 用户交互混在一起，太啰嗦没人看
  - 文件 DEBUG+：磁盘便宜，排查时不用先调级别再复现
  - 格式包含线程名：队友是后台线程，不带线程名根本分不清谁打的日志
"""
import logging
import sys
from pathlib import Path

_INITIALIZED = False


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    """
    初始化全局日志。重复调用安全（幂等）。

    Args:
        level: 控制台日志级别（DEBUG / INFO / WARNING / ERROR）
        log_file: 文件日志路径；空字符串表示不写文件
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 总开关：DEBUG，由各 handler 决定实际输出
    # 清掉默认 handler，避免和我们加的重复输出
    for h in list(root.handlers):
        root.removeHandler(h)

    # 控制台 handler
    console_fmt = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)s %(threadName)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    console = logging.StreamHandler(sys.stderr)
    try:
        console.setLevel(getattr(logging, level.upper()))
    except AttributeError:
        console.setLevel(logging.INFO)
    console.setFormatter(console_fmt)
    root.addHandler(console)

    # 文件 handler（如配置）
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_fmt = logging.Formatter(
                fmt="[%(asctime)s] %(levelname)s %(threadName)s %(name)s: %(message)s",
            )
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(file_fmt)
            root.addHandler(fh)
        except OSError as e:
            # 文件无法打开（权限/路径问题）不应该让程序起不来。
            console.handle(logging.LogRecord(
                "logging_setup", logging.WARNING, __file__, 0,
                f"file handler disabled: {e}", None, None,
            ))

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """模块拿 logger 的统一入口。等价于 logging.getLogger(name) 但语义更明确。"""
    return logging.getLogger(name)
