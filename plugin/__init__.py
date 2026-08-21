"""
Skill-gardener 硬送达插件：把 .skill-gardener/PENDING.md 的待办注入当前轮 user message。

设计对齐 agent/shell_hooks.py 的 pre_llm_call 语义：
- 无待办 → 返回 None（零注入，不碰 prompt cache）
- 有待办 → 返回 {"context": ...}（固定文本，注入 user message，不进 system prompt）
- 待办过期（>7 天）→ 换文案提示确认/删除，避免永久噪声

cron 周巡检发现待办时写 PENDING.md；本插件在每次 LLM 调用前检查并提醒。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PLUGIN_NAME = "skill-gardener-reminder"
_TTL_SECONDS = 7 * 86400  # 7 天
_REMINDER_TEXT = (
    "⚠️ 技能库有未处理的巡检待办（见 $HERMES_HOME/.skill-gardener/PENDING.md）。"
    "在回复用户之前，先读取该文件，并在合适时机主动提醒用户处理这些待办项。"
)
_STALE_TEXT = (
    "⚠️ .skill-gardener/PENDING.md 已超过 7 天未处理（可能已被手动处理但未删除）。"
    "读取该文件：若待办已处理则删除文件；若仍有效则提醒用户尽快处理。"
)


def _hermes_home() -> Path:
    try:
        from utils import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        import os

        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _pending_file() -> Path:
    return _hermes_home() / ".skill-gardener" / "PENDING.md"


def _check_pending() -> Optional[str]:
    """返回提醒文本；无待办返回 None。"""
    try:
        p = _pending_file()
        if not (p.is_file() and p.stat().st_size > 0):
            return None
        age = time.time() - p.stat().st_mtime
        if age > _TTL_SECONDS:
            return _STALE_TEXT
        return _REMINDER_TEXT
    except OSError:
        return None


def pre_llm_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    text = _check_pending()
    if text:
        logger.info("%s: PENDING.md 存在（age=%.0fs），注入提醒", _PLUGIN_NAME, 
                    time.time() - _pending_file().stat().st_mtime)
        return {"context": text}
    return None


def register(api) -> None:
    api.register_hook("pre_llm_call", pre_llm_call)
    logger.info("%s registered pre_llm_call", _PLUGIN_NAME)
