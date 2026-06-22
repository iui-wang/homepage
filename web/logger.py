"""单例 logger：文件（每日滚动，保留 14 天）+ 控制台。

用法：
    from logger import get_logger
    log = get_logger(__name__)
    log.info("hello")

日志默认写到本文件同级的 logs/ 目录，每个 name 一个 <name>.log。
如需改目录，设置环境变量 LOG_DIR，或直接改下面的 LOG_DIR。
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.environ.get("LOG_DIR") or Path(__file__).resolve().parent / "logs")
LOG_BACKUP_DAYS = 14

_loggers: dict[str, logging.Logger] = {}


def get_logger(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="[%(asctime)s.%(msecs)03d][%(filename)s:%(lineno)d][%(funcName)s][%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=str(LOG_DIR / f"{name}.log"),
        when="midnight",
        backupCount=LOG_BACKUP_DAYS,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    _loggers[name] = logger
    return logger
