from .main import MyPlugin, AstrBotConfig

__all__ = ["MyPlugin"]

__name__ = "你们的记忆"
__version__ = "1.0.0"
__author__ = "Iris"
__description__ = "使用llm自动总结聊天记录，让ta的记忆活起来。"
__config_class__ = AstrBotConfig