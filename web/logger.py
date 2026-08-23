"""单例 logger：文件（每日滚动，保留天数可设置）+ 控制台 + error 告警。

用法：
    from logger import get_logger
    log = get_logger(__name__)
    log.info("hello")

日志默认写到本文件同级的 logs/ 目录，每个 name 一个 <name>.log。
如需改目录，设置环境变量 LOG_DIR，或直接改下面的 LOG_DIR。

ERROR 及以上的日志会经 chat 告警机器人私聊发给 admin（ChatAlertHandler），
配置写死在下面的常量里；发送用标准库 urllib，失败静默，不影响业务。
"""

import json
import logging
import os
import socket
import sys
import time
import traceback
import urllib.request
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.environ.get("LOG_DIR", Path(__file__).resolve().parent / "logs"))

# chat 告警配置：告警机器人（chat user_id 102）的 key 与 admin 的 user_id
CHAT_HTTP_BASE = "http://10.77.0.2:5001"
CHAT_KEY = "d6b1d94e-9fd0-4be1-bd02-9f70aed14c86"
CHAT_ADMIN_USER_ID = 1
ALERT_WINDOW_SECONDS = 600  # 滑动窗口：10 分钟
ALERT_MAX_MESSAGES = 20  # 窗口内最多发 20 条
ALERT_TIMEOUT_SECONDS = 5

_loggers: dict[str, logging.Logger] = {}


class ChatAlertHandler(logging.Handler):
    """ERROR 及以上日志经 chat 告警机器人发给 admin；发送失败静默，避免递归。"""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._sent_at: list[float] = []

    def _allowed(self) -> bool:
        now = time.monotonic()
        self._sent_at = [t for t in self._sent_at if now - t < ALERT_WINDOW_SECONDS]
        if len(self._sent_at) >= ALERT_MAX_MESSAGES:
            return False
        self._sent_at.append(now)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        if not self._allowed():
            return
        text = (
            f"[{record.name}] error 告警\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.created))}\n"
            f"机器：{socket.gethostname()}\n"
            f"cwd：{os.getcwd()}\n"
            f"位置：{record.filename}:{record.lineno} ({record.funcName})\n"
            f"信息：{record.getMessage()}"
        )
        if record.exc_info:
            text += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        try:
            payload = json.dumps(
                {"to_user_id": CHAT_ADMIN_USER_ID, "content": text}
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{CHAT_HTTP_BASE}/api/messages?key={CHAT_KEY}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=ALERT_TIMEOUT_SECONDS) as resp:
                resp.read()
        except Exception:
            pass


def get_logger(name: str, log_backup_days: int = 14) -> logging.Logger:
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
        backupCount=log_backup_days,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    # pytest 进程内不挂告警 handler：测试里的 error 路径不该惊动 admin
    if "pytest" not in sys.modules:
        logger.addHandler(ChatAlertHandler())

    _loggers[name] = logger
    return logger
