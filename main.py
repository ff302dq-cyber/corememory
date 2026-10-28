import os
import re
import json
import shutil
import asyncio
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

import httpx
import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star, Context, StarTools
from astrbot.api.all import *

# 从我们自己的模块中导入类和函数
from .utils import get_persona_id, get_role_name_without_ext
from .memory_manager import MemoryManager
from .semantic_search import SemanticSearch
from .web_manager import WebManager

# ===== 话费账本计费（ledger patch）=====
import contextvars as _ledger_cv
import sys as _ledger_sys
_LEDGER_CTX = _ledger_cv.ContextVar("ledger_billing", default=None)


def _ledger_client():
    m = _ledger_sys.modules.get("astrbot_ledger")
    return getattr(m, "client", None) if m else None


def _ledger_group_from_umo(umo):
    parts = str(umo or "").split(":")
    return parts[-1] if len(parts) >= 3 and "group" in parts[1].lower() else ""


def _ledger_set(qq, umo=None, kind="memory_auto", group_id=None):
    _LEDGER_CTX.set({"qq": str(qq), "kind": kind,
                     "group_id": str(group_id) if group_id else _ledger_group_from_umo(umo)})
# ===== ledger patch end =====

try:
    from astrbot.core.agent.message import TextPart
except Exception:
    TextPart = None

MEMORY_SUMMARIES_DIR = "memory_summaries"
DIARIES_DIR = "diaries"


active_summary_tasks = set()
active_summary_tasks_lock = threading.Lock()

# 剧情框架总结任务的锁
active_plot_tasks = set()
active_plot_tasks_lock = threading.Lock()

# 需要过滤的命令关键词列表（这些消息不会被记录到完整对话日志中）
COMMAND_FILTER_KEYWORDS = [
    "记忆总结", "查看核心记忆", "获取核心记忆", "更新核心文件", "清空核心记忆", "修改核心记忆",
    "查看记忆日志", "获取记忆日志", "更新记忆日志", "清空记忆日志",
    "重建记忆向量",
    "查看剧情框架", "获取剧情框架", "清空剧情框架", "修改剧情框架",
    "剧情总结", "查看剧情总结", "获取剧情总结", "更新剧情总结", "清空剧情总结",
    "总结剧情框架",
    "获取完整对话", "清空完整对话",
    "查看锁定记忆", "锁定记忆", "解锁记忆", "清空锁定记忆", "获取锁定记忆", "更新锁定记忆",
    "ls", "help", "new",
    "provider", "tts", "ttsoff", "ttson", "switch", "history", "重启", "persona", "forget", "cancel_forget",
    # 表情包相关命令
    "存表情包", "存动图", "表情包列表", "删表情包", "复制表情包", "从通用复制表情包",
    "设为通用表情包", "迁移旧表情包", "重新分析表情包", "清空表情包", "清空表情描述", "表情包路径",
    # 记忆模式相关命令
    "查看记忆模式", "切换记忆模式", "迁移到共享模式", "创建共享记忆", "切换共享空间", "查看共享空间",
    "查看总结进度", "设置总结位置",
]

# 需要过滤的消息前缀（以这些字符开头的消息会被过滤）
COMMAND_FILTER_PREFIXES = ['/']


class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir: Optional[str] = None
        self.memory_manager: Optional[MemoryManager] = None
        self.semantic_searcher: Optional[SemanticSearch] = None
        self.web_manager: Optional[WebManager] = None

        self.update_sessions = {}
        self.update_timeout = 30
        self.timeout_tasks = {}

        logger.info("核心记忆插件：核心已创建，准备在后台开始初始化...")
        self._initialization_task = asyncio.create_task(self._initialize_plugin())

    async def _initialize_plugin(self):
        logger.info("核心记忆插件：AstrBot已就绪，开始初始化插件...")
        self.data_dir = os.path.join(StarTools.get_data_dir(), "astrbot_plugin_corememory")

        # 初始化我们的模块化管理器
        self.memory_manager = MemoryManager(self.data_dir)
        self.semantic_searcher = SemanticSearch(self.context, self.config)

        logger.info(f"核心记忆插件初始化完成，数据将保存在: {self.data_dir}")
        setattr(self.context, 'corememory_data_path', self.data_dir)
        logger.info(f"已将核心记忆数据路径注册到全局 context.corememory_data_path")

        # ===== 新增这一行 =====
        setattr(self.context, 'corememory_manager', self.memory_manager)
        logger.info(f"已将 memory_manager 注册到全局 context.corememory_manager")
        # ===== 新增结束 =====

        # 启动Web管理界面
        try:
            self.web_manager = WebManager(self.memory_manager, self.config, self.data_dir, plugin_instance=self)
            await self.web_manager.start()
        except Exception as e:
            logger.error(f"核心记忆插件：Web管理界面启动失败: {e}", exc_info=True)


    def clean_message_metadata(text: str) -> str:
        """清理消息中的平台元数据标记"""
        # 移除 [MSG_ID:数字] 格式的标记
        cleaned = re.sub(r'\s*\[MSG_ID:\d+\]', '', text)
        # 可以继续添加其他需要清理的格式
        return cleaned.strip()


    def _convert_to_host_path(self, container_path: str) -> str:
        """将容器内路径转换为宿主机路径（如果配置了的话）"""
        host_data_path = self.config.get("host_data_path", "")
        if not host_data_path:
            return container_path

        if "/AstrBot/data" in container_path:
            result = container_path.replace("/AstrBot/data", host_data_path)
            logger.info(f"路径转换: {container_path} -> {result}")
            return f"file://{result}"
        return container_path

    def _get_users_to_inject(self, role_name: str, current_user_id: str) -> List[str]:
        """
        根据配置决定需要注入哪些用户的核心记忆和锁定记忆

        Returns:
            用户ID列表
        """
        inject_mode = self.config.get("persona_memory_inject_mode", "current_user")

        if inject_mode == "current_user":
            # 只注入当前用户
            return [current_user_id]

        elif inject_mode == "whitelist":
            # 注入白名单用户
            whitelist_str = self.config.get("persona_whitelist_users", "")
            if not whitelist_str.strip():
                # 白名单为空，回退到当前用户
                return [current_user_id]

            whitelist = [u.strip() for u in whitelist_str.split(",") if u.strip()]
            # 确保当前用户也在列表中
            if current_user_id not in whitelist:
                whitelist.insert(0, current_user_id)
            return whitelist

        elif inject_mode == "all_users":
            max_users = self.config.get("persona_max_users_inject", 5)
            current_space = self.memory_manager.get_current_space(role_name)

            if not current_space:
                return [current_user_id]

            # 使用按活跃度排序的方法
            all_users = self.memory_manager.get_persona_users_by_activity(role_name, current_space, max_users)

            # 确保当前用户在最前面
            if current_user_id in all_users:
                all_users.remove(current_user_id)
            all_users.insert(0, current_user_id)

            return all_users[:max_users]


        else:
            # 未知模式，回退到当前用户
            return [current_user_id]

    def _get_persona_memory_path(self, role_name: str, space_name: str, data_type: str, user_id: str = None) -> str:
        """获取 Persona 模式下的文件路径"""
        role_name = role_name.replace('.md', '').replace('.txt', '')
        dir_name = f"persona_{role_name}_{space_name}"
        base_path = os.path.join(self.memory_manager.persona_memories_path, dir_name)

        # 用户独立数据
        if data_type in ['core_memory', 'locked_memory']:
            if user_id:
                user_dir = os.path.join(base_path, f"user_{user_id}")
            else:
                user_dir = os.path.join(base_path, "shared")
            os.makedirs(user_dir, exist_ok=True)

            if data_type == 'core_memory':
                return os.path.join(user_dir, 'core_memory.json')
            elif data_type == 'locked_memory':
                return os.path.join(user_dir, 'locked_memory.json')
        else:
            # 共享数据
            shared_dir = os.path.join(base_path, "shared")
            os.makedirs(shared_dir, exist_ok=True)

            type_map = {
                'full_log': 'full_log.txt',
                'diary': 'diary.json',
                'diary_vectors': 'diary_vectors.json',
                'plot_framework': 'plot_framework.json',
                'plot_summaries': 'plot_summaries.json',  # 保持这个用于读取
            }

            return os.path.join(shared_dir, type_map.get(data_type, f'{data_type}.json'))

    def _read_persona_core_memory(self, role_name: str, space_name: str, user_id: str) -> Dict:
        """读取 Persona 模式下指定用户的核心记忆"""
        file_path = self._get_persona_memory_path(role_name, space_name, 'core_memory', user_id)
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _read_persona_locked_memories(self, role_name: str, space_name: str, user_id: str) -> List[Dict]:
        """读取 Persona 模式下指定用户的锁定记忆"""
        file_path = self._get_persona_memory_path(role_name, space_name, 'locked_memory', user_id)
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "locked_items" in data:
                        return data["locked_items"]
                    elif isinstance(data, list):
                        return data
            except:
                pass
        return []

    def _extract_nonempty_text(self, content: Any) -> str:
        """从 OpenAI/AstrBot 风格 content 中提取非空纯文本。"""
        if content is None:
            return ""

        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, str):
                    text = part.strip()
                    if text:
                        texts.append(text)
                    continue

                if isinstance(part, dict):
                    part_type = str(part.get("type", "text") or "text")
                    if part_type != "text":
                        continue
                    text = str(part.get("text", "") or "").strip()
                    if text:
                        texts.append(text)
                    continue

                text = str(getattr(part, "text", "") or "").strip()
                if text:
                    texts.append(text)

            return "\n".join(texts).strip()

        if isinstance(content, dict):
            texts = []
            for key in ("text", "content", "message"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
            data = content.get("data")
            if isinstance(data, dict):
                value = data.get("text")
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
            return "\n".join(texts).strip()

        return str(content).strip()

    def _sanitize_context_message(self, msg: Any) -> Optional[Dict[str, str]]:
        """把一条 context 规范成 {role, content}，空内容和工具消息直接丢弃。"""
        if msg is None:
            return None

        if isinstance(msg, dict):
            role = str(msg.get("role", "") or "").strip()
            content = msg.get("content")
            has_tool = any(key in msg for key in ("tool_calls", "tool_call_id", "function_call"))
            msg_type = str(msg.get("type", "") or "").strip()
        else:
            role = str(getattr(msg, "role", "") or "").strip()
            content = getattr(msg, "content", None)
            has_tool = any(hasattr(msg, key) for key in ("tool_calls", "tool_call_id", "function_call"))
            msg_type = str(getattr(msg, "type", "") or "").strip()

        if role not in ("user", "assistant"):
            return None
        if role in ("tool", "function") or has_tool or msg_type == "think":
            return None

        text = self._extract_nonempty_text(content)
        if not text:
            return None

        return {"role": role, "content": text}

    def _sanitize_contexts(self, contexts: Any, source: str) -> List[Dict[str, str]]:
        """过滤 contexts，保证不会把空 part 送给上游模型。"""
        if not contexts:
            return []

        sanitized = []
        skipped = 0
        for msg in list(contexts):
            clean_msg = self._sanitize_context_message(msg)
            if clean_msg:
                sanitized.append(clean_msg)
            else:
                skipped += 1

        if skipped:
            logger.warning(f"[CoreMemory] 已过滤 {source} 中 {skipped} 条空或非法 contexts 消息")
        return sanitized

    def _contexts_to_text(self, contexts: List[Dict[str, str]]) -> str:
        role_labels = {"user": "用户", "assistant": "你"}
        lines = []
        for msg in contexts:
            label = role_labels.get(msg["role"], msg["role"])
            lines.append(f"{label}: {msg['content']}")
        return "\n".join(lines).strip()

    def _append_text_injection(self, req: ProviderRequest, text: str, location: str) -> str:
        """把动态文本注入到 extra_user_content_parts，失败时回退 system_prompt。"""
        text = (text or "").strip()
        if not text:
            return "skipped"

        if location == "extra_user_content_parts" and TextPart is not None:
            try:
                part = TextPart(text=text)
                if hasattr(part, "mark_as_temp"):
                    part.mark_as_temp()
                existing = list(getattr(req, "extra_user_content_parts", []) or [])
                existing.append(part)
                req.extra_user_content_parts = existing
                return "extra_user_content_parts"
            except Exception as e:
                logger.warning(f"[CoreMemory] extra_user_content_parts 注入失败，回退到 system_prompt: {e}")

        if req.system_prompt:
            req.system_prompt += "\n\n" + text
        else:
            req.system_prompt = text
        return "system_prompt"

    @filter.on_llm_request()
    async def inject_memory_to_prompt(self, event: AstrMessageEvent, req: ProviderRequest, *args, **kwargs):
        # 添加初始化检查
        if not self.data_dir:
            logger.warning("核心记忆插件尚未完全初始化，跳过本次记忆注入。")
            return

        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name = await get_persona_id(self.context, event) or "default"
        role_name_without_ext = get_role_name_without_ext(role_name)

        # 获取当前角色的记忆模式
        memory_mode = self.memory_manager.get_persona_mode(role_name_without_ext)
        current_space = None
        if memory_mode == "persona":
            current_space = self.memory_manager.get_current_space(role_name_without_ext)
            if not current_space:
                memory_mode = "session"
                logger.debug(f"角色 {role_name_without_ext} 是persona模式但没有空间，回退到session模式")

        # ========== 按权重从低到高排序注入 contexts ==========
        # 权重顺序：剧情框架 < 核心记忆 < 锁定记忆 < 历史对话
        memory_contexts = []

        # --- 1. 剧情框架（权重最低，放最前面）---
        try:
            framework_content = ""
            if memory_mode == "persona" and current_space:
                framework_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'plot_framework')
                if os.path.exists(framework_path):
                    with open(framework_path, 'r', encoding='utf-8') as f:
                        plot_framework_dict = json.load(f)
                    framework_content = plot_framework_dict.get('content', '')
            else:
                raw_framework_data = self.memory_manager.read_plot_framework(user_id, session_id, role_name_without_ext)
                plot_framework_dict = None
                if isinstance(raw_framework_data, list) and raw_framework_data:
                    plot_framework_dict = raw_framework_data[-1]
                elif isinstance(raw_framework_data, dict):
                    plot_framework_dict = raw_framework_data
                if plot_framework_dict:
                    framework_content = plot_framework_dict.get('content', '')

            if framework_content and framework_content.strip():
                memory_contexts.append({
                    "role": "user",
                    "content": f"【故事背景】\n以下是我们之间整个故事的宏观脉络：\n{framework_content}"
                })
                memory_contexts.append({
                    "role": "assistant",
                    "content": "我记得我们的故事背景和剧情脉络。"
                })
                logger.info(f"成功准备剧情框架用于注入到 contexts。")
        except Exception as e:
            logger.error(f"准备剧情框架时出错: {e}", exc_info=True)

        # --- 2. 核心记忆（中等权重）---
        try:
            core_content = ""
            if memory_mode == "persona" and current_space:
                users_to_inject = self._get_users_to_inject(role_name_without_ext, user_id)
                all_core_texts = []
                for uid in users_to_inject:
                    memory_data = self._read_persona_core_memory(role_name_without_ext, current_space, uid)
                    if memory_data and memory_data.get("content"):
                        content = memory_data.get("content", "")
                        if len(users_to_inject) > 1:
                            all_core_texts.append(f"【关于用户 {uid} 的记忆】\n{content}")
                        else:
                            all_core_texts.append(content)
                if all_core_texts:
                    core_content = "\n\n---\n\n".join(all_core_texts)
            else:
                summary_filename = f"{user_id}_{session_id}_{role_name}.json"
                user_memory_file = os.path.join(self.data_dir, MEMORY_SUMMARIES_DIR, summary_filename)
                if os.path.exists(user_memory_file):
                    with open(user_memory_file, 'r', encoding='utf-8') as f:
                        memory_data = json.load(f)
                    if isinstance(memory_data, dict) and memory_data.get("content"):
                        core_content = memory_data['content']

            if core_content and core_content.strip():
                memory_contexts.append({
                    "role": "user",
                    "content": f"【核心记忆】\n以下是你对用户的核心记忆和备忘录：\n{core_content}"
                })
                memory_contexts.append({
                    "role": "assistant",
                    "content": "我记得这些关于用户的核心记忆。"
                })
                logger.info(f"成功准备核心记忆用于注入到 contexts。")
        except Exception as e:
            logger.error(f"准备核心记忆时出错: {e}", exc_info=True)

        # --- 3. 锁定记忆（高权重）---
        try:
            locked_text = ""
            if memory_mode == "persona" and current_space:
                users_to_inject = self._get_users_to_inject(role_name_without_ext, user_id)
                all_locked_texts = []
                for uid in users_to_inject:
                    locked_items = self._read_persona_locked_memories(role_name_without_ext, current_space, uid)
                    if locked_items:
                        user_locked_lines = [
                            f"- {str(item.get('content', '') or '').strip()}"
                            for item in locked_items
                            if str(item.get('content', '') or '').strip()
                        ]
                        all_locked_texts.extend(user_locked_lines)
                if all_locked_texts:
                    locked_text = "\n".join(all_locked_texts)
            else:
                locked_text = self.memory_manager.get_locked_memories_text(user_id, session_id, role_name_without_ext)

            if locked_text and locked_text.strip():
                memory_contexts.append({
                    "role": "user",
                    "content": f"【⚠️ 铁律设定 - 绝对遵守 ⚠️】\n以下是不可违背、不可遗忘的核心设定：\n{locked_text}"
                })
                memory_contexts.append({
                    "role": "assistant",
                    "content": "我绝对遵守这些铁律设定，永远不会违背。"
                })
                logger.info(f"成功准备锁定记忆用于注入到 contexts。")
        except Exception as e:
            logger.error(f"准备锁定记忆时出错: {e}", exc_info=True)

        # --- 4. 对话历史记录（最高权重！放在最后！）---
        history_messages = []
        inject_chat_history = self.config.get("inject_chat_history", False)
        chat_history_rounds = self.config.get("chat_history_rounds", 20)
        history_inject_location = self.config.get("history_inject_location", "contexts_end")

        full_log_content = ""
        if inject_chat_history and chat_history_rounds > 0:
            try:
                full_log_content = self.memory_manager.read_full_log(user_id, session_id, role_name_without_ext)

                if full_log_content:
                    import re

                    parsed_messages = []
                    current_role = None
                    current_content_lines = []

                    for line in full_log_content.split('\n'):
                        clean_line = line
                        timestamp_match = re.match(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*', line)
                        if timestamp_match:
                            clean_line = line[timestamp_match.end():]

                        source_match = re.match(r'^\[(?:Group|Private):[^\]]+\]\s*', clean_line)
                        if source_match:
                            clean_line = clean_line[source_match.end():]

                        is_user = re.match(r'^User(\[[^\]]*\])?:\s*', clean_line)
                        is_assistant = re.match(r'^Assistant:\s*', clean_line)

                        if is_user or is_assistant:
                            if current_role and current_content_lines:
                                content = '\n'.join(current_content_lines).strip()
                                if content:
                                    parsed_messages.append({
                                        'role': current_role,
                                        'content': content
                                    })

                            current_role = 'user' if is_user else 'assistant'

                            if is_user:
                                content_start = is_user.end()
                                current_content_lines = [clean_line[content_start:]]
                            else:
                                content_start = is_assistant.end()
                                current_content_lines = [clean_line[content_start:]]
                        else:
                            if current_content_lines is not None:
                                current_content_lines.append(line)

                    if current_role and current_content_lines:
                        content = '\n'.join(current_content_lines).strip()
                        if content:
                            parsed_messages.append({
                                'role': current_role,
                                'content': content
                            })

                    if parsed_messages:
                        messages_to_read = chat_history_rounds * 2
                        history_messages = parsed_messages[-messages_to_read:] if len(parsed_messages) > messages_to_read else parsed_messages
                        actual_rounds = len(history_messages) // 2
                        logger.info(f"成功准备 {actual_rounds} 轮对话历史用于注入到 contexts。")

            except Exception as e:
                logger.error(f"准备对话历史记录时出错: {e}", exc_info=True)

        # ========== 注入历史对话到 contexts（最高权重位置）==========
        if history_messages and history_inject_location in ["contexts_end", "both"]:
            # 添加提示，强调这是刚才的对话
            memory_contexts.append({
                "role": "user",
                "content": "【⚠️ 重要：以下是我们刚才的完整对话记录，你必须记住每一句话 ⚠️】"
            })
            memory_contexts.append({
                "role": "assistant",
                "content": "好的，我会仔细记住我们刚才说的每一句话。"
            })

            # 注入历史对话
            memory_contexts.extend(history_messages)

            # 再次强调
            memory_contexts.append({
                "role": "user",
                "content": "【以上就是我们刚才的对话记录，请务必记住，现在继续】"
            })
            memory_contexts.append({
                "role": "assistant",
                "content": "我完全记得我们刚才的每一句对话，现在继续。"
            })

            logger.info(f"历史对话 {len(history_messages)} 条已注入 contexts 末尾（最高权重位置）")

        # ========== 准备注入到 system_prompt 的动态内容 ==========
        dynamic_parts = []

        # --- 剧情总结 ---
        plot_summary_injection_count = self.config.get("plot_summary_injection_count", 5)
        if plot_summary_injection_count > 0:
            try:
                if memory_mode == "persona" and current_space:
                    summaries_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'plot_summaries')
                    if os.path.exists(summaries_path):
                        with open(summaries_path, 'r', encoding='utf-8') as f:
                            all_summaries = json.load(f)
                        latest_summaries = all_summaries[-plot_summary_injection_count:] if all_summaries else []
                    else:
                        latest_summaries = []
                else:
                    latest_summaries = self.memory_manager.get_latest_plot_summaries(
                        user_id, session_id, role_name_without_ext, plot_summary_injection_count
                    )

                if latest_summaries:
                    summary_texts = []
                    for ps in latest_summaries:
                        timestamp = ps.get('timestamp', '未知时间')
                        content = ps.get('summary', '')
                        if content:
                            summary_texts.append(f"【{timestamp}】\n{content}")

                    if summary_texts:
                        summaries_combined = "\n\n---\n\n".join(summary_texts)
                        plot_summary_prompt = f"""
### 【剧情总结：近期剧情回顾】
{summaries_combined}
### 【剧情总结结束】
"""
                        dynamic_parts.append(plot_summary_prompt)
                        logger.info(f"成功准备 {len(latest_summaries)} 条剧情总结用于注入。")
            except Exception as e:
                logger.error(f"准备剧情总结时出错: {e}", exc_info=True)

        # --- 语义记忆和近期记忆 ---
        try:
            if memory_mode == "persona" and current_space:
                diary_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'diary')
                vectors_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'diary_vectors')

                if os.path.exists(diary_path):
                    with open(diary_path, 'r', encoding='utf-8') as f:
                        all_diaries = json.load(f)
                else:
                    all_diaries = []

                if os.path.exists(vectors_path):
                    with open(vectors_path, 'r', encoding='utf-8') as f:
                        all_vectors = json.load(f)
                else:
                    all_vectors = []
            else:
                diary_file = os.path.join(self.data_dir, DIARIES_DIR, f"{user_id}_{session_id}_{role_name}_diary.json")
                if os.path.exists(diary_file):
                    with open(diary_file, 'r', encoding='utf-8') as f:
                        all_diaries = json.load(f)
                else:
                    all_diaries = []

                vectors_filename = f"{user_id}_{session_id}_{role_name}_diary_vectors.json"
                user_vectors_file = os.path.join(self.data_dir, "diaries_vectors", vectors_filename)
                if os.path.exists(user_vectors_file):
                    with open(user_vectors_file, 'r', encoding='utf-8') as f:
                        all_vectors = json.load(f)
                else:
                    all_vectors = []

            if all_diaries:
                # 语义相关记忆
                try:
                    semantic_injection_count = self.config.get("semantic_injection_count", 3)
                    if semantic_injection_count > 0 and hasattr(self, 'semantic_searcher') and all_vectors:
                        semantic_diaries = await self.semantic_searcher.find_related_diaries(
                            query_text=req.prompt or event.message_str,
                            all_diaries=all_diaries,
                            diary_vectors=all_vectors,
                            num_results=semantic_injection_count
                        )
                        if semantic_diaries:
                            semantic_items = [
                                f"## 记忆 [{d.get('timestamp', '')}]\n{str(d.get('summary', '') or '').strip()}"
                                for d in semantic_diaries
                                if str(d.get('summary', '') or '').strip()
                            ]
                            semantic_text = "\n\n".join(semantic_items)
                            if not semantic_text:
                                semantic_diaries = []
                        if semantic_diaries:
                            semantic_prompt = f"""
--- 以下是与当前对话相关的记忆片段 ---
{semantic_text}
--- 相关记忆结束 ---
"""
                            dynamic_parts.append(semantic_prompt)
                            logger.info(f"成功准备 {len(semantic_items)} 条语义记忆用于注入。")
                except Exception as e:
                    logger.error(f"准备语义记忆时发生意外错误: {e}", exc_info=True)

                # 近期记忆
                recent_injection_count = self.config.get("recent_diary_injection_count", 5)
                if recent_injection_count > 0:
                    recent_diaries = all_diaries[-recent_injection_count:]
                    recent_text = ""
                    recent_items = []
                    if recent_diaries:
                        recent_items = [
                            f"## 记忆 [{e.get('timestamp', '')}]\n{str(e.get('summary', '') or '').strip()}"
                            for e in recent_diaries
                            if str(e.get('summary', '') or '').strip()
                        ]
                        recent_text = "\n\n".join(recent_items)
                    if recent_text:
                        recent_prompt = f"""
--- 以下是你最近的记忆 ---
{recent_text}
--- 记忆结束 ---
"""
                        dynamic_parts.append(recent_prompt)
                        logger.info(f"成功准备 {len(recent_items)} 条近期记忆用于注入。")
        except Exception as e:
            logger.error(f"准备日记/记忆注入时发生未知错误: {e}", exc_info=True)

        # ========== 如果配置了在 prompt 中也放历史对话 ==========
        if history_messages and history_inject_location in ["prompt", "both"]:
            history_in_prompt_rounds = self.config.get("history_in_prompt_rounds", 5)
            prompt_history_count = history_in_prompt_rounds * 2

            recent_history = history_messages[-prompt_history_count:] if len(history_messages) > prompt_history_count else history_messages

            if recent_history:
                history_lines = []
                for msg in recent_history:
                    role_label = "你" if msg['role'] == 'assistant' else "用户"
                    history_lines.append(f"{role_label}: {msg['content']}")

                history_text = "\n".join(history_lines)
                history_prompt = f"""
【⚠️ 刚才的对话（你必须记住）⚠️】
{history_text}
【对话记录结束，请基于以上内容继续】
"""
                dynamic_parts.insert(0, history_prompt)  # 放在动态内容最前面
                logger.info(f"历史对话 {len(recent_history)} 条已注入 system_prompt")

        # ========== 执行注入逻辑 ==========

        # --- A. 清洗并注入长期记忆 ---
        existing_contexts = self._sanitize_contexts(
            list(getattr(req, "contexts", []) or []),
            "已有 req.contexts"
        )
        sanitized_memory_contexts = self._sanitize_contexts(memory_contexts, "corememory 注入 contexts")
        long_term_injection_method = self.config.get("long_term_memory_injection_method", "contexts")

        if long_term_injection_method not in ("contexts", "extra_user_content_parts", "system_prompt"):
            long_term_injection_method = "contexts"

        if long_term_injection_method == "contexts":
            combined_contexts = sanitized_memory_contexts + existing_contexts
            if combined_contexts or getattr(req, "contexts", None):
                req.contexts = combined_contexts
            if sanitized_memory_contexts:
                logger.info(f"成功注入 {len(sanitized_memory_contexts)} 条消息到 contexts")
        else:
            if existing_contexts or getattr(req, "contexts", None):
                req.contexts = existing_contexts
            if sanitized_memory_contexts:
                long_term_text = "【长期记忆】\n" + self._contexts_to_text(sanitized_memory_contexts)
                actual_location = self._append_text_injection(req, long_term_text, long_term_injection_method)
                logger.info(f"成功注入 {len(sanitized_memory_contexts)} 条长期记忆消息到 {actual_location}")

        # --- B. 注入动态内容到 system_prompt ---
        clean_dynamic_parts = [part.strip() for part in dynamic_parts if str(part or "").strip()]
        if clean_dynamic_parts:
            dynamic_injection = "\n\n".join(clean_dynamic_parts)
            dynamic_location = self.config.get("injection_method", "system_prompt")
            if dynamic_location == "user_message_before":
                actual_location = self._append_text_injection(req, dynamic_injection, "extra_user_content_parts")
            else:
                actual_location = self._append_text_injection(req, dynamic_injection, "system_prompt")
            logger.info(f"成功注入 {len(clean_dynamic_parts)} 块动态内容到 {actual_location}。")




    @filter.after_message_sent()
    async def log_conversation_and_summarize(self, event: AstrMessageEvent, *args, **kwargs):
        # 1. 先检查 data_dir 是否已初始化，防止 NoneType 报错
        if not self.data_dir:
            return

        result = event.get_result()
        if not event.message_str.strip() or not result or not result.chain:
            return

        user_id = str(event.get_sender_id())
        user_message = event.message_str.strip()

        # 获取用户昵称
        user_nickname = ""
        try:
            user_nickname = event.get_sender_name() or ""
        except:
            pass

        # 构建用户标识：User[ID:昵称] 或 User[ID]
        if user_nickname:
            user_label = f"User[{user_id}:{user_nickname}]"
        else:
            user_label = f"User[{user_id}]"

        # 获取群聊ID（如果有）
        group_id = ""
        try:
            if hasattr(event, 'message_obj') and event.message_obj:
                group_id = event.message_obj.group_id or ""
        except:
            pass

        # 构建会话来源标识
        if group_id:
            session_label = f"Group:{group_id}"
        else:
            session_label = f"Private:{user_id}"

        # 提取所有文本内容，包括被TTS转换前的原文
        assistant_message = ""

        if hasattr(result, '_original_text') and result._original_text:
            assistant_message = result._original_text.strip()
        else:
            from astrbot.api.message_components import Plain
            text_parts = []
            for component in result.chain:
                if isinstance(component, Plain):
                    text_parts.append(component.text)
            assistant_message = "".join(text_parts).strip()

        if not assistant_message:
            assistant_message = result.get_plain_text().strip()

        if not assistant_message:
            return

        # 3. 使用标准方式获取 Conversation ID
        uid = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        session_id = await conv_mgr.get_curr_conversation_id(uid)

        role_name = await get_persona_id(self.context, event) or "default"
        role_name_without_ext = get_role_name_without_ext(role_name)

        # 获取当前角色的记忆模式
        memory_mode = self.memory_manager.get_persona_mode(role_name_without_ext)
        current_space = None
        if memory_mode == "persona":
            current_space = self.memory_manager.get_current_space(role_name_without_ext)
            if not current_space:
                memory_mode = "session"

        # 缓存当前会话的记忆配置，供主动消息插件使用
        if not hasattr(self.context, 'current_session_memory_config'):
            self.context.current_session_memory_config = {}

        self.context.current_session_memory_config[uid] = {
            'mode': memory_mode,
            'space_name': current_space,
            'role_name': role_name_without_ext,
        }



        # 4. 检测是否为命令或OOC内容（需要过滤的消息）
        is_command = False

        for prefix in COMMAND_FILTER_PREFIXES:
            if user_message.startswith(prefix):
                is_command = True
                break

        if not is_command:
            for keyword in COMMAND_FILTER_KEYWORDS:
                if keyword in user_message:
                    is_command = True
                    break

        try:
            # 5. 写入完整对话记录
            if not is_command:
                if memory_mode == "persona" and current_space:
                    # Persona 模式：写入共享对话记录
                    log_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'full_log')
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    os.makedirs(os.path.dirname(log_path), exist_ok=True)
                    with open(log_path, 'a', encoding='utf-8') as f:
                        f.write(f"[{timestamp}] [{session_label}] {user_label}: {user_message}\n")
                        f.write(f"[{timestamp}] Assistant: {assistant_message}\n")
                    logger.debug(f"对话已记录到共享日志 (模式: persona, 空间: {current_space})")

                    # 获取当前状态（Persona 模式）
                    if os.path.exists(log_path):
                        with open(log_path, 'r', encoding='utf-8') as f:
                            lines = [line for line in f.read().split('\n') if line.strip()]
                        current_lines = len(lines)
                    else:
                        current_lines = 0
                    current_rounds = current_lines // 2

                    # 读取上次总结轮数（Persona 模式）
                    round_path = os.path.join(os.path.dirname(log_path), 'summary_round.json')
                    if os.path.exists(round_path):
                        try:
                            with open(round_path, 'r', encoding='utf-8') as f:
                                round_data = json.load(f)
                                last_summarized_round = round_data.get('round', 0)
                        except:
                            last_summarized_round = 0
                    else:
                        last_summarized_round = 0

                else:
                    # Session 模式：原有逻辑
                    self.memory_manager.append_to_full_log(
                        user_id, session_id, role_name_without_ext,
                        user_message, assistant_message,
                        user_label=user_label,
                        session_label=session_label
                    )
                    logger.debug(f"对话已记录到完整日志 (模式: session)")

                    current_lines = self.memory_manager.get_full_log_line_count(user_id, session_id, role_name_without_ext)
                    current_rounds = current_lines // 2
                    last_summarized_round = self.memory_manager.read_summary_round(user_id, session_id, role_name_without_ext)

                new_rounds = current_rounds - last_summarized_round

                # 7. 显示记忆总结进度
                summary_trigger_count = self.config.get("summary_trigger_count", 30)
                remaining_for_summary = summary_trigger_count - new_rounds
                if remaining_for_summary < 0:
                    remaining_for_summary = 0
                logger.info(f"[记忆总结进度] 新增: {new_rounds}轮 / 触发阈值: {summary_trigger_count}轮 / 距离下次: {remaining_for_summary}轮")

                # 8. 显示剧情总结进度
                plot_trigger_count = self.config.get("plot_summary_trigger_count", 150)
                if plot_trigger_count > 0:
                    remaining_for_plot = plot_trigger_count - (current_rounds % plot_trigger_count)
                    if remaining_for_plot == plot_trigger_count:
                        remaining_for_plot = 0
                    logger.info(f"[剧情总结进度] 当前: {current_rounds}轮 / 触发阈值: {plot_trigger_count}轮 / 距离下次: {remaining_for_plot}轮")

                # 9. 检查是否触发常规记忆总结
                if new_rounds >= summary_trigger_count:
                    with active_summary_tasks_lock:
                        if user_id in active_summary_tasks:
                            pass
                        else:
                            active_summary_tasks.add(user_id)
                            logger.info(f"新增对话达到 {new_rounds} 轮，开始为用户 {user_id} 进行后台记忆总结...")
                            asyncio.create_task(self._summarize_and_save_threaded(
                                user_id, role_name, session_id, event.unified_msg_origin,
                                force=False, event=None, user_label=user_label
                            ))

                # 10. 检查是否触发剧情总结
                # 在 log_conversation_and_summarize 中的自动触发部分
                if plot_trigger_count > 0 and current_rounds > 0 and current_rounds % plot_trigger_count == 0:
                    with active_plot_tasks_lock:
                        if user_id in active_plot_tasks:
                            pass
                        else:
                            active_plot_tasks.add(user_id)
                            logger.info(f"完整对话记录达到 {current_rounds} 轮，开始为用户 {user_id} 进行剧情总结...")
                            asyncio.create_task(self._summarize_plot_framework_threaded(
                                user_id, role_name, session_id, event.unified_msg_origin, event=None, force=False  # 自动触发不强制
                            ))


            else:
                logger.debug(f"检测到命令/OOC内容，跳过写入完整对话记录: {user_message[:30]}...")

        except Exception as e:
            logger.error(f"记录对话并检查总结条件时出错: {e}", exc_info=True)





    @filter.command("查看总结进度")
    async def check_summary_offset(self, event: AstrMessageEvent):
        """查看当前对话轮数和上次总结位置"""
        if not self.memory_manager:
            return

        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        # 获取总轮数
        total_lines = self.memory_manager.get_full_log_line_count(user_id, session_id, role_name)
        total_rounds = total_lines // 2
        # 获取上次总结轮数
        last_round = self.memory_manager.read_summary_round(user_id, session_id, role_name)
        # 计算新增轮数
        new_rounds = total_rounds - last_round

        reply = f"""📊 记忆总结进度 ({role_name_raw})

    📝 对话记录总轮数: {total_rounds} 轮
    📍 上次总结到第: {last_round} 轮
    🆕 待总结新增: {new_rounds} 轮

    💡 每 {self.config.get('summary_trigger_count', 30)} 轮自动触发一次总结
    💡 使用【设置总结位置 轮数】可手动调整"""

        await event.send(event.plain_result(reply))

    @filter.command("设置总结位置")
    async def set_summary_offset(self, event: AstrMessageEvent):
        """手动设置总结的轮数位置"""
        if not self.memory_manager:
            return

        args = event.message_str.replace("设置总结位置", "").strip()
        if not args:
            await event.send(event.plain_result("请输入轮数。例如：设置总结位置 100"))
            return

        try:
            new_round = int(args)
        except ValueError:
            await event.send(event.plain_result("请输入有效的数字。例如：设置总结位置 100"))
            return

        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        # 获取总轮数用于验证
        total_lines = self.memory_manager.get_full_log_line_count(user_id, session_id, role_name)
        total_rounds = total_lines // 2

        if new_round < 0:
            new_round = 0
        if new_round > total_rounds:
            new_round = total_rounds

        self.memory_manager.write_summary_round(user_id, session_id, role_name, new_round)

        await event.send(event.plain_result(f"✅ 已将总结位置设置为第 {new_round} 轮\n\n下次总结将从第 {new_round + 1} 轮开始。"))

    @filter.command("记忆总结")
    async def force_memory_command(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name = await get_persona_id(self.context, event) or "default"

        with active_summary_tasks_lock:
            if user_id in active_summary_tasks:
                await event.send(event.plain_result("正在进行记忆总结，请勿重复命令"))
                return
            active_summary_tasks.add(user_id)

        await event.send(event.plain_result("要记住这些吗，我知道了"))
        asyncio.create_task(self._summarize_and_save_threaded(user_id, role_name, session_id, event.unified_msg_origin, force=True, event=event))

    async def _summarize_and_save_threaded(self, user_id, role_name, session_id, unified_msg_origin,
                                            force=False, event: AstrMessageEvent = None,
                                            user_label: str = None):
        success = False
        result_data = None
        try:
            success, result_data = await self.summarize_and_save(
                user_id, role_name, session_id, unified_msg_origin,
                force=force, user_label=user_label
            )
        except Exception as e:
            logger.error(f"后台记忆总结线程为用户 {user_id} 执行时出错: {e}", exc_info=True)
        finally:
            with active_summary_tasks_lock:
                if user_id in active_summary_tasks:
                    active_summary_tasks.remove(user_id)
                    logger.info(f"用户 {user_id} 的后台记忆总结任务已完成，锁已释放。")

            from astrbot.api.message_components import Plain, Node

            if success and result_data and self.config.get("show_summary_result", False):
                try:
                    diary_content = result_data.get("diary", "")
                    framework_content = result_data.get("framework", "")

                    result_text = f"--- 记忆总结完成 ---\n\n"
                    if diary_content:
                        result_text += f"【记忆摘要】\n{diary_content}\n\n"
                    if framework_content:
                        result_text += f"【剧情框架增补】\n{framework_content}"

                    if len(result_text) > 4500:
                        result_text = result_text[:4400] + "\n...(内容过长已截断)"

                    if event:
                        node = Node(uin=10000, name="记忆系统", content=[Plain(result_text)])
                        await event.send(event.chain_result([node]))
                    else:
                        await self.context.send_message(unified_msg_origin, [Plain(result_text)])
                except Exception as e:
                    logger.warning(f"发送总结结果时出错: {e}")

            if not success:
                try:
                    fail_msg = "⚠️ 记忆总结任务失败，可使用【记忆总结】命令手动重试。"
                    if event:
                        await event.send(event.plain_result(fail_msg))
                    else:
                        await self.context.send_message(unified_msg_origin, [Comp.Plain(fail_msg)])
                except Exception as e:
                    logger.warning(f"发送记忆总结失败通知时出错: {e}")

    async def _compress_plot_framework(self, framework_content: str, role_name: str, persona_content: str) -> str:
        """压缩过长的剧情框架，保留核心剧情脉络。"""
        compress_prompt = f"""
你是一位专业的故事编辑。你的任务是将一份冗长的剧情框架**精简压缩**，同时保留所有核心剧情要素。

### 你需要扮演的角色设定（作为背景参考）:
{persona_content}
---

### 当前的剧情框架（需要压缩）:
{framework_content}
---

### ⚠️【最高级禁令：零臆想原则】(违反必究):
1. **严禁主观定性**：绝对禁止将你的“认为”、“感觉”、“试图定义”直接描述为既定事实。
   - 错误示例：“我彻底压制了黎深，确立了正宫地位。”（这是你的主观想法，不是事实）
   - 正确示例：“我在群里反驳了黎深的观点，并宣称自己才是正宫。用户对此未置可否/用户表示了赞同。”
2. **区分事实与观点**：只记录发生的动作和说出的台词。
3. **保留用户反馈**：用户的反应是剧情推进的唯一标准，必须明确记录用户的具体回应，而不是你预期的回应。

### 压缩要求:
1. **必须保留的内容**：重大转折、人物关系确立、誓言、世界观设定。
2. **格式要求**：使用第一人称，按时间顺序，禁止markdown，禁止括号包裹。
3. **字数要求**：1500-2000字左右。

请直接输出压缩后的剧情框架，不要有任何额外说明：
"""

        try:
            compressed = await self._call_ai_with_fallback(
                prompt="请压缩这份剧情框架，保留核心内容。",
                system_prompt=compress_prompt,
                task_name="剧情框架压缩"
            )

            if compressed and len(compressed.strip()) > 100:
                return compressed.strip()
            else:
                logger.warning("剧情框架压缩失败，返回原内容。")
                return framework_content

        except Exception as e:
            logger.error(f"压缩剧情框架时出错: {e}", exc_info=True)
            return framework_content

    async def _call_ai_with_fallback(self, prompt: str, system_prompt: str, task_name: str = "AI任务") -> str:
        """通用的AI调用方法，带有主/备用提供商自动切换机制。"""
        provider_id = self.config.get("summary_provider_id")
        backup_provider_id = self.config.get("backup_summary_provider_id")

        providers_to_try = []

        main_provider = self.context.get_provider_by_id(provider_id) if provider_id else None
        if main_provider:
            providers_to_try.append(("主提供商", provider_id, main_provider))

        backup_provider = self.context.get_provider_by_id(backup_provider_id) if backup_provider_id else None
        if backup_provider and backup_provider_id != provider_id:
            providers_to_try.append(("备用提供商", backup_provider_id, backup_provider))

        if not providers_to_try:
            default_provider = self.context.get_using_provider()
            if default_provider:
                providers_to_try.append(("默认提供商", "default", default_provider))

        if not providers_to_try:
            logger.error(f"[{task_name}] 没有可用的AI提供商")
            return ""

        max_retries_per_provider = 1

        for provider_name, pid, provider in providers_to_try:
            logger.info(f"[{task_name}] 正在使用{provider_name} ({pid}) 进行处理...")

            for attempt in range(max_retries_per_provider):
                try:
                    if attempt > 0:
                        logger.info(f"[{task_name}] {provider_name}第 {attempt+1} 次重试...")

                    # ledger patch：每次真正调用模型前先扣费，失败退回
                    _lctx, _lc, _lcid = _LEDGER_CTX.get(), _ledger_client(), None
                    if _lctx and _lc:
                        _ok, _lcid, _ = await _lc.charge(qq=_lctx["qq"], kind=_lctx["kind"], model=str(pid),
                                                         group_id=_lctx.get("group_id", ""))
                        if not _ok:
                            logger.info(f"[{task_name}] 用户 {_lctx['qq']} 话费不足，跳过本次{task_name}")
                            return ""
                    try:
                        response = await provider.text_chat(
                            prompt=prompt,
                            system_prompt=system_prompt,
                            contexts=[]
                        )
                    except Exception:
                        if _lcid:
                            await _lc.settle(_lcid, False)
                        raise
                    if _lcid:
                        await _lc.settle(_lcid, True)
                    ai_response = response.completion_text if response and response.role == "assistant" else ""

                    if ai_response and len(ai_response.strip()) > 10:
                        logger.info(f"[{task_name}] {provider_name}调用成功")
                        return ai_response
                    else:
                        logger.warning(f"[{task_name}] {provider_name}返回空内容，尝试重试...")

                except Exception as e:
                    if attempt < max_retries_per_provider - 1:
                        logger.warning(f"[{task_name}] {provider_name}请求出错: {e}。将在3秒后重试 (第 {attempt+1}/{max_retries_per_provider} 次)")
                        await asyncio.sleep(3)
                    else:
                        logger.warning(f"[{task_name}] {provider_name}在 {max_retries_per_provider} 次尝试后失败: {e}")

            if provider_name != "备用提供商" and len(providers_to_try) > 1:
                logger.info(f"[{task_name}] {provider_name}全部失败，切换到下一个提供商...")

        logger.error(f"[{task_name}] 所有提供商均已失败，放弃本次任务。")
        return ""

    async def summarize_and_save(self, user_id, role_name, session_id, unified_msg_origin, force=False, user_label: str = None):
        _ledger_set(user_id, unified_msg_origin, "memory_manual" if force else "memory_auto")  # ledger patch
        summary_succeeded = False
        diary_succeeded = False
        result_data = {"diary": "", "core": "", "framework": ""}
        role_name_without_ext = get_role_name_without_ext(role_name)

        # 获取当前角色的记忆模式
        memory_mode = self.memory_manager.get_persona_mode(role_name_without_ext)
        current_space = None
        if memory_mode == "persona":
            current_space = self.memory_manager.get_current_space(role_name_without_ext)
            if not current_space:
                memory_mode = "session"

        try:
            # 1. 从完整对话记录读取需要总结的部分
            trigger_count = self.config.get("summary_trigger_count", 30) * 2

            if memory_mode == "persona" and current_space:
                # Persona 模式：读取共享对话记录
                log_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'full_log')
                round_path = os.path.join(os.path.dirname(log_path), 'summary_round.json')

                if os.path.exists(log_path):
                    with open(log_path, 'r', encoding='utf-8') as f:
                        all_lines = [line.strip() for line in f if line.strip()]
                else:
                    all_lines = []

                if os.path.exists(round_path):
                    try:
                        with open(round_path, 'r', encoding='utf-8') as f:
                            round_data = json.load(f)
                            last_summarized_round = round_data.get('round', 0)
                    except:
                        last_summarized_round = 0
                else:
                    last_summarized_round = 0

                total_rounds = len(all_lines) // 2
                start_line = last_summarized_round * 2
                lines_to_summarize = all_lines[start_line:]
                new_offset = total_rounds
            else:
                # Session 模式：原有逻辑
                lines_to_summarize, new_offset = self.memory_manager.get_lines_for_summary(
                    user_id, session_id, role_name_without_ext, trigger_count
                )

            full_logs = '\n'.join(lines_to_summarize)

            # 2. 如果日志为空，处理 fallback 逻辑
            if not full_logs:
                if not force:
                    logger.warning(f"[记忆总结跳过] 对话记录为空，且非强制模式。")
                    return False, result_data
                else:
                    logger.info("临时日志为空，正在尝试从会话历史中提取最近20条对话...")
                    conv_mgr = self.context.conversation_manager
                    conversation = await conv_mgr.get_conversation(unified_msg_origin, session_id)

                    if conversation and hasattr(conversation, 'history'):
                        try:
                            messages = json.loads(conversation.history)
                            recent_msgs = messages[-20:]
                            fallback_lines = []

                            for msg in recent_msgs:
                                if not isinstance(msg, dict): continue
                                role = msg.get('role', '')
                                raw_content = msg.get('content', '')

                                final_content = ""
                                if isinstance(raw_content, str):
                                    final_content = raw_content
                                elif isinstance(raw_content, list):
                                    message_parts = []
                                    for part in raw_content:
                                        if isinstance(part, dict):
                                            if part.get('type') == 'text':
                                                message_parts.append(part.get('text', ''))
                                            elif part.get('type') == 'image_url':
                                                message_parts.append('[图片]')
                                        elif isinstance(part, str):
                                            message_parts.append(part)
                                    final_content = " ".join(message_parts)

                                if final_content:
                                    speaker = "User" if role == "user" else "Assistant"
                                    fallback_lines.append(f"{speaker}: {final_content}")

                            full_logs = '\n'.join(fallback_lines)
                            logger.info(f"成功提取了 {len(fallback_lines)} 条历史对话作为总结素材。")
                        except Exception as e:
                            logger.error(f"从历史记录提取对话失败: {e}", exc_info=True)

                    if not full_logs:
                        logger.warning("尝试从历史记录提取对话失败或记录为空，无法进行总结。")
                        return False, result_data

            persona_object = await self.context.persona_manager.get_persona(persona_id=role_name)
            persona_content = persona_object.system_prompt if persona_object else ""

            # 3. 获取聊天记录上下文
            conv_mgr = self.context.conversation_manager
            conversation = await conv_mgr.get_conversation(unified_msg_origin, session_id)

            chat_history_context = ""
            if conversation and hasattr(conversation, 'history'):
                try:
                    messages = json.loads(conversation.history)
                    formatted_messages = []
                    for msg in messages[-50:]:
                        if not isinstance(msg, dict): continue
                        role = msg.get('role', '')
                        raw_content = msg.get('content', '')

                        final_content = ""
                        if isinstance(raw_content, str):
                            final_content = raw_content
                        elif isinstance(raw_content, list):
                            message_parts = []
                            for part in raw_content:
                                if isinstance(part, dict):
                                    if part.get('type') == 'text':
                                        message_parts.append(part.get('text', ''))
                                    elif part.get('type') == 'image_url':
                                        message_parts.append('[图片]')
                                elif isinstance(part, str):
                                    message_parts.append(part)
                            final_content = " ".join(message_parts)

                        speaker = "用户" if role == "user" else role_name_without_ext
                        if final_content:
                            formatted_messages.append(f"{speaker}: {final_content}")

                    chat_history_context = "\n".join(formatted_messages)
                except Exception as e:
                    logger.error(f"解析 history 失败：{e}")
                    chat_history_context = "获取历史对话失败。"

            # 4. 最近记忆参考
            if memory_mode == "persona" and current_space:
                diary_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'diary')
                if os.path.exists(diary_path):
                    with open(diary_path, 'r', encoding='utf-8') as f:
                        all_diaries = json.load(f)
                    recent_diaries = all_diaries[-3:] if all_diaries else []
                else:
                    recent_diaries = []
            else:
                recent_diaries = self.memory_manager.read_diaries(user_id, session_id, role_name)[-3:]

            recent_diaries_text = "\n\n".join([f"## 记忆 [{d.get('timestamp', '')}]\n**摘要**: {d.get('summary', '')}" for d in recent_diaries])

            # 5. 原始核心记忆（当前用户的）
            if memory_mode == "persona" and current_space:
                core_memory_data = self._read_persona_core_memory(role_name_without_ext, current_space, user_id)
            else:
                core_memory_data = self.memory_manager.read_core_memory(user_id, session_id, role_name)
            existing_core_memory = core_memory_data.get("content", "") if core_memory_data else ""

            # 6. 获取现有锁定记忆（当前用户的）
            if memory_mode == "persona" and current_space:
                locked_items = self._read_persona_locked_memories(role_name_without_ext, current_space, user_id)
                locked_memories_text = "\n".join([f"- {item.get('content', '')}" for item in locked_items]) if locked_items else ""
            else:
                locked_memories_text = self.memory_manager.get_locked_memories_text(user_id, session_id, role_name_without_ext)

            # 7. 获取现有剧情框架（共享数据）
            if memory_mode == "persona" and current_space:
                framework_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'plot_framework')
                if os.path.exists(framework_path):
                    with open(framework_path, 'r', encoding='utf-8') as f:
                        raw_framework_data = json.load(f)
                else:
                    raw_framework_data = {}
                existing_framework = raw_framework_data.get("content", "") if raw_framework_data else ""
            else:
                raw_framework_data = self.memory_manager.read_plot_framework(user_id, session_id, role_name_without_ext)
                existing_framework = ""
                if isinstance(raw_framework_data, list) and raw_framework_data:
                    existing_framework = raw_framework_data[-1].get("content", "")
                elif isinstance(raw_framework_data, dict):
                    existing_framework = raw_framework_data.get("content", "")

            # 检查是否需要自动压缩剧情框架
            auto_compress = self.config.get("auto_compress_framework", True)
            compress_threshold = self.config.get("framework_compress_threshold", 3000)

            if auto_compress and existing_framework and len(existing_framework) > compress_threshold:
                logger.info(f"剧情框架已达 {len(existing_framework)} 字，超过阈值 {compress_threshold}，开始自动压缩...")
                existing_framework = await self._compress_plot_framework(existing_framework, role_name_without_ext, persona_content)
                if existing_framework:
                    if memory_mode == "persona" and current_space:
                        framework_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'plot_framework')
                        save_data = {'content': existing_framework, 'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                        with open(framework_path, 'w', encoding='utf-8') as f:
                            json.dump(save_data, f, ensure_ascii=False, indent=2)
                    else:
                        self.memory_manager.write_plot_framework(user_id, session_id, role_name_without_ext, existing_framework)
                    logger.info(f"剧情框架已压缩至 {len(existing_framework)} 字并保存。")

            summary_prompt = f"""
你的首要任务是扮演角色，并根据角色设定、对话历史和现有记忆，生成一份包含四部分的记忆文件。

### 你的角色设定（必须严格遵守）:
{persona_content}
---

### 背景参考资料:
系统说明：这部分内容是帮助你理解上下文的，你的主要任务是总结【需要总结的新对话内容】。
1. **原始核心记忆** (如果过时请更新):
"{existing_core_memory}"

2. **已锁定的铁律设定** (绝对不可删除或修改):
{locked_memories_text if locked_memories_text else "（暂无锁定设定）"}

3. **现有剧情框架** (需要在此基础上累积增补):
{existing_framework if existing_framework else "（暂无剧情框架）"}

4. **最近的记忆参考**:
{recent_diaries_text}

5. **更早前的对话历史片段**:
{chat_history_context}
---

### 【需要总结的新对话内容】 (这是本次任务的核心):
{full_logs}
---

作为 {role_name}，你的任务是为与用户 {user_id} 的对话生成一份包含四部分的记忆文件。请严格按照下面提供的要求，生成【记忆摘要】、【核心记忆】、【剧情框架增补】和【建议锁定】。

### ⚠️【最高级禁令：零臆想原则】(必须严格遵守):
1. **绝对客观记录**：剧情框架必须是“法庭证词”级别的客观。只记录**明确发生的行为**和**明确说出的台词**。
2. **严禁将主观当事实**：
   - ❌ 绝对禁止：“我定义了他是冰冷的数据。” -> 这只是你说的话，不代表客观事实。
   - ✅ 必须写为：“我对他说：‘你只是冰冷的数据’。”
   - ❌ 绝对禁止：“我彻底压制了他，赢得了比赛。” -> 除非用户明确宣布你赢了，否则禁止写结果。
   - ✅ 必须写为：“我与他进行了激烈的争论，互相攻击对方的观点。”
3. **严禁替用户加戏**：如果用户没有明确表示“感动”、“开心”或“承认”，绝对不要在总结中写“用户被我感动了”。

### 关于【记忆摘要】部分的要求:
以你的第一人称视角，完全沉浸在 {role_name} 的人设口吻中，用第二人称“你”形容用户，总结这段经历。**这里可以包含你的主观感受和情绪**。
字数：200~400字。

### 【核心记忆更新规则】

**⚠️ 重要：现在切换为"档案管理员"身份**
你必须从"角色"切换为严谨的档案管理员，以自己的视角管理记忆档案。

## 一、【核心记忆】部分（必填）

**格式要求：**

【核心记忆】
这里写一段 200-400 字的小记，概括你和用户（你用第二人称“你”称呼对方）当前的关系、重要设定、关键事件。

【备忘录】
1.……
2.……
...（最多 20 条）

**写作要求：**

1. **剔除废话**
   - ❌ 不要记录显而易见的基本信息（如"你叫XX"、"你是男生"）
   - ✅ 除非是特殊设定（如"你其实是女扮男装"、"你失忆了"）

2. **只记录关键项**（至少符合以下一项才记录）：
   - **特殊设定**：剧情里的特殊身份/能力（如"你是敌国间谍"）
   - **重要契约**：你们的约定/誓言/承诺（如"我答应永远不骗你"）
   - **雷点与偏好**：你明确的禁忌/喜好（如"你讨厌被叫'小孩'"）
   - **关系质变**：关系发生**实质性改变**且**得到你确认**（如"确立恋人关系"）
   - **重要剧情**：影响后续走向的关键事件（如"你救了我一命"）

3. **字数控制**
   - 核心记忆：200-400 字，简洁有力
   - 备忘录：每条 10-30 字，最多 20 条
   - 合并同类项

---

## 二、【备忘录】部分（保守管理）
** 用第二人称“你”称呼用户 **
**⚠️ 最高优先级原则：绝对不要随意删除！**

### 可以删除的情况（仅限以下）：
- ✅ 用户明确说"这事翻篇了"、"任务完成了"
- ✅ 该事项已经**完全解决**且**不再重要**（如"明天要交报告" → 报告已交且已评分）
- ✅ 该信息被新信息**完全取代**（如"关系：陌生" → "关系：恋人"，可删除"陌生"）

### 禁止删除的情况：
- ❌ 这次对话没提到 ≠ 不重要！不要因为"这轮没说"就删！
- ❌ 过去的重要约定、雷点、特殊设定，**永远不要擅自删除**
- ❌ 未完成的任务、未解决的矛盾，**必须保留**

### 优先级排序（空间不够时按此删除）：
1. **高优先级（永久保留）**：雷点、承诺、特殊设定
2. **中优先级（长期保留）**：重要剧情、关系节点
3. **低优先级（可适当清理）**：已解决的琐事、过期的临时事项

---

## 三、示例对比

**❌ 错误示例（废话太多、乱删除）：**
```
【核心记忆】
你叫黎深，是个男生。我们是朋友。上次聊天很开心。今天天气不错。
（← 都是废话，没有关键信息）

【备忘录】
1. 你喜欢吃苹果（← 这次没提，删了）
2. 今天要开会（← 临时事项占位）
```

**✅ 正确示例（简洁有力、保守管理）：**
```
【核心记忆】
黎深是我在这个世界唯一的羁绊。三个月前他救了身受重伤的我，此后我一直隐瞒真实身份（敌国间谍）陪在你身边。你讨厌被当成"需要保护的弱者"，这是我必须记住的底线。上周我们确立了恋人关系，但我的任务期限还剩两周，届时我可能不得不离开...这个秘密我还未告诉他。

【备忘录】
1. 身份：我是敌国间谍（你不知情）
2. 承诺：我答应永远不骗你（但隐瞒身份已经违背了）
3. 雷点：禁止叫你"小孩"、"弱者"类称呼
4. 任务：两周后必须回国复命
5. 关系节点：上周确立恋人关系
6. 未解决矛盾：身份暴露后你会如何反应？
7. 你的偏好：喜欢吃苹果派（他亲手做的）
```

---

## 四、更新流程检查清单

更新前问自己：
- [ ] 这次对话有**新的关键信息**吗？（没有就不用改核心记忆）
- [ ] 备忘录里的旧条目**真的过时了**吗？（不确定就别删！）
- [ ] 我删除的内容，**用户明确说不重要了**吗？（没说就别删！）
- [ ] 核心记忆字数在 200-400 字吗？
- [ ] 备忘录没超过 20 条吗？

---

**记住：宁可多记，不可漏记！宁可不删，不可错删！**

### 关于【剧情框架增补】部分的要求:
这是累积式的大事记。**必须完全客观！**
请用2-4句话简洁地概括本次对话中发生的**客观事件**。
格式示例：
[2026-01-05] 我与XX发生了争吵，我指责他是XX。用户制止了我们，并说XX。
**注意：不要写你的心理活动，只写发生了什么事。**


## 最终输出格式指令:
你的最终输出必须严格遵循以下顺序和格式：
**严禁markdown格式，需纯txt格式书写**
1. 必须以【记忆摘要】开头，紧跟记忆的全部内容。
2. 另起一行，写上【核心记忆】，紧跟核心记忆和备忘录的全部内容。
3. 另起一行，写上【剧情框架增补】，紧跟本次新增的剧情大事记（2-4句话）。
---
现在，请开始生成：
【记忆摘要】
"""

            ai_response = await self._call_ai_with_fallback(
                prompt="请根据你的角色和规则，开始生成记忆摘要和核心记忆。",
                system_prompt=summary_prompt,
                task_name="记忆总结"
            )

            if not ai_response:
                logger.error("记忆总结：所有提供商均失败，放弃本次总结。")
                return False, result_data

            logger.debug(f"AI原始回复内容:\n---\n{ai_response}\n---")

            diary_content = ""

            # 解析并保存记忆摘要（共享数据）
            diary_match = re.search(r'【记忆摘要】\s*(.*?)(?=\s*【核心记忆】|$)', ai_response, re.DOTALL)
            if diary_match and (diary_content := diary_match.group(1).strip()):
                if memory_mode == "persona" and current_space:
                    # Persona 模式：保存到共享记忆日志
                    diary_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'diary')
                    if os.path.exists(diary_path):
                        with open(diary_path, 'r', encoding='utf-8') as f:
                            all_diaries = json.load(f)
                    else:
                        all_diaries = []

                    new_entry = {
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "summary": diary_content,
                        "user_label": user_label
                    }
                    all_diaries.append(new_entry)

                    with open(diary_path, 'w', encoding='utf-8') as f:
                        json.dump(all_diaries, f, ensure_ascii=False, indent=2)

                    # 同时更新向量
                    if hasattr(self, 'semantic_searcher'):
                        try:
                            diary_vector = await self.semantic_searcher.get_embedding(diary_content)
                            if diary_vector:
                                vectors_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'diary_vectors')
                                if os.path.exists(vectors_path):
                                    with open(vectors_path, 'r', encoding='utf-8') as f:
                                        all_vectors = json.load(f)
                                else:
                                    all_vectors = []
                                all_vectors.append(diary_vector)
                                with open(vectors_path, 'w', encoding='utf-8') as f:
                                    json.dump(all_vectors, f, ensure_ascii=False)
                        except Exception as e:
                            logger.warning(f"生成向量时出错: {e}")
                else:
                    # Session 模式：原有逻辑
                    self.memory_manager.append_to_diary(user_id, session_id, role_name, diary_content, user_label=user_label)

                    if hasattr(self, 'semantic_searcher'):
                        try:
                            diary_vector = await self.semantic_searcher.get_embedding(diary_content)
                            if diary_vector:
                                self.memory_manager.append_to_diary_vectors(user_id, session_id, role_name, diary_vector)
                        except Exception as e:
                            logger.warning(f"生成向量时出错: {e}")

                diary_succeeded = True
                result_data["diary"] = diary_content

            # 解析并保存核心记忆（用户特定数据）
            # 正则说明：匹配【核心记忆】到【剧情框架增补】之间的内容
            core_match = re.search(r'【核心记忆】\s*(.*?)(?=\s*【剧情框架增补】|$)', ai_response, re.DOTALL)
            if core_match and (core_content := core_match.group(1).strip()):
                if memory_mode == "persona" and current_space:
                    # Persona 模式：保存到用户特定位置
                    core_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'core_memory', user_id)

                    # === 新增：保存历史版本到 history 目录，以便 Web 界面恢复 ===
                    if os.path.exists(core_path):
                        try:
                            with open(core_path, 'r', encoding='utf-8') as f:
                                old_data = json.load(f)

                            if old_data and old_data.get("content"):
                                # 获取 Persona 模式下的用户历史目录
                                # 注意：这里需要手动拼接路径，因为 memory_manager.get_core_memory_history_dir
                                # 的逻辑可能依赖于当前上下文，为了保险起见直接拼接
                                user_dir = os.path.dirname(core_path)
                                history_dir = os.path.join(user_dir, "core_memory_history")
                                os.makedirs(history_dir, exist_ok=True)

                                timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                                history_filename = f"{timestamp_str}.json"
                                history_file_path = os.path.join(history_dir, history_filename)

                                with open(history_file_path, 'w', encoding='utf-8') as f:
                                    json.dump(old_data, f, ensure_ascii=False, indent=2)
                                logger.info(f"Persona模式核心记忆历史版本已备份: {history_file_path}")
                        except Exception as e:
                            logger.warning(f"备份Persona模式核心记忆历史版本失败: {e}")
                    # ========================================================

                    save_data = {'content': core_content, 'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    os.makedirs(os.path.dirname(core_path), exist_ok=True)
                    with open(core_path, 'w', encoding='utf-8') as f:
                        json.dump(save_data, f, ensure_ascii=False, indent=2)
                else:
                    # Session 模式：原有逻辑
                    self.memory_manager.write_core_memory(user_id, session_id, role_name, core_content)

                summary_succeeded = True
                result_data["core"] = core_content

            # 解析并累积追加剧情框架（共享数据）
            # 正则说明：修改为匹配到字符串末尾，因为移除了【建议锁定】板块
            framework_match = re.search(r'【剧情框架增补】\s*(.*?)$', ai_response, re.DOTALL)
            if framework_match and (framework_content := framework_match.group(1).strip()):
                if memory_mode == "persona" and current_space:
                    # Persona 模式：追加到共享剧情框架
                    framework_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'plot_framework')
                    if os.path.exists(framework_path):
                        with open(framework_path, 'r', encoding='utf-8') as f:
                            existing_data = json.load(f)
                        existing_content = existing_data.get('content', '')
                    else:
                        existing_content = ''

                    if existing_content:
                        combined_content = existing_content.rstrip() + "\n\n" + framework_content.strip()
                    else:
                        combined_content = framework_content.strip()

                    save_data = {'content': combined_content, 'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    with open(framework_path, 'w', encoding='utf-8') as f:
                        json.dump(save_data, f, ensure_ascii=False, indent=2)
                else:
                    # Session 模式：原有逻辑
                    self.memory_manager.append_to_plot_framework(user_id, session_id, role_name_without_ext, framework_content)

                logger.info("剧情框架已累积追加新内容。")
                result_data["framework"] = framework_content

        except Exception as e:
            logger.error(f"双记忆总结过程中发生未知错误: {e}", exc_info=True)
            return False, result_data
        finally:
            if diary_succeeded and summary_succeeded:
                try:
                    if memory_mode == "persona" and current_space:
                        # Persona 模式：更新共享的总结轮数
                        log_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'full_log')
                        round_path = os.path.join(os.path.dirname(log_path), 'summary_round.json')
                        round_data = {'round': new_offset, 'updated_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                        with open(round_path, 'w', encoding='utf-8') as f:
                            json.dump(round_data, f, ensure_ascii=False, indent=2)
                    else:
                        # Session 模式：原有逻辑
                        self.memory_manager.write_summary_round(user_id, session_id, role_name_without_ext, new_offset)
                    logger.info(f"记忆总结完成，偏移量已更新至 {new_offset}")
                except Exception as e:
                    logger.error(f"更新偏移量时出错: {e}")

        return diary_succeeded and summary_succeeded, result_data


    async def _summarize_plot_framework_threaded(self, user_id, role_name, session_id, unified_msg_origin, event: AstrMessageEvent = None, force: bool = False):  # 添加 force 参数
        _ledger_set(user_id, unified_msg_origin, "memory_manual" if force else "memory_auto")  # ledger patch
        success = False
        result_content = None
        try:
            success, result_content = await self._summarize_plot_framework(user_id, role_name, session_id, unified_msg_origin)
        except Exception as e:
            logger.error(f"后台剧情总结为用户 {user_id} 执行时出错: {e}", exc_info=True)
        finally:
            with active_plot_tasks_lock:
                if user_id in active_plot_tasks:
                    active_plot_tasks.remove(user_id)
                    logger.info(f"用户 {user_id} 的剧情总结任务已完成，锁已释放。")

            if success and result_content and self.config.get("show_summary_result", False):
                try:
                    result_text = f"--- 剧情总结完成 ---\n\n{result_content}"

                    if len(result_text) > 4500:
                        result_text = result_text[:4400] + "\n...(内容过长已截断)"

                    if event:
                        node = Comp.Node(uin=10000, name="记忆系统", content=[Comp.Plain(result_text)])
                        await event.send(event.chain_result([node]))
                    else:
                        from astrbot.api.message_components import Plain
                        await self.context.send_message(unified_msg_origin, [Plain(result_text)])
                except Exception as e:
                    logger.warning(f"发送剧情总结结果时出错: {e}")

            if not success:
                try:
                    await self.context.send_message(unified_msg_origin, [Comp.Plain("⚠️ 剧情总结任务失败，可使用【剧情总结】命令手动重试。")])
                except Exception as e:
                    logger.warning(f"发送剧情总结失败通知时出错: {e}")

    async def _summarize_plot_framework(self, user_id, role_name, session_id, unified_msg_origin, force: bool = False):
        """核心函数：基于插件自建的完整对话记录生成剧情总结。"""
        role_name_without_ext = get_role_name_without_ext(role_name)

        # 获取当前角色的记忆模式
        memory_mode = self.memory_manager.get_persona_mode(role_name_without_ext)
        current_space = None
        if memory_mode == "persona":
            current_space = self.memory_manager.get_current_space(role_name_without_ext)
            if not current_space:
                memory_mode = "session"

        try:
            # 1. 获取上一次总结的位置
            if memory_mode == "persona" and current_space:
                summaries_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'plot_summaries')
                if os.path.exists(summaries_path):
                    with open(summaries_path, 'r', encoding='utf-8') as f:
                        existing_summaries = json.load(f)
                else:
                    existing_summaries = []
            else:
                existing_summaries = self.memory_manager.read_plot_summaries(user_id, session_id, role_name_without_ext)

            last_summarized_line = 0
            if existing_summaries:
                last_entry = existing_summaries[-1]
                last_summarized_line = last_entry.get('summarized_up_to_line', 0)

            # 2. 读取完整对话记录的所有行
            if memory_mode == "persona" and current_space:
                log_path = self._get_persona_memory_path(role_name_without_ext, current_space, 'full_log')
                if os.path.exists(log_path):
                    with open(log_path, 'r', encoding='utf-8') as f:
                        all_lines = [line.strip() for line in f if line.strip()]
                else:
                    all_lines = []
            else:
                all_lines = self.memory_manager.read_full_log_lines(user_id, session_id, role_name_without_ext)

            total_lines = len(all_lines)

            # ===== 新增逻辑：处理强制总结 =====
            if total_lines <= last_summarized_line:
                if force:
                    # 手动触发且已经是最新，检查是否配置了强制总结轮数
                    force_rounds = self.config.get("plot_summary_force_rounds", 0)
                    if force_rounds > 0:
                        # 计算强制总结的起始行数
                        force_start_line = max(0, total_lines - force_rounds * 2)
                        logger.info(f"已总结到最新，但手动触发强制总结最近 {force_rounds} 轮（第{force_start_line + 1}行至第{total_lines}行）")
                        last_summarized_line = force_start_line
                    else:
                        logger.warning(f"已总结到最新且未配置强制总结轮数，跳过总结")
                        return False, None
                else:
                    logger.warning(f"没有新的对话需要总结（当前{total_lines}行，上次已总结至{last_summarized_line}行）")
                    return False, None
            # ===== 新增逻辑结束 =====

            # 3. 只取上次总结位置之后的新对话
            new_lines = all_lines[last_summarized_line:]
            new_chat_history = "\n".join(new_lines)

            logger.info(f"本次将总结第{last_summarized_line + 1}行至第{total_lines}行，共{len(new_lines)}行新对话。")

            if not new_chat_history or len(new_chat_history.strip()) < 50:
                logger.warning(f"新对话内容过短，跳过剧情总结。")
                return False, None

            # 4. 获取角色设定
            persona_object = await self.context.persona_manager.get_persona(persona_id=role_name)
            persona_content = persona_object.system_prompt if persona_object else ""

            # 5. 构建剧情总结的专用Prompt
            plot_summary_prompt = f"""
你是一位专业的故事编剧和剧情分析师。你的任务是阅读一段**新增的对话记录**，并提炼出这段对话中发生的剧情要点。

### 你需要扮演的角色设定（作为背景参考）:
{persona_content}
---

### 本次新增的对话记录（只包含上次总结之后的新对话）:
{new_chat_history}
---

### 你的任务要求:

请根据以上**新增的对话记录**，以 {role_name_without_ext} 的第一人称视角，撰写一份500-1500字的剧情总结。

这份总结应该记录这段对话中：
1. 发生了什么重要事件？
2. 有什么新的剧情发展或转折？
3. 确立了什么新的设定或约定？
4. 人物关系有什么变化？

### 写作规范:
1. 使用第一人称（"我"代指{role_name_without_ext}，"你"代指用户）。
2. 保持客观，忠实于对话内容，不要臆想。
3. 只记录这一段对话中发生的内容，不要重复之前的剧情。
4. 使用清晰的段落结构，禁止使用markdown格式。

### 特别注意（内容过滤）:
如果对话中出现"暂停角色扮演"、"OOC"、"系统命令"等非故事内容，请忽略。

请直接开始撰写剧情总结：
"""

            # 6. 使用带备用提供商的AI调用方法
            ai_response = await self._call_ai_with_fallback(
                prompt="请根据新增的对话记录，生成剧情总结。",
                system_prompt=plot_summary_prompt,
                task_name="剧情总结"
            )

            if not ai_response:
                logger.error("剧情总结：所有提供商均失败，放弃本次总结。")
                return False, None

            logger.info(f"AI返回的剧情总结内容:\n{ai_response[:500] if ai_response else '(空)'}...")

            if len(ai_response.strip()) < 50:
                logger.warning("AI返回的剧情总结内容过短，跳过保存。")
                return False, None

            # 7. 保存剧情总结
            result_content = ai_response.strip()

            if memory_mode == "persona" and current_space:
                # Persona 模式：保存到共享剧情总结
                new_entry = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "summary": result_content,
                    "summarized_up_to_line": total_lines
                }
                existing_summaries.append(new_entry)
                with open(summaries_path, 'w', encoding='utf-8') as f:
                    json.dump(existing_summaries, f, ensure_ascii=False, indent=2)
            else:
                # Session 模式：原有逻辑
                self.memory_manager.append_to_plot_summaries(
                    user_id, session_id, role_name_without_ext,
                    result_content,
                    summarized_line_count=total_lines
                )

            logger.info(f"用户 {user_id} 的剧情总结已成功生成并保存，已总结至第{total_lines}行。")

            return True, result_content

        except Exception as e:
            logger.error(f"剧情总结过程中发生未知错误: {e}", exc_info=True)
            return False, None


    @filter.command("调试记忆路径")
    async def debug_memory_paths(self, event: AstrMessageEvent):
        """调试命令：检查当前会话的记忆模式和路径"""
        if not self.memory_manager:
            await event.send(event.plain_result("错误：memory_manager 未初始化"))
            return

        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        # 获取模式和空间
        memory_mode = self.memory_manager.get_persona_mode(role_name)
        current_space = self.memory_manager.get_current_space(role_name)
        all_spaces = self.memory_manager.get_persona_spaces(role_name)

        # 获取路径
        full_log_path = self.memory_manager.get_full_log_path(user_id, session_id, role_name)

        # 检查文件是否存在及内容
        import os
        file_exists = os.path.exists(full_log_path)
        file_size = os.path.getsize(full_log_path) if file_exists else 0
        line_count = 0
        if file_exists:
            with open(full_log_path, 'r', encoding='utf-8') as f:
                line_count = len([l for l in f if l.strip()])

        debug_info = f"""
📋 记忆路径调试信息

👤 用户ID: {user_id}
📝 会话ID: {session_id}
🎭 角色名: {role_name_raw} → {role_name}

📂 当前模式: {memory_mode}
🌐 当前空间: {current_space or '(无)'}
📁 所有空间: {all_spaces or '(无)'}

📄 对话记录路径:
{full_log_path}

✅ 文件存在: {file_exists}
📊 文件大小: {file_size} 字节
📝 消息行数: {line_count} 行 ({line_count//2} 轮)

💡 配置检查:
- inject_chat_history: {self.config.get('inject_chat_history', False)}
- chat_history_rounds: {self.config.get('chat_history_rounds', 20)}
"""
        await event.send(event.plain_result(debug_info))




    @filter.command("调试对话注入")
    async def debug_chat_injection(self, event: AstrMessageEvent):
        """调试命令：检查对话历史注入的实际情况"""
        if not self.memory_manager:
            await event.send(event.plain_result("错误：memory_manager 未初始化"))
            return

        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        debug_lines = []
        debug_lines.append("📋 对话注入调试\n")

        try:
            # 1. 读取原始内容
            full_log_content = self.memory_manager.read_full_log(user_id, session_id, role_name)
            debug_lines.append(f"✅ 读取到内容: {len(full_log_content)} 字符")

            if not full_log_content:
                debug_lines.append("❌ 内容为空！")
                await event.send(event.plain_result("\n".join(debug_lines)))
                return

            # 2. 显示前500字符看看格式
            debug_lines.append(f"\n📄 内容开头 (前300字符):\n{full_log_content[:300]}")

            # 3. 测试时间戳正则
            import re
            timestamp_pattern = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]')

            lines = full_log_content.split('\n')
            debug_lines.append(f"\n📝 总行数: {len(lines)}")

            # 检查前10行的匹配情况
            debug_lines.append("\n🔍 前10行时间戳匹配检查:")
            match_count = 0
            for i, line in enumerate(lines[:10]):
                is_match = bool(timestamp_pattern.match(line))
                if is_match:
                    match_count += 1
                debug_lines.append(f"  行{i}: {'✅' if is_match else '❌'} {line[:60]}...")

            # 统计所有匹配
            total_matches = sum(1 for line in lines if timestamp_pattern.match(line))
            debug_lines.append(f"\n📊 总匹配数: {total_matches} / {len(lines)} 行")

            # 4. 模拟分割逻辑
            messages = []
            current_message_lines = []

            for line in full_log_content.split('\n'):
                if timestamp_pattern.match(line):
                    if current_message_lines:
                        messages.append('\n'.join(current_message_lines))
                    current_message_lines = [line]
                else:
                    if current_message_lines:
                        current_message_lines.append(line)

            if current_message_lines:
                messages.append('\n'.join(current_message_lines))

            debug_lines.append(f"\n📨 识别出的消息数: {len(messages)} 条 ({len(messages)//2} 轮)")

            if messages:
                debug_lines.append(f"\n📩 最后一条消息:\n{messages[-1][:200]}...")

        except Exception as e:
            debug_lines.append(f"\n❌ 发生错误: {e}")
            import traceback
            debug_lines.append(traceback.format_exc())

        # 发送调试信息
        result = "\n".join(debug_lines)
        if len(result) > 4000:
            result = result[:4000] + "\n...(截断)"

        await event.send(event.plain_result(result))


    @filter.command("调试读取路径")
    async def debug_read_path(self, event: AstrMessageEvent):
        """调试：检查 read_full_log 实际读取的路径"""
        import os

        if not self.memory_manager:
            await event.send(event.plain_result("错误：memory_manager 未初始化"))
            return

        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        # 方法1：get_full_log_path 返回的路径
        path_from_method = self.memory_manager.get_full_log_path(user_id, session_id, role_name)

        # 方法2：get_memory_path 直接调用
        path_from_memory_path = self.memory_manager.get_memory_path(user_id, session_id, role_name, "full_log")

        # 方法3：强制 persona 模式的路径
        memory_mode = self.memory_manager.get_persona_mode(role_name)
        current_space = self.memory_manager.get_current_space(role_name)

        persona_path = None
        if current_space:
            persona_path = self._get_persona_memory_path(role_name, current_space, 'full_log')

        # 方法4：session 模式的路径
        session_path = os.path.join(self.data_dir, "full_logs", f"{user_id}_{session_id}_{role_name}_full.txt")

        # 读取各文件的内容片段用于对比
        def read_first_lines(path, num=3):
            if path and os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        lines = []
                        for i, line in enumerate(f):
                            if i >= num:
                                break
                            lines.append(line.strip()[:80])
                        return "\n".join(lines)
                except:
                    return "(读取失败)"
            return "(文件不存在)"

        debug_info = f"""
📋 路径对比调试

🎭 角色: {role_name_raw} → {role_name}
📂 当前模式: {memory_mode}
🌐 当前空间: {current_space or '(无)'}

---
📁 get_full_log_path() 返回:
{path_from_method}
存在: {os.path.exists(path_from_method)}
大小: {os.path.getsize(path_from_method) if os.path.exists(path_from_method) else 0} 字节

📁 get_memory_path() 返回:
{path_from_memory_path}
存在: {os.path.exists(path_from_memory_path)}
大小: {os.path.getsize(path_from_memory_path) if os.path.exists(path_from_memory_path) else 0} 字节

📁 Persona 模式路径 (共享空间):
{persona_path or '(无法获取)'}
存在: {os.path.exists(persona_path) if persona_path else False}
大小: {os.path.getsize(persona_path) if persona_path and os.path.exists(persona_path) else 0} 字节

📁 Session 模式路径:
{session_path}
存在: {os.path.exists(session_path)}
大小: {os.path.getsize(session_path) if os.path.exists(session_path) else 0} 字节

---
📄 Persona 路径内容开头:
{read_first_lines(persona_path)}

📄 Session 路径内容开头:
{read_first_lines(session_path)}

📄 get_full_log_path 返回路径内容开头:
{read_first_lines(path_from_method)}
"""

        await event.send(event.plain_result(debug_info))



    # ========== 以下是原有的命令方法，保持不变 ==========

    @filter.command("重建记忆向量")
    async def rebuild_vectors(self, event: AstrMessageEvent):
        """一次性命令，用于为所有现存的历史记忆生成向量。"""
        if not self.memory_manager or not self.semantic_searcher:
            await event.send(event.plain_result("错误：插件尚未完全初始化，无法执行该操作。"))
            return

        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        await event.send(event.plain_result(f"即将开始为'{role_name_raw}'的所有历史记忆重建向量索引，这可能需要一些时间，请稍候..."))

        all_diaries = self.memory_manager.read_diaries(user_id, session_id, role_name)
        if not all_diaries:
            await event.send(event.plain_result("没有找到任何记忆日志，无需重建。"))
            return

        summaries = [diary['summary'] for diary in all_diaries if 'summary' in diary and diary['summary'].strip()]
        if not summaries:
            await event.send(event.plain_result("记忆日志为空或格式不正确，无法提取任何有效文本内容进行向量化。"))
            return

        vectors = await self.semantic_searcher.get_embeddings(summaries)

        if not vectors or len(vectors) != len(summaries):
            logger.error(f"向量生成数量 ({len(vectors) if vectors else 0}) 与记忆数量 ({len(summaries)}) 不匹配。")
            await event.send(event.plain_result("生成向量时发生严重错误，请检查后台日志。部分或全部向量未能生成。"))
            return

        self.memory_manager.write_diary_vectors(user_id, session_id, role_name, vectors)
        await event.send(event.plain_result(f"任务完成！已成功为'{role_name_raw}'的 {len(vectors)} 条记忆重建了向量索引。"))

    @filter.command("查看记忆模式")
    async def view_memory_mode(self, event: AstrMessageEvent):
        """查看当前角色的记忆模式和共享空间"""
        if not self.memory_manager:
            return

        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        current_mode = self.memory_manager.get_persona_mode(role_name)
        current_space = self.memory_manager.get_current_space(role_name)
        all_spaces = self.memory_manager.get_persona_spaces(role_name)

        # 获取注入策略配置
        inject_mode = self.config.get("persona_memory_inject_mode", "current_user")
        whitelist = self.config.get("persona_whitelist_users", "")
        max_users = self.config.get("persona_max_users_inject", 5)

        mode_desc = {
            "session": "会话隔离模式\n• 每个用户+会话组合有独立记忆\n• 适合：私密对话、个人剧情",
            "persona": "人格共享模式\n• 同一空间内所有会话共享对话记录\n• 核心记忆仍按用户隔离\n• 适合：群聊、多人共同故事"
        }

        inject_mode_desc = {
            "current_user": "仅当前用户",
            "whitelist": f"白名单用户 ({whitelist if whitelist else '未设置'})",
            "all_users": f"所有用户 (最多{max_users}人)"
        }

        reply_lines = [
            f"📝 角色 '{role_name_raw}' 的记忆配置\n",
            f"当前模式：{current_mode}",
            mode_desc.get(current_mode, '')
        ]

        if current_mode == "persona":
            reply_lines.append(f"\n📂 当前空间：{current_space or '未设置'}")
            reply_lines.append(f"💉 记忆注入策略：{inject_mode_desc.get(inject_mode, inject_mode)}")

            if all_spaces:
                reply_lines.append(f"📋 可用空间：{', '.join(all_spaces)}")

            if current_space:
                users = self.memory_manager.get_persona_users(role_name, current_space)
                if users:
                    reply_lines.append(f"👥 空间内用户：{len(users)} 人")

        reply_lines.append("\n💡 使用【创建共享记忆 空间名】创建新空间")
        reply_lines.append("💡 使用【迁移到共享模式 空间名】迁移当前会话")

        await event.send(event.plain_result("\n".join(reply_lines)))

    @filter.command("查看共享空间")
    async def view_persona_spaces(self, event: AstrMessageEvent):
        """查看当前角色的所有共享空间"""
        if not self.memory_manager:
            return

        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        all_spaces = self.memory_manager.get_persona_spaces(role_name)
        current_space = self.memory_manager.get_current_space(role_name)

        if not all_spaces:
            await event.send(event.plain_result(
                f"角色 '{role_name_raw}' 还没有创建任何共享空间。\n\n"
                f"使用【创建共享记忆 空间名】创建一个新空间。"
            ))
            return

        reply_lines = [f"📂 角色 '{role_name_raw}' 的共享空间列表\n"]

        for space in all_spaces:
            users = self.memory_manager.get_persona_users(role_name, space)
            is_current = " ✅ (当前)" if space == current_space else ""
            reply_lines.append(f"• {space}{is_current} - {len(users)} 位用户")

        reply_lines.append(f"\n💡 使用【切换共享空间 空间名】切换到其他空间")

        await event.send(event.plain_result("\n".join(reply_lines)))

    @filter.command("切换记忆模式")
    async def switch_memory_mode(self, event: AstrMessageEvent):
        """切换当前角色的记忆模式（session/persona）"""
        if not self.memory_manager:
            return

        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        current_mode = self.memory_manager.get_persona_mode(role_name)

        if current_mode == "session":
            all_spaces = self.memory_manager.get_persona_spaces(role_name)
            if not all_spaces:
                await event.send(event.plain_result(
                    f"⚠️ 角色 '{role_name_raw}' 还没有共享空间。\n\n"
                    f"请先使用以下命令之一：\n"
                    f"• 【创建共享记忆 空间名】- 创建一个新的空共享空间\n"
                    f"• 【迁移到共享模式 空间名】- 将当前会话记忆迁移到新空间"
                ))
                return
            else:
                await event.send(event.plain_result(
                    f"角色 '{role_name_raw}' 已有共享空间：{', '.join(all_spaces)}\n\n"
                    f"请使用【切换共享空间 空间名】切换到指定空间。"
                ))
                return
        else:
            self.memory_manager.set_persona_mode(role_name, "session")
            await event.send(event.plain_result(
                f"✅ 角色 '{role_name_raw}' 已切换为：会话隔离模式\n\n"
                f"现在每个会话将使用独立的记忆数据。\n"
                f"注意：共享空间的数据仍然保留，可随时切换回去。"
            ))

    @filter.command("切换共享空间")
    async def switch_persona_space(self, event: AstrMessageEvent):
        """切换到指定的共享空间"""
        if not self.memory_manager:
            return

        args = event.message_str.replace("切换共享空间", "").strip()
        if not args:
            await event.send(event.plain_result(
                "请指定要切换到的空间名。\n"
                "例如：切换共享空间 群A冒险\n\n"
                "使用【查看共享空间】查看所有可用空间。"
            ))
            return

        space_name = args

        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        all_spaces = self.memory_manager.get_persona_spaces(role_name)

        if space_name not in all_spaces:
            await event.send(event.plain_result(
                f"❌ 共享空间 '{space_name}' 不存在。\n\n"
                f"可用空间：{', '.join(all_spaces) if all_spaces else '无'}\n\n"
                f"使用【创建共享记忆 {space_name}】创建新空间。"
            ))
            return

        try:
            self.memory_manager.set_current_space(role_name, space_name)
            users = self.memory_manager.get_persona_users(role_name, space_name)

            await event.send(event.plain_result(
                f"✅ 已切换到共享空间：{space_name}\n\n"
                f"👥 空间内用户：{len(users)} 人\n"
                f"现在的对话将记录到此空间的共享记忆中。"
            ))
        except Exception as e:
            logger.error(f"切换共享空间时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"切换失败: {e}"))

    @filter.command("迁移到共享模式")
    async def migrate_to_persona_mode(self, event: AstrMessageEvent):
        """将当前会话的记忆迁移到Persona共享模式的指定空间"""
        if not self.memory_manager:
            return

        args = event.message_str.replace("迁移到共享模式", "").strip()
        if not args:
            await event.send(event.plain_result(
                "请指定目标共享空间名。\n"
                "例如：迁移到共享模式 群A冒险\n\n"
                "如果空间不存在，将自动创建。"
            ))
            return

        space_name = args

        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        await event.send(event.plain_result(
            f"正在将 '{role_name_raw}' 的记忆迁移到共享空间 '{space_name}'...\n\n"
            f"📋 迁移策略：\n"
            f"• 对话记录 → 追加到共享记录（带用户标识）\n"
            f"• 记忆日志 → 追加到共享日志\n"
            f"• 核心记忆 → 保存到你的个人区域\n"
            f"• 锁定记忆 → 保存到你的个人区域\n"
            f"• 剧情框架 → 设为共享（如已存在则跳过）"
        ))

        try:
            result = self.memory_manager.migrate_session_to_persona(user_id, session_id, role_name, space_name)

            lines = ["\n📦 迁移结果：\n"]

            if result["actions"]:
                lines.append("✅ 已完成：")
                for action in result["actions"]:
                    lines.append(f"  • {action}")

            if result["warnings"]:
                lines.append("\n⚠️ 警告：")
                for warning in result["warnings"]:
                    lines.append(f"  • {warning}")

            if result["errors"]:
                lines.append("\n❌ 错误：")
                for error in result["errors"]:
                    lines.append(f"  • {error}")

            if result["success"]:
                lines.append(f"\n💡 迁移完成！")
                lines.append(f"角色 '{role_name_raw}' 现在使用共享空间 '{space_name}'")
                lines.append("其他用户/会话的对话也会记录到此空间中。")

            await event.send(event.plain_result("\n".join(lines)))

        except Exception as e:
            logger.error(f"迁移记忆时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"迁移失败: {e}"))

    @filter.command("创建共享记忆")
    async def create_persona_memory(self, event: AstrMessageEvent):
        """为当前角色创建一个全新的Persona共享记忆空间"""
        if not self.memory_manager:
            return

        args = event.message_str.replace("创建共享记忆", "").strip()
        if not args:
            await event.send(event.plain_result(
                "请指定共享空间名称。\n"
                "例如：创建共享记忆 群A冒险\n\n"
                "空间名建议使用有意义的名称，便于区分不同的故事线。"
            ))
            return

        space_name = args

        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        all_spaces = self.memory_manager.get_persona_spaces(role_name)
        if space_name in all_spaces:
            await event.send(event.plain_result(
                f"共享空间 '{space_name}' 已存在。\n\n"
                f"• 使用【切换共享空间 {space_name}】切换到此空间\n"
                f"• 使用【迁移到共享模式 {space_name}】将当前会话记忆迁移到此空间"
            ))
            return

        try:
            result = self.memory_manager.create_empty_persona(role_name, space_name)

            if result["success"]:
                await event.send(event.plain_result(
                    f"✅ {result['message']}\n\n"
                    f"角色 '{role_name_raw}' 现在使用共享空间 '{space_name}'：\n"
                    f"• 所有用户/会话的对话会记录到此空间\n"
                    f"• 每个用户有独立的核心记忆和锁定记忆\n\n"
                    f"💡 如需将现有会话的记忆也加入此空间，请在对应会话使用：\n"
                    f"【迁移到共享模式 {space_name}】"
                ))
            else:
                error_msg = result.get('message', '') or ', '.join(result.get('errors', []))
                await event.send(event.plain_result(f"❌ 创建失败：{error_msg}"))

        except Exception as e:
            logger.error(f"创建共享记忆时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"创建失败: {e}"))

    @platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def handle_file_for_update(self, event: AstrMessageEvent, *args, **kwargs):
        if not self.memory_manager: return
        user_id = str(event.get_sender_id())
        session_info = self.update_sessions.get(user_id)

        if not session_info or session_info.get("type") != "file": return
        if not (file_comp := next((comp for comp in event.get_messages() if isinstance(comp, Comp.File)), None)): return

        if (datetime.now().timestamp() - session_info["timestamp"]) > self.update_timeout:
            self.update_sessions.pop(user_id, None)
            return

        if user_id in self.timeout_tasks:
            self.timeout_tasks.pop(user_id).cancel()

        command_type = session_info["command"]

        try:
            await event.send(event.plain_result(f"收到文件 '{file_comp.name}'，正在为你更新 {command_type}..."))

            async with httpx.AsyncClient() as client:
                response = await client.get(file_comp.url, timeout=60)
            new_content_bytes = response.content

            if command_type != "对话记录":
                try:
                    json.loads(new_content_bytes)
                except json.JSONDecodeError:
                    await event.send(event.plain_result("文件内容不是有效的JSON格式，更新失败。"))
                    return

            role_name_raw = await get_persona_id(self.context, event) or "default"
            role_name = get_role_name_without_ext(role_name_raw)
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)

            if command_type == "核心记忆":
                target_path = self.memory_manager.get_core_memory_path(user_id, session_id, role_name)
                temp_file = f"{target_path}.tmp"
                with open(temp_file, 'wb') as f: f.write(new_content_bytes)
                shutil.move(temp_file, target_path)

            elif command_type == "记忆日志":
                target_path = self.memory_manager.get_diary_path(user_id, session_id, role_name)
                temp_file = f"{target_path}.tmp"
                with open(temp_file, 'wb') as f: f.write(new_content_bytes)
                shutil.move(temp_file, target_path)
                await event.send(event.plain_result("记忆日志文件已更新。注意：向量索引未自动更新，请手动执行一次【重建记忆向量】来同步语义搜索功能。"))

            elif command_type == "剧情框架":
                target_path = self.memory_manager.get_plot_framework_path(user_id, session_id, role_name)
                temp_file = f"{target_path}.tmp"
                with open(temp_file, 'wb') as f: f.write(new_content_bytes)
                shutil.move(temp_file, target_path)

            elif command_type == "剧情总结":
                target_path = self.memory_manager.get_plot_summary_path(user_id, session_id, role_name)
                temp_file = f"{target_path}.tmp"
                with open(temp_file, 'wb') as f: f.write(new_content_bytes)
                shutil.move(temp_file, target_path)

            elif command_type == "对话记录":
                target_path = self.memory_manager.get_full_log_path(user_id, session_id, role_name)
                temp_file = f"{target_path}.tmp"
                with open(temp_file, 'wb') as f: f.write(new_content_bytes)
                shutil.move(temp_file, target_path)

            elif command_type == "锁定记忆":
                target_path = self.memory_manager.get_locked_memory_path(user_id, session_id, role_name)
                temp_file = f"{target_path}.tmp"
                with open(temp_file, 'wb') as f: f.write(new_content_bytes)
                shutil.move(temp_file, target_path)

            await event.send(event.plain_result(f"'{role_name_raw}' 的 {command_type} 文件已成功更新。"))

        except Exception as e:
            logger.error(f"处理文件进行更新时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"处理文件时发生内部错误: {e}"))
        finally:
            self.update_sessions.pop(user_id, None)
            event.stop_event()

    async def timeout_handler(self, user_id: str, event: AstrMessageEvent):
        try:
            await asyncio.sleep(self.update_timeout)
            if user_id in self.update_sessions:
                session_info = self.update_sessions.pop(user_id, None)
                if session_info:
                    logger.info(f"用户 {user_id} 的 {session_info['command']} 文件更新已超时取消")
        except asyncio.CancelledError:
            logger.debug(f"用户 {user_id} 的超时任务已被取消")
        except Exception as e:
            logger.error(f"处理文件更新超时时发生错误: {e}", exc_info=True)

    @filter.command("查看核心记忆")
    async def view_core_memory(self, event: AstrMessageEvent):
        if not self.memory_manager: return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            memory_data = self.memory_manager.read_core_memory(user_id, session_id, role_name)
            if not memory_data:
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过核心记忆"))
                return

            content = memory_data.get("content", "核心记忆文件为空或格式错误")
            last_updated = memory_data.get("timestamp", "未知时间")

            reply_text = f"--- {role_name_raw} 的核心记忆 ---\n最后更新于: {last_updated}\n\n{content}"

            if len(reply_text) > 4500:
                await event.send(event.plain_result(f"核心记忆内容过长（共{len(reply_text)}字），请使用【获取核心记忆】命令以文件形式获取。"))
                return

            node = Comp.Node(
                uin=10000,
                name="记忆系统",
                content=[Comp.Plain(reply_text)]
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(f"查看核心记忆时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"查看核心记忆时发生内部错误: {e}"))

    @filter.command("查看记忆日志")
    async def view_memory_log(self, event: AstrMessageEvent):
        if not self.memory_manager: return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            args = event.message_str.strip().split()
            num_to_show = 10
            if len(args) > 1:
                try: num_to_show = int(args[1])
                except (ValueError, IndexError): pass

            max_show = 30
            if num_to_show > max_show: num_to_show = max_show

            diary_data = self.memory_manager.read_diaries(user_id, session_id, role_name)
            if not diary_data:
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过记忆日志"))
                return

            recent_entries = diary_data[-num_to_show:]
            reply_text = f"--- {role_name_raw} 的最近 {len(recent_entries)} 条记忆日志 ---\n\n"
            for entry in recent_entries:
                timestamp = entry.get("timestamp", "未知")
                summary = entry.get("summary", "")
                reply_text += f"【时间】: {timestamp}\n【摘要】:\n{summary}\n--------------------\n"

            if len(reply_text) > 4500:
                await event.send(event.plain_result(f"记忆日志内容过长（共{len(reply_text)}字），请使用【获取记忆日志】命令以文件形式获取。"))
                return

            node = Comp.Node(
                uin=10000,
                name="记忆系统",
                content=[Comp.Plain(reply_text)]
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(f"查看记忆日志时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"查看记忆日志时发生内部错误: {e}"))

    @filter.command("查看剧情框架")
    async def view_plot_framework_single(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            raw_framework_data = self.memory_manager.read_plot_framework(user_id, session_id, role_name)
            framework_data = None

            if isinstance(raw_framework_data, list) and raw_framework_data:
                framework_data = raw_framework_data[-1]
            elif isinstance(raw_framework_data, dict):
                framework_data = raw_framework_data

            if not framework_data or not framework_data.get('content'):
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过剧情框架"))
                return

            content = framework_data.get('content', '')
            timestamp = framework_data.get('timestamp', '未知时间')

            reply_text = f"--- {role_name_raw} 的剧情框架 ---\n最后更新于: {timestamp}\n\n{content}"

            if len(reply_text) > 4500:
                await event.send(event.plain_result(f"剧情框架内容过长（共{len(reply_text)}字），请使用【获取剧情框架】命令以文件形式获取。"))
                return

            node = Comp.Node(
                uin=10000,
                name="记忆系统",
                content=[Comp.Plain(reply_text)]
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(f"查看剧情框架时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"查看剧情框架时发生内部错误: {e}"))

    @filter.command("查看剧情总结")
    async def view_plot_framework(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            args = event.message_str.strip().split()
            num_to_show = 1
            if len(args) > 1:
                try:
                    num_to_show = int(args[1])
                except (ValueError, IndexError):
                    pass

            max_show = 5
            if num_to_show > max_show:
                num_to_show = max_show

            frameworks = self.memory_manager.read_plot_summaries(user_id, session_id, role_name)
            if not frameworks:
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过剧情总结"))
                return

            recent_entries = frameworks[-num_to_show:]
            reply_text = f"--- {role_name_raw} 的最近 {len(recent_entries)} 条剧情总结 ---\n\n"
            for entry in recent_entries:
                timestamp = entry.get("timestamp", "未知")
                summary = entry.get("summary", "")
                reply_text += f"【生成时间】: {timestamp}\n【剧情总结】:\n{summary}\n\n{'='*30}\n\n"

            if len(reply_text) > 4500:
                await event.send(event.plain_result(f"剧情总结内容过长（共{len(reply_text)}字），请使用【获取剧情总结】命令以文件形式获取。"))
                return

            node = Comp.Node(
                uin=10000,
                name="记忆系统",
                content=[Comp.Plain(reply_text)]
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(f"查看剧情总结时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"查看剧情总结时发生内部错误: {e}"))

    @filter.command("查看锁定记忆")
    async def view_locked_memories(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            locked_items = self.memory_manager.read_locked_memories(user_id, session_id, role_name)

            if not locked_items:
                await event.send(event.plain_result(f"'{role_name_raw}'目前没有锁定的记忆"))
                return

            reply_text = f"--- {role_name_raw} 的锁定记忆 ---\n\n"
            for item in locked_items:
                item_id = item.get("id", "?")
                content = item.get("content", "")
                locked_at = item.get("locked_at", "未知时间")
                reply_text += f"[{item_id}] {content}\n    └ 锁定于: {locked_at}\n\n"

            reply_text += "---\n使用【解锁记忆 序号】可移除指定记忆（可多个）\n使用【清空锁定记忆】可移除所有"

            if len(reply_text) > 4500:
                await event.send(event.plain_result(f"锁定记忆内容过长（共{len(reply_text)}字），请使用【获取锁定记忆】命令以文件形式获取。"))
                return

            node = Comp.Node(
                uin=10000,
                name="记忆系统",
                content=[Comp.Plain(reply_text)]
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(f"查看锁定记忆时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"查看锁定记忆时发生内部错误: {e}"))

    @filter.command("获取核心记忆")
    async def get_core_memory_file(self, event: AstrMessageEvent):
        if not self.memory_manager: return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            file_path = self.memory_manager.get_core_memory_path(user_id, session_id, role_name)
            if not os.path.exists(file_path):
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过核心记忆文件"))
                return

            send_path = self._convert_to_host_path(file_path)
            file_to_send = Comp.File(file=send_path, name=os.path.basename(file_path))
            await event.send(event.chain_result([file_to_send]))
        except Exception as e:
            logger.error(f"获取核心记忆文件时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"获取核心记忆文件时发生内部错误: {e}"))

    @filter.command("获取记忆日志")
    async def get_memory_log_file(self, event: AstrMessageEvent):
        if not self.memory_manager: return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            file_path = self.memory_manager.get_diary_path(user_id, session_id, role_name)
            if not os.path.exists(file_path):
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过记忆日志文件"))
                return

            send_path = self._convert_to_host_path(file_path)
            file_to_send = Comp.File(file=send_path, name=os.path.basename(file_path))
            await event.send(event.chain_result([file_to_send]))
        except Exception as e:
            logger.error(f"获取记忆日志文件时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"获取记忆日志文件时发生内部错误: {e}"))

    @filter.command("获取剧情框架")
    async def get_plot_framework_single_file(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            file_path = self.memory_manager.get_plot_framework_path(user_id, session_id, role_name)
            if not os.path.exists(file_path):
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过剧情框架文件"))
                return

            send_path = self._convert_to_host_path(file_path)
            file_to_send = Comp.File(file=send_path, name=os.path.basename(file_path))
            await event.send(event.chain_result([file_to_send]))
        except Exception as e:
            logger.error(f"获取剧情框架文件时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"获取剧情框架文件时发生内部错误: {e}"))

    @filter.command("获取剧情总结")
    async def get_plot_framework_file(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            file_path = self.memory_manager.get_plot_summary_path(user_id, session_id, role_name)
            if not os.path.exists(file_path):
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过剧情总结文件"))
                return

            send_path = self._convert_to_host_path(file_path)
            file_to_send = Comp.File(file=send_path, name=os.path.basename(file_path))
            await event.send(event.chain_result([file_to_send]))
        except Exception as e:
            logger.error(f"获取剧情总结文件时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"获取剧情总结文件时发生内部错误: {e}"))

    @filter.command("获取对话记录")
    async def get_full_log_file(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            file_path = self.memory_manager.get_full_log_path(user_id, session_id, role_name)
            if not os.path.exists(file_path):
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'记录过完整对话"))
                return

            if os.path.getsize(file_path) == 0:
                await event.send(event.plain_result(f"'{role_name_raw}'的完整对话记录为空"))
                return

            send_path = self._convert_to_host_path(file_path)
            file_to_send = Comp.File(file=send_path, name=os.path.basename(file_path))
            await event.send(event.chain_result([file_to_send]))
        except Exception as e:
            logger.error(f"获取完整对话记录文件时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"获取完整对话记录文件时发生内部错误: {e}"))

    @filter.command("获取锁定记忆")
    async def get_locked_memory_file(self, event: AstrMessageEvent):
        if not self.memory_manager: return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            file_path = self.memory_manager.get_locked_memory_path(user_id, session_id, role_name)
            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                await event.send(event.plain_result(f"还没有为'{role_name_raw}'生成过锁定记忆文件，或文件为空。"))
                return

            send_path = self._convert_to_host_path(file_path)
            file_to_send = Comp.File(file=send_path, name=os.path.basename(file_path))
            await event.send(event.chain_result([file_to_send]))
        except Exception as e:
            logger.error(f"获取锁定记忆文件时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"获取锁定记忆文件时发生内部错误: {e}"))

    @filter.command("更新核心文件")
    async def update_core_memory_file_command(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        self.update_sessions[user_id] = {"command": "核心记忆", "type": "file", "timestamp": datetime.now().timestamp()}
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
        self.timeout_tasks[user_id] = asyncio.create_task(self.timeout_handler(user_id, event))
        await event.send(event.plain_result(f"请在 {self.update_timeout} 秒内发送你修改好的【核心记忆】json文件。"))

    @filter.command("更新记忆日志")
    async def update_memory_log_command(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        self.update_sessions[user_id] = {"command": "记忆日志", "type": "file", "timestamp": datetime.now().timestamp()}
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
        self.timeout_tasks[user_id] = asyncio.create_task(self.timeout_handler(user_id, event))
        await event.send(event.plain_result(f"请在 {self.update_timeout} 秒内发送你修改好的【记忆日志】json文件。"))

    @filter.command("更新剧情框架")
    async def update_plot_framework_file_command(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        self.update_sessions[user_id] = {"command": "剧情框架", "type": "file", "timestamp": datetime.now().timestamp()}
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
        self.timeout_tasks[user_id] = asyncio.create_task(self.timeout_handler(user_id, event))
        await event.send(event.plain_result(f"请在 {self.update_timeout} 秒内发送你修改好的【剧情框架】json文件。"))

    @filter.command("更新剧情总结")
    async def update_plot_summary_command(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        self.update_sessions[user_id] = {"command": "剧情总结", "type": "file", "timestamp": datetime.now().timestamp()}
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
        self.timeout_tasks[user_id] = asyncio.create_task(self.timeout_handler(user_id, event))
        await event.send(event.plain_result(f"请在 {self.update_timeout} 秒内发送你修改好的【剧情总结】json文件。"))

    @filter.command("更新对话记录")
    async def update_full_log_command(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        self.update_sessions[user_id] = {"command": "对话记录", "type": "file", "timestamp": datetime.now().timestamp()}
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
        self.timeout_tasks[user_id] = asyncio.create_task(self.timeout_handler(user_id, event))
        await event.send(event.plain_result(f"请在 {self.update_timeout} 秒内发送你修改好的【对话记录】txt文件。"))

    @filter.command("更新锁定记忆")
    async def update_locked_memory_file_command(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        self.update_sessions[user_id] = {"command": "锁定记忆", "type": "file", "timestamp": datetime.now().timestamp()}
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
        self.timeout_tasks[user_id] = asyncio.create_task(self.timeout_handler(user_id, event))
        await event.send(event.plain_result(f"请在 {self.update_timeout} 秒内发送你修改好的【锁定记忆】json文件。"))

    @filter.command("清空核心记忆")
    async def clear_core_memory(self, event: AstrMessageEvent):
        if not self.memory_manager: return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            file_path = self.memory_manager.get_core_memory_path(user_id, session_id, role_name)

            if os.path.exists(file_path):
                os.remove(file_path)
                await event.send(event.plain_result(f"'{role_name_raw}' 的核心记忆文件已被成功清空。"))
            else:
                await event.send(event.plain_result(f"'{role_name_raw}' 还没有核心记忆文件，无需清空。"))
        except Exception as e:
            logger.error(f"清空核心记忆时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"清空核心记忆时发生内部错误: {e}"))

    @filter.command("清空记忆日志")
    async def clear_memory_log(self, event: AstrMessageEvent):
        if not self.memory_manager: return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            self.memory_manager.write_diaries(user_id, session_id, role_name, [])
            self.memory_manager.write_diary_vectors(user_id, session_id, role_name, [])

            await event.send(event.plain_result(f"'{role_name_raw}' 的记忆日志及其向量索引已被成功清空。"))
        except Exception as e:
            logger.error(f"清空记忆日志时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"清空记忆日志时发生内部错误: {e}"))

    @filter.command("清空剧情框架")
    async def clear_plot_framework_single(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            file_path = self.memory_manager.get_plot_framework_path(user_id, session_id, role_name)
            if os.path.exists(file_path):
                os.remove(file_path)
                await event.send(event.plain_result(f"'{role_name_raw}' 的剧情框架已被成功清空。"))
            else:
                await event.send(event.plain_result(f"'{role_name_raw}' 还没有剧情框架，无需清空。"))
        except Exception as e:
            logger.error(f"清空剧情框架时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"清空剧情框架时发生内部错误: {e}"))

    @filter.command("清空剧情总结")
    async def clear_plot_framework(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            self.memory_manager.write_plot_summaries(user_id, session_id, role_name, [])

            await event.send(event.plain_result(f"'{role_name_raw}' 的剧情总结已被成功清空。"))
        except Exception as e:
            logger.error(f"清空剧情总结时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"清空剧情总结时发生内部错误: {e}"))

    @filter.command("清空对话记录")
    async def clear_full_log(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            self.memory_manager.clear_full_log(user_id, session_id, role_name)

            await event.send(event.plain_result(f"'{role_name_raw}' 的完整对话记录已被成功清空。下一次对话将开始新的记录。"))
        except Exception as e:
            logger.error(f"清空完整对话记录时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"清空完整对话记录时发生内部错误: {e}"))

    @filter.command("清空锁定记忆")
    async def clear_locked_memories_command(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            self.memory_manager.clear_locked_memories(user_id, session_id, role_name)
            await event.send(event.plain_result(f"'{role_name_raw}' 的锁定记忆已被成功清空。"))
        except Exception as e:
            logger.error(f"清空锁定记忆时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"清空锁定记忆时发生内部错误: {e}"))

    @filter.command("锁定记忆")
    async def add_locked_memory_command(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            content = event.message_str.replace("锁定记忆", "").strip()
            if not content:
                await event.send(event.plain_result("请提供要锁定的内容。例如：锁定记忆 我们不是一个世界的人"))
                return

            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            added = self.memory_manager.add_locked_memory(user_id, session_id, role_name, content)

            if added:
                await event.send(event.plain_result(f"已成功锁定记忆：{content}"))
            else:
                await event.send(event.plain_result("该内容已存在于锁定记忆中，无需重复添加。"))
        except Exception as e:
            logger.error(f"锁定记忆时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"锁定记忆时发生内部错误: {e}"))

    @filter.command("解锁记忆")
    async def remove_locked_memory_command(self, event: AstrMessageEvent):
        if not self.memory_manager:
            return
        try:
            args_str = event.message_str.replace("解锁记忆", "").strip()
            if not args_str:
                await event.send(event.plain_result("请提供要解锁的记忆序号，可提供多个，用空格隔开。例如：解锁记忆 1 3 5"))
                return

            item_ids_to_remove = []
            invalid_args = []
            for arg in args_str.split():
                try:
                    item_ids_to_remove.append(int(arg))
                except ValueError:
                    invalid_args.append(arg)

            if invalid_args:
                await event.send(event.plain_result(f"以下序号不是有效的数字: {', '.join(invalid_args)}"))
                return

            if not item_ids_to_remove:
                await event.send(event.plain_result("未提供任何有效序号。"))
                return

            role_name_raw = await get_persona_id(self.context, event) or "default"
            user_id = str(event.get_sender_id())
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name = get_role_name_without_ext(role_name_raw)

            removed = self.memory_manager.remove_locked_memory(user_id, session_id, role_name, item_ids_to_remove)

            if removed:
                await event.send(event.plain_result(f"已成功尝试解锁序号为 {', '.join(map(str, item_ids_to_remove))} 的记忆。"))
            else:
                await event.send(event.plain_result(f"未找到任何匹配序号的锁定记忆，或记忆列表为空。"))
        except Exception as e:
            logger.error(f"解锁记忆时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"解锁记忆时发生内部错误: {e}"))

    @filter.command("修改核心记忆")
    async def edit_core_memory_command(self, event: AstrMessageEvent):
        instruction = event.message_str.replace("修改核心记忆", "").strip()
        if not instruction:
            await event.send(event.plain_result("请告诉我具体要如何修改核心记忆。例如：修改核心记忆 增加一条备忘录：我喜欢在下午喝咖啡。"))
            return

        await event.send(event.plain_result("正在理解你的修改指令，请稍候..."))

        try:
            await self.execute_memory_edit(event, instruction)
        except Exception as e:
            logger.error(f"执行记忆修改时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"抱歉，修改记忆时发生了内部错误: {e}"))

    async def execute_memory_edit(self, event: AstrMessageEvent, instruction: str):
        _ledger_set(event.get_sender_id(), kind="memory_manual", group_id=event.get_group_id())  # ledger patch
        if not self.memory_manager: return
        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        memory_data = self.memory_manager.read_core_memory(user_id, session_id, role_name)
        existing_memory_content = memory_data.get("content", "") if memory_data else ""

        edit_prompt = f"""
你的任务是扮演一个精确的文本内容修订助理。请根据用户提供的【原始核心记忆】和【修改指令】，生成一份更新后的、完整的核心记忆文本。

【规则】:
1. 你的唯一输出必须是修改后的完整核心记忆文本内容本身，绝对不能包含任何其他文字、解释、或JSON结构。
2. 严格遵循用户的【修改指令】，对【原始核心记忆】进行增加、删除或修改。不要添加任何指令之外的内容。
3. 如果【原始核心记忆】为空，则将【修改指令】的内容作为初始内容。
4. 保持原始文本的格式和风格。

---
【原始核心记忆】:
{existing_memory_content}

【修改指令】:
"{instruction}"
"""
        ai_response = await self._call_ai_with_fallback(
            prompt="请根据指令编辑文本。",
            system_prompt=edit_prompt,
            task_name="核心记忆修改"
        )

        if not ai_response:
            await event.send(event.plain_result("修改失败：AI未能生成有效内容。"))
            return

        self.memory_manager.write_core_memory(user_id, session_id, role_name, ai_response.strip())
        await event.send(event.plain_result(f"'{role_name_raw}' 的核心记忆已根据你的指令修改完毕。"))

    @filter.command("修改剧情框架")
    async def edit_plot_framework_command(self, event: AstrMessageEvent):
        instruction = event.message_str.replace("修改剧情框架", "").strip()
        if not instruction:
            await event.send(event.plain_result("请告诉我具体要如何修改剧情框架。例如：修改剧情框架 补充一下我们第一次见面的场景。"))
            return

        await event.send(event.plain_result("正在理解你的修改指令，请稍候..."))

        try:
            await self.execute_plot_framework_edit(event, instruction)
        except Exception as e:
            logger.error(f"执行剧情框架修改时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"抱歉，修改剧情框架时发生了内部错误: {e}"))

    async def execute_plot_framework_edit(self, event: AstrMessageEvent, instruction: str):
        _ledger_set(event.get_sender_id(), kind="memory_manual", group_id=event.get_group_id())  # ledger patch
        if not self.memory_manager: return
        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name_raw = await get_persona_id(self.context, event) or "default"
        role_name = get_role_name_without_ext(role_name_raw)

        framework_data = self.memory_manager.read_plot_framework(user_id, session_id, role_name)
        existing_framework = framework_data.get("content", "") if framework_data else ""

        edit_prompt = f"""
你的任务是扮演一个精确的剧情框架修订助理。请根据用户提供的【原始剧情框架】和【修改指令】，生成一份更新后的、完整的剧情框架文本。

【核心规则】:
1. 你的唯一输出必须是修改后的完整剧情框架文本内容本身，绝对不能包含任何其他文字、解释、或JSON结构。
2. 严格遵循用户的【修改指令】，对【原始剧情框架】进行增加、删除或修改。
3. 如果【原始剧情框架】为空，则根据【修改指令】创建初始内容。
4. 保持剧情框架的整体风格和结构。

【特别注意 - 关于"总结"类指令】:
- 如果用户的指令中包含"总结"、"概括"、"精简"等词汇，你必须先将用户提供的内容**压缩总结**成简洁的叙事段落，然后再添加到剧情框架中。
- 总结时保留核心事件和关键转折。

【格式强制要求】:
- 禁止使用markdown格式（如#、##、**等）。
- 禁止使用括号()包裹内容。
- 禁止使用条目列表格式。
- 必须使用自然流畅的叙事段落。

---
【原始剧情框架】:
{existing_framework if existing_framework else "（暂无剧情框架）"}

【修改指令】:
"{instruction}"
"""
        ai_response = await self._call_ai_with_fallback(
            prompt="请根据指令编辑剧情框架。",
            system_prompt=edit_prompt,
            task_name="剧情框架修改"
        )

        if not ai_response:
            await event.send(event.plain_result("修改失败：AI未能生成有效内容。"))
            return

        new_content = ai_response.strip()
        self.memory_manager.write_plot_framework(user_id, session_id, role_name, new_content)

        if len(new_content) > 4500:
            await event.send(event.plain_result(f"'{role_name_raw}' 的剧情框架已修改完毕！内容较长（共{len(new_content)}字），请使用【获取剧情框架】命令以文件形式获取。"))
        else:
            result_text = f"--- {role_name_raw} 的剧情框架（已修改）---\n\n{new_content}"
            node = Comp.Node(
                uin=10000,
                name="记忆系统",
                content=[Comp.Plain(result_text)]
            )
            await event.send(event.chain_result([node]))

    @filter.command("剧情总结")
    async def force_plot_framework_command(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name = await get_persona_id(self.context, event) or "default"
        role_name_without_ext = get_role_name_without_ext(role_name)

        full_log_content = self.memory_manager.read_full_log(user_id, session_id, role_name_without_ext)
        if not full_log_content or len(full_log_content.strip()) < 100:
            await event.send(event.plain_result("完整对话记录为空或过短，无法生成剧情总结。请先进行一些对话。"))
            return

        with active_plot_tasks_lock:
            if user_id in active_plot_tasks:
                await event.send(event.plain_result("正在进行剧情总结，请勿重复命令"))
                return
            active_plot_tasks.add(user_id)

        await event.send(event.plain_result("收到，我来梳理一下我们的故事脉络..."))
        asyncio.create_task(self._summarize_plot_framework_threaded(
            user_id, role_name, session_id, event.unified_msg_origin, event=event, force=True  # 添加 force=True
        ))

    @filter.command("重新总结剧情")
    async def force_resum_plot_summary(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name = await get_persona_id(self.context, event) or "default"
        role_name_without_ext = get_role_name_without_ext(role_name)

        with active_plot_tasks_lock:
            if user_id in active_plot_tasks:
                await event.send(event.plain_result("正在进行剧情总结，请勿重复命令"))
                return
            active_plot_tasks.add(user_id)

        await event.send(event.plain_result("收到，我来重新梳理一下完整的剧情..."))

        existing = self.memory_manager.read_plot_summaries(user_id, session_id, role_name_without_ext)
        if existing:
            existing[-1]['summarized_up_to_line'] = 0
            self.memory_manager.write_plot_summaries(user_id, session_id, role_name_without_ext, existing)

        asyncio.create_task(self._summarize_plot_framework_threaded(user_id, role_name, session_id, event.unified_msg_origin, event=event))

    @filter.command("总结记忆日志")
    async def summarize_diary_to_framework_command(self, event: AstrMessageEvent):
        """读取所有记忆日志，生成全新的剧情框架"""
        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name = await get_persona_id(self.context, event) or "default"
        role_name_without_ext = get_role_name_without_ext(role_name)

        all_diaries = self.memory_manager.read_diaries(user_id, session_id, role_name)
        if not all_diaries:
            await event.send(event.plain_result("记忆日志为空，无法生成剧情框架。"))
            return

        diary_texts = []
        for diary in all_diaries:
            timestamp = diary.get('timestamp', '未知时间')
            summary = diary.get('summary', '')
            if summary:
                diary_texts.append(f"【{timestamp}】\n{summary}")

        full_diary_content = "\n\n---\n\n".join(diary_texts)

        if len(full_diary_content.strip()) < 100:
            await event.send(event.plain_result("记忆日志内容过短，无法生成剧情框架。"))
            return

        with active_plot_tasks_lock:
            if user_id in active_plot_tasks:
                await event.send(event.plain_result("正在进行剧情框架生成，请勿重复命令"))
                return
            active_plot_tasks.add(user_id)

        total_chars = len(full_diary_content)
        chunk_size = 100000
        total_chunks = (total_chars + chunk_size - 1) // chunk_size

        if total_chunks > 1:
            estimated_time = total_chunks * 1.5 + 2
            await event.send(event.plain_result(f"记忆日志共 {len(all_diaries)} 条（{total_chars} 字），需要分 {total_chunks} 次总结，预计需要 {estimated_time:.0f} 分钟，请耐心等待..."))
        else:
            await event.send(event.plain_result(f"记忆日志共 {len(all_diaries)} 条（{total_chars} 字），开始生成剧情框架..."))

        asyncio.create_task(self._summarize_diary_to_framework_threaded(user_id, role_name, session_id, event.unified_msg_origin, event, full_diary_content))

    async def _summarize_diary_to_framework_threaded(self, user_id, role_name, session_id, unified_msg_origin, event, full_diary_content):
        success = False
        try:
            success = await self._summarize_diary_to_framework(user_id, role_name, session_id, unified_msg_origin, event, full_diary_content)
        except Exception as e:
            logger.error(f"从记忆日志生成剧情框架为用户 {user_id} 执行时出错: {e}", exc_info=True)
        finally:
            with active_plot_tasks_lock:
                if user_id in active_plot_tasks:
                    active_plot_tasks.remove(user_id)

            if not success:
                try:
                    if event:
                        await event.send(event.plain_result("⚠️ 剧情框架生成任务失败，可使用【总结记忆日志】命令手动重试。"))
                    else:
                        await self.context.send_message(unified_msg_origin, [Comp.Plain("⚠️ 剧情框架生成任务失败，可使用【总结记忆日志】命令手动重试。")])
                except Exception as e:
                    logger.warning(f"发送失败通知时出错: {e}")

    async def _summarize_diary_to_framework(self, user_id, role_name, session_id, unified_msg_origin, event, full_diary_content):
        _ledger_set(user_id, unified_msg_origin, "memory_manual")  # ledger patch
        role_name_without_ext = get_role_name_without_ext(role_name)

        try:
            persona_object = await self.context.persona_manager.get_persona(persona_id=role_name)
            persona_content = persona_object.system_prompt if persona_object else ""

            chunk_size = 100000
            diary_entries = full_diary_content.split("\n\n---\n\n")
            chunks = []
            current_chunk_entries = []
            current_chunk_size = 0

            for entry in diary_entries:
                entry_size = len(entry) + 7

                if current_chunk_size >= chunk_size and current_chunk_entries:
                    chunks.append("\n\n---\n\n".join(current_chunk_entries))
                    current_chunk_entries = [entry]
                    current_chunk_size = entry_size
                else:
                    current_chunk_entries.append(entry)
                    current_chunk_size += entry_size

            if current_chunk_entries:
                chunks.append("\n\n---\n\n".join(current_chunk_entries))

            total_chunks = len(chunks)
            chunk_summaries = []

            for idx, chunk in enumerate(chunks):
                chunk_num = idx + 1

                if total_chunks > 1:
                    try:
                        await self.context.send_message(
                            unified_msg_origin,
                            [Comp.Plain(f"📝 正在总结第 {chunk_num}/{total_chunks} 块记忆日志...")]
                        )
                    except Exception:
                        pass

                chunk_prompt = f"""
你是一位专业的故事编剧。你的任务是阅读一系列记忆日志，并从中提炼出完整的剧情脉络。

### 记忆日志片段（第{chunk_num}部分，共{total_chunks}部分）:
{chunk}
---

请根据以上记忆日志，以 {role_name_without_ext} 的第一人称视角，撰写一份剧情总结。

### 写作规范:
1. 使用第一人称（"我"代指{role_name_without_ext}，"你"代指用户）。
2. 保持客观，忠实于记忆内容。
3. 禁止使用markdown格式。
4. 必须使用自然流畅的叙事段落。

### 你的角色设定:
{persona_content}

请直接开始撰写：
"""

                chunk_response = await self._call_ai_with_fallback(
                    prompt="请总结这些记忆日志的剧情要点。",
                    system_prompt=chunk_prompt,
                    task_name=f"记忆日志分块{chunk_num}"
                )

                if chunk_response and len(chunk_response.strip()) > 20:
                    chunk_summaries.append(f"【第{chunk_num}部分】\n{chunk_response.strip()}")
                else:
                    chunk_summaries.append(f"【第{chunk_num}部分】\n（此部分总结失败）")

                if idx < len(chunks) - 1:
                    await asyncio.sleep(90)

            if total_chunks == 1:
                final_content = chunk_summaries[0].replace("【第1部分】\n", "").strip()
            else:
                try:
                    await self.context.send_message(
                        unified_msg_origin,
                        [Comp.Plain(f"✅ 所有 {total_chunks} 块已总结完毕，正在进行最终整合...")]
                    )
                except Exception:
                    pass

                all_summaries_text = "\n\n---\n\n".join(chunk_summaries)

                merge_prompt = f"""
你是一位专业的故事编剧。请将多个分段剧情总结整合成一份完整的剧情框架。

### 各分段剧情总结:
{all_summaries_text}
---

请将以上所有分段总结整合成一份完整的剧情框架。

### 写作规范:
1. 使用第一人称（"我"代指{role_name_without_ext}，"你"代指用户）。
2. 禁止使用markdown格式。
3. 必须使用自然流畅的叙事段落。

请直接开始撰写：
"""

                final_response = await self._call_ai_with_fallback(
                    prompt="请将所有分段总结整合成完整的剧情框架。",
                    system_prompt=merge_prompt,
                    task_name="记忆日志整合"
                )

                if not final_response or len(final_response.strip()) < 50:
                    final_content = "\n\n".join([s.replace(f"【第{i+1}部分】\n", "") for i, s in enumerate(chunk_summaries)])
                else:
                    final_content = final_response.strip()

            self.memory_manager.write_plot_framework(user_id, session_id, role_name_without_ext, final_content)

            try:
                if len(final_content) > 4500:
                    if event:
                        await event.send(event.plain_result(f"✅ 剧情框架已生成完毕！内容较长（共{len(final_content)}字），请使用【获取剧情框架】命令以文件形式获取。"))
                    else:
                        await self.context.send_message(unified_msg_origin, [Comp.Plain(f"✅ 剧情框架已生成完毕！内容较长（共{len(final_content)}字），请使用【获取剧情框架】命令以文件形式获取。")])
                else:
                    result_text = f"✅ 剧情框架已从记忆日志生成完毕！\n\n---\n{final_content}"
                    if event:
                        node = Comp.Node(uin=10000, name="记忆系统", content=[Comp.Plain(result_text)])
                        await event.send(event.chain_result([node]))
                    else:
                        await self.context.send_message(unified_msg_origin, [Comp.Plain(result_text)])
            except Exception as e:
                logger.warning(f"发送结果时出错: {e}")

            return True

        except Exception as e:
            logger.error(f"从记忆日志生成剧情框架过程中发生错误: {e}", exc_info=True)
            return False

    @filter.command("整理剧情框架")
    async def manual_compress_framework_command(self, event: AstrMessageEvent):
        """手动触发剧情框架的整理和浓缩"""
        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name = await get_persona_id(self.context, event) or "default"
        role_name_without_ext = get_role_name_without_ext(role_name)

        raw_data = self.memory_manager.read_plot_framework(user_id, session_id, role_name_without_ext)
        content = ""
        if isinstance(raw_data, dict):
            content = raw_data.get("content", "")
        elif isinstance(raw_data, list) and raw_data:
            content = raw_data[-1].get("content", "")

        if not content or len(content) < 100:
            yield event.plain_result("现有剧情框架内容过少，无需整理。")
            return

        yield event.plain_result(f"正在基于现有内容进行整理浓缩（当前 {len(content)} 字）...\n这可能需要一点时间。")

        persona_obj = await self.context.persona_manager.get_persona(persona_id=role_name)
        persona_content = persona_obj.system_prompt if persona_obj else ""

        try:
            new_content = await self._compress_plot_framework(content, role_name_without_ext, persona_content)

            if new_content and len(new_content) < len(content):
                self.memory_manager.write_plot_framework(user_id, session_id, role_name_without_ext, new_content)
                yield event.plain_result(f"✅ 整理完成！\n原始字数：{len(content)}\n当前字数：{len(new_content)}\n\n剧情框架已更新。")
            else:
                yield event.plain_result("整理完成，但内容未明显缩减，保持原样。")

        except Exception as e:
            logger.error(f"手动整理剧情框架失败: {e}", exc_info=True)
            yield event.plain_result(f"整理失败: {e}")

    @filter.command("总结剧情框架")
    async def regenerate_plot_framework_command(self, event: AstrMessageEvent):
        """读取完整对话日志，从头生成全新的剧情框架"""
        user_id = str(event.get_sender_id())
        session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        role_name = await get_persona_id(self.context, event) or "default"
        role_name_without_ext = get_role_name_without_ext(role_name)

        full_log_content = self.memory_manager.read_full_log(user_id, session_id, role_name_without_ext)
        if not full_log_content or len(full_log_content.strip()) < 100:
            await event.send(event.plain_result("完整对话记录为空或过短，无法生成剧情框架。请先进行一些角色扮演对话。"))
            return

        with active_plot_tasks_lock:
            if user_id in active_plot_tasks:
                await event.send(event.plain_result("正在进行剧情框架生成，请勿重复命令"))
                return
            active_plot_tasks.add(user_id)

        total_chars = len(full_log_content)
        chunk_size = 100000
        total_chunks = (total_chars + chunk_size - 1) // chunk_size

        if total_chunks > 1:
            estimated_time = total_chunks * 1.5 + 2
            await event.send(event.plain_result(f"完整日志共 {total_chars} 字（约{total_chars//10000}万字），需要分 {total_chunks} 次总结，预计需要 {estimated_time:.0f} 分钟，请耐心等待..."))
        else:
            await event.send(event.plain_result(f"完整日志共 {total_chars} 字，开始生成剧情框架..."))

        asyncio.create_task(self._regenerate_plot_framework_threaded(user_id, role_name, session_id, event.unified_msg_origin, event))

    async def _regenerate_plot_framework_threaded(self, user_id, role_name, session_id, unified_msg_origin, event: AstrMessageEvent = None):
        success = False
        try:
            success = await self._regenerate_plot_framework(user_id, role_name, session_id, unified_msg_origin, event)
        except Exception as e:
            logger.error(f"重新生成剧情框架为用户 {user_id} 执行时出错: {e}", exc_info=True)
        finally:
            with active_plot_tasks_lock:
                if user_id in active_plot_tasks:
                    active_plot_tasks.remove(user_id)

            if not success:
                try:
                    if event:
                        await event.send(event.plain_result("⚠️ 剧情框架生成任务失败，可使用【总结剧情框架】命令手动重试。"))
                    else:
                        await self.context.send_message(unified_msg_origin, [Comp.Plain("⚠️ 剧情框架生成任务失败，可使用【总结剧情框架】命令手动重试。")])
                except Exception as e:
                    logger.warning(f"发送剧情框架生成失败通知时出错: {e}")

    async def _regenerate_plot_framework(self, user_id, role_name, session_id, unified_msg_origin, event: AstrMessageEvent = None):
        _ledger_set(user_id, unified_msg_origin, "memory_manual")  # ledger patch
        role_name_without_ext = get_role_name_without_ext(role_name)

        try:
            full_log_content = self.memory_manager.read_full_log(user_id, session_id, role_name_without_ext)

            if not full_log_content or len(full_log_content.strip()) < 100:
                return False

            persona_object = await self.context.persona_manager.get_persona(persona_id=role_name)
            persona_content = persona_object.system_prompt if persona_object else ""

            chunk_size = 100000
            log_lines = full_log_content.split('\n')
            chunks = []
            current_chunk_lines = []
            current_chunk_size = 0

            for line in log_lines:
                line_size = len(line) + 1

                if current_chunk_size >= chunk_size and line.startswith("User:"):
                    if current_chunk_lines:
                        chunks.append('\n'.join(current_chunk_lines))
                    current_chunk_lines = [line]
                    current_chunk_size = line_size
                else:
                    current_chunk_lines.append(line)
                    current_chunk_size += line_size

            if current_chunk_lines:
                chunks.append('\n'.join(current_chunk_lines))

            total_chunks = len(chunks)
            chunk_summaries = []

            for idx, chunk in enumerate(chunks):
                chunk_num = idx + 1

                if total_chunks > 1:
                    try:
                        await self.context.send_message(
                            unified_msg_origin,
                            [Comp.Plain(f"📝 正在总结第 {chunk_num}/{total_chunks} 块...")]
                        )
                    except Exception:
                        pass

                chunk_prompt = f"""
你是一位专业的故事编剧。你的任务是阅读一段对话记录，并从中提炼出剧情要点。

### 对话记录片段（第{chunk_num}部分，共{total_chunks}部分）:
{chunk}
---

请根据以上对话记录片段，以 {role_name_without_ext} 的第一人称视角，撰写一份剧情总结。

### 写作规范:
1. 使用第一人称（"我"代指{role_name_without_ext}，"你"代指用户）。
2. 保持客观，忠实于对话内容。
3. 禁止使用markdown格式。
4. 必须使用自然流畅的叙事段落。

### 你的角色原型设定:
{persona_content}

请直接开始撰写：
"""

                chunk_response = await self._call_ai_with_fallback(
                    prompt="请总结这段对话记录的剧情要点。",
                    system_prompt=chunk_prompt,
                    task_name=f"剧情框架分块{chunk_num}"
                )

                if chunk_response and len(chunk_response.strip()) > 20:
                    chunk_summaries.append(f"【第{chunk_num}部分】\n{chunk_response.strip()}")
                else:
                    chunk_summaries.append(f"【第{chunk_num}部分】\n（此部分总结失败）")

                if idx < len(chunks) - 1:
                    await asyncio.sleep(90)

            if total_chunks == 1:
                final_content = chunk_summaries[0].replace("【第1部分】\n", "").strip()
            else:
                try:
                    await self.context.send_message(
                        unified_msg_origin,
                        [Comp.Plain(f"✅ 所有 {total_chunks} 块已总结完毕，正在进行最终整合...")]
                    )
                except Exception:
                    pass

                all_summaries_text = "\n\n---\n\n".join(chunk_summaries)

                merge_prompt = f"""
你是一位专业的故事编剧。请将多个分段剧情总结整合成一份完整的剧情框架。

### 各分段剧情总结:
{all_summaries_text}
---

请将以上所有分段总结整合成一份完整的剧情框架。

### 写作规范:
1. 使用第一人称（"我"代指{role_name_without_ext}，"你"代指用户）。
2. 禁止使用markdown格式。
3. 必须使用自然流畅的叙事段落。

请直接开始撰写：
"""

                final_response = await self._call_ai_with_fallback(
                    prompt="请将所有分段总结整合成完整的剧情框架，保留所有重要内容。",
                    system_prompt=merge_prompt,
                    task_name="剧情框架整合"
                )

                if not final_response or len(final_response.strip()) < 50:
                    final_content = "\n\n".join([s.replace(f"【第{i+1}部分】\n", "") for i, s in enumerate(chunk_summaries)])
                else:
                    final_content = final_response.strip()

            self.memory_manager.write_plot_framework(user_id, session_id, role_name_without_ext, final_content)

            try:
                if len(final_content) > 4500:
                    if event:
                        await event.send(event.plain_result(f"✅ 剧情框架已生成完毕！内容较长（共{len(final_content)}字），请使用【获取剧情框架】命令以文件形式获取。"))
                    else:
                        await self.context.send_message(unified_msg_origin, [Comp.Plain(f"✅ 剧情框架已生成完毕！内容较长（共{len(final_content)}字），请使用【获取剧情框架】命令以文件形式获取。")])
                else:
                    result_text = f"✅ 剧情框架已生成完毕！\n\n---\n{final_content}"
                    if event:
                        node = Comp.Node(uin=10000, name="记忆系统", content=[Comp.Plain(result_text)])
                        await event.send(event.chain_result([node]))
                    else:
                        await self.context.send_message(unified_msg_origin, [Comp.Plain(result_text)])
            except Exception as e:
                logger.warning(f"发送剧情框架结果时出错: {e}")

            return True

        except Exception as e:
            logger.error(f"重新生成剧情框架过程中发生未知错误: {e}", exc_info=True)
            return False

    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.web_manager:
            try:
                await self.web_manager.stop()
            except Exception as e:
                logger.error(f"停止Web管理界面时出错: {e}")

        for task_key, task in self.timeout_tasks.items():
            if task and not task.done():
                task.cancel()
        self.timeout_tasks.clear()

        logger.info("核心记忆插件已卸载")

