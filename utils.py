import os
from typing import Optional
from astrbot.api.star import Context
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

# 我们将 get_persona_id 移到这里，作为一个独立的工具函数，
# 这样 main.py 和其他未来可能需要它的文件都可以方便地调用。

async def get_persona_id(context: Context, event: AstrMessageEvent) -> Optional[str]:
    """
    获取当前会话的人格 ID。
    如果当前会话没有特定人格，则返回 AstrBot 的默认人格。
    """
    try:
        session_id = await context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        conversation = await context.conversation_manager.get_conversation(
            event.unified_msg_origin, session_id
        )
        persona_id = conversation.persona_id if conversation else None

        # 如果无人格或明确设置为None，则使用全局默认人格
        if not persona_id or persona_id == "[%None]":
            default_persona = context.provider_manager.selected_default_persona
            persona_id = default_persona["name"] if default_persona else None

        return persona_id
    except Exception as e:
        # 在某些情况下（如无会话），获取可能会失败，返回 None
        logger.debug(f"获取人格ID时发生错误: {e}")
        return None

def get_role_name_without_ext(role_name: str) -> str:
    """
    移除人格名称中的 .md 后缀。
    """
    return role_name.replace(".md", "") if role_name else "default"