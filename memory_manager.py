import os
import json
import shutil
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from astrbot.api import logger

# 文件夹名称常量
MEMORY_TEMP_DIR = "memory_temp"
MEMORY_SUMMARIES_DIR = "memory_summaries"
DIARIES_DIR = "diaries"
DIARIES_VECTORS_DIR = "diaries_vectors"
PLOT_FRAMEWORKS_DIR = "plot_frameworks"
PLOT_SUMMARIES_DIR = "plot_summaries"
FULL_LOGS_DIR = "full_logs"
LOCKED_MEMORIES_DIR = "locked_memories"
CORE_MEMORY_HISTORY_DIR = "core_memory_history"
PERSONA_MEMORIES_DIR = "persona_memories"
PERSONA_CONFIG_DIR = "persona_config"


class MemoryManager:
    """
    负责所有记忆文件的读写操作。

    支持两种记忆模式：
    - session: 按会话隔离，每个 用户+会话+人格 组合有独立记忆
    - persona: 按人格共享，同一人格的指定共享空间内所有会话共享对话记录和记忆日志，但核心记忆按用户隔离

    Persona模式支持一个角色创建多个共享空间（如：群A冒险、群B日常）
    """

    def __init__(self, data_dir: str):
        if not data_dir:
            raise ValueError("MemoryManager必须使用有效的数据目录进行初始化")
        self.data_dir = data_dir

        # 初始化时确保所有必需的目录都存在
        self.summaries_path = os.path.join(self.data_dir, MEMORY_SUMMARIES_DIR)
        self.diaries_path = os.path.join(self.data_dir, DIARIES_DIR)
        self.vectors_path = os.path.join(self.data_dir, DIARIES_VECTORS_DIR)
        self.temp_path = os.path.join(self.data_dir, MEMORY_TEMP_DIR)
        self.plot_frameworks_path = os.path.join(self.data_dir, PLOT_FRAMEWORKS_DIR)
        self.plot_summaries_path = os.path.join(self.data_dir, PLOT_SUMMARIES_DIR)
        self.full_logs_path = os.path.join(self.data_dir, FULL_LOGS_DIR)
        self.locked_memories_path = os.path.join(self.data_dir, LOCKED_MEMORIES_DIR)
        self.core_memory_history_path = os.path.join(self.data_dir, CORE_MEMORY_HISTORY_DIR)
        self.offsets_path = os.path.join(self.data_dir, "offsets")
        self.persona_memories_path = os.path.join(self.data_dir, PERSONA_MEMORIES_DIR)
        self.persona_config_path = os.path.join(self.data_dir, PERSONA_CONFIG_DIR)

        for path in [
            self.summaries_path, self.diaries_path, self.vectors_path,
            self.temp_path, self.plot_frameworks_path, self.plot_summaries_path,
            self.full_logs_path, self.locked_memories_path, self.core_memory_history_path,
            self.offsets_path, self.persona_memories_path, self.persona_config_path
        ]:
            os.makedirs(path, exist_ok=True)


    def get_persona_users_by_activity(self, role_name: str, space_name: str, limit: int = 5) -> List[str]:
        """获取某个共享空间下的用户ID列表，按最近活跃时间排序"""
        if not space_name:
            return []

        users_with_time = []
        persona_dir = os.path.join(self.persona_memories_path, f"persona_{role_name}_{space_name}")

        if not os.path.exists(persona_dir):
            return []

        for item in os.listdir(persona_dir):
            if item.startswith("user_") and os.path.isdir(os.path.join(persona_dir, item)):
                user_id = item[5:]  # 去掉 "user_" 前缀
                user_dir = os.path.join(persona_dir, item)

                # 获取该用户目录下文件的最新修改时间
                latest_mtime = 0
                for f in os.listdir(user_dir):
                    fp = os.path.join(user_dir, f)
                    try:
                        mtime = os.path.getmtime(fp)
                        if mtime > latest_mtime:
                            latest_mtime = mtime
                    except:
                        pass

                users_with_time.append((user_id, latest_mtime))

        # 按时间倒序排序
        users_with_time.sort(key=lambda x: x[1], reverse=True)

        # 返回前 limit 个用户ID
        return [u[0] for u in users_with_time[:limit]]



    # ========== 通用文件操作 ==========

    def _read_json_file(self, file_path: str, default: Any = None) -> Any:
        """安全地读取JSON文件。"""
        if not os.path.exists(file_path):
            return default
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"读取JSON文件 {file_path} 失败: {e}")
            return default

    def _write_json_file(self, file_path: str, data: Any):
        """安全地写入JSON文件，使用临时文件和移动操作保证原子性。"""
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        temp_file = f"{file_path}.tmp"
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            shutil.move(temp_file, file_path)
            logger.debug(f"JSON文件已成功写入: {file_path}")
        except Exception as e:
            logger.error(f"写入JSON文件 {file_path} 时发生致命错误: {e}", exc_info=True)
            if os.path.exists(temp_file):
                os.remove(temp_file)

    # ========== 角色模式配置 ==========

    def get_role_config(self, role_name: str) -> Dict:
        """获取指定角色的完整配置"""
        config_file = os.path.join(self.persona_config_path, f"{role_name}_config.json")
        return self._read_json_file(config_file, default={
            "memory_mode": "session",
            "current_space": None,
            "spaces": []
        })

    def save_role_config(self, role_name: str, config: Dict):
        """保存角色配置"""
        config_file = os.path.join(self.persona_config_path, f"{role_name}_config.json")
        config["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_json_file(config_file, config)

    def get_persona_mode(self, role_name: str) -> str:
        """获取指定角色的记忆模式"""
        config = self.get_role_config(role_name)
        return config.get("memory_mode", "session")

    def set_persona_mode(self, role_name: str, mode: str, space_name: str = None):
        """设置指定角色的记忆模式"""
        if mode not in ["session", "persona"]:
            raise ValueError(f"无效的记忆模式: {mode}")

        config = self.get_role_config(role_name)
        config["memory_mode"] = mode

        if mode == "persona" and space_name:
            config["current_space"] = space_name
            if space_name not in config.get("spaces", []):
                config.setdefault("spaces", []).append(space_name)

        self.save_role_config(role_name, config)
        logger.info(f"角色 {role_name} 的记忆模式已设置为: {mode}" + (f", 空间: {space_name}" if space_name else ""))

    def get_current_space(self, role_name: str) -> Optional[str]:
        """获取角色当前使用的共享空间名"""
        config = self.get_role_config(role_name)
        return config.get("current_space")

    def set_current_space(self, role_name: str, space_name: str):
        """设置角色当前使用的共享空间"""
        config = self.get_role_config(role_name)
        if space_name not in config.get("spaces", []):
            raise ValueError(f"共享空间 '{space_name}' 不存在")
        config["current_space"] = space_name
        config["memory_mode"] = "persona"
        self.save_role_config(role_name, config)
        logger.info(f"角色 {role_name} 已切换到共享空间: {space_name}")

    def get_persona_spaces(self, role_name: str) -> List[str]:
        """获取角色的所有共享空间列表"""
        config = self.get_role_config(role_name)
        return config.get("spaces", [])

    def get_persona_users(self, role_name: str, space_name: str = None) -> List[str]:
        """获取某个共享空间下的所有用户ID列表"""
        if space_name is None:
            space_name = self.get_current_space(role_name)
        if not space_name:
            return []

        users = []
        persona_dir = os.path.join(self.persona_memories_path, f"persona_{role_name}_{space_name}")
        if not os.path.exists(persona_dir):
            return users

        for item in os.listdir(persona_dir):
            if item.startswith("user_") and os.path.isdir(os.path.join(persona_dir, item)):
                user_id = item[5:]  # 去掉 "user_" 前缀
                users.append(user_id)
        return users

    # ========== 路径生成方法 ==========

    def _get_session_base_filename(self, user_id: str, session_id: str, role_name: str) -> str:
        """生成Session模式的基础文件名（不带后缀）"""
        return f"{user_id}_{session_id}_{role_name}"

    def _get_persona_shared_dir(self, role_name: str, space_name: str) -> str:
        """获取Persona模式的共享数据目录"""
        shared_dir = os.path.join(self.persona_memories_path, f"persona_{role_name}_{space_name}", "shared")
        os.makedirs(shared_dir, exist_ok=True)
        return shared_dir

    def _get_persona_user_dir(self, role_name: str, space_name: str, user_id: str) -> str:
        """获取Persona模式的用户特定数据目录"""
        user_dir = os.path.join(self.persona_memories_path, f"persona_{role_name}_{space_name}", f"user_{user_id}")
        os.makedirs(user_dir, exist_ok=True)
        return user_dir

    def get_memory_path(self, user_id: str, session_id: str, role_name: str,
                        data_type: str, mode: str = None, space_name: str = None) -> str:
        """
        根据记忆模式和数据类型获取正确的文件路径

        Args:
            user_id: 用户ID
            session_id: 会话ID
            role_name: 角色名（不带扩展名）
            data_type: 数据类型 - core_memory, diary, diary_vectors, plot_framework,
                       plot_summary, full_log, locked_memory, offset
            mode: 记忆模式 - session 或 persona，如果为None则自动获取角色配置
            space_name: 共享空间名（仅persona模式需要）

        Returns:
            文件路径
        """
        if mode is None:
            mode = self.get_persona_mode(role_name)

        if mode == "persona":
            if space_name is None:
                space_name = self.get_current_space(role_name)
            if not space_name:
                # 如果没有设置空间，回退到session模式
                logger.warning(f"角色 {role_name} 是persona模式但没有设置空间，回退到session模式")
                mode = "session"

        if mode == "persona":
            # Persona模式：区分共享数据和用户特定数据
            shared_types = {"full_log", "diary", "diary_vectors", "plot_framework", "plot_summaries", "offset"}
            user_specific_types = {"core_memory", "locked_memory"}

            if data_type in shared_types:
                base_dir = self._get_persona_shared_dir(role_name, space_name)

                # 剧情总结特殊处理：兼容两种文件名
                if data_type == "plot_summaries":
                    old_path = os.path.join(base_dir, "plot_summary.json")
                    new_path = os.path.join(base_dir, "plot_summaries.json")
                    if os.path.exists(old_path):
                        return old_path
                    return new_path

                filename_map = {
                    "full_log": "full_log.txt",
                    "diary": "diary.json",
                    "diary_vectors": "diary_vectors.json",
                    "plot_framework": "plot_framework.json",
                    "offset": "offset.json",
                }
                return os.path.join(base_dir, filename_map[data_type])


            elif data_type in user_specific_types:
                base_dir = self._get_persona_user_dir(role_name, space_name, user_id)
                filename_map = {
                    "core_memory": "core_memory.json",
                    "locked_memory": "locked_memory.json",
                }
                return os.path.join(base_dir, filename_map[data_type])
            else:
                raise ValueError(f"未知的数据类型: {data_type}")
        else:
            # Session模式：原有逻辑
            base_name = self._get_session_base_filename(user_id, session_id, role_name)
            path_map = {
                "core_memory": (self.summaries_path, f"{base_name}.json"),
                "diary": (self.diaries_path, f"{base_name}_diary.json"),
                "diary_vectors": (self.vectors_path, f"{base_name}_diary_vectors.json"),
                "plot_framework": (self.plot_frameworks_path, f"{base_name}_plot.json"),
                "plot_summaries": (self.plot_summaries_path, f"{base_name}_plot_summary.json"),
                "full_log": (self.full_logs_path, f"{base_name}_full.txt"),
                "locked_memory": (self.locked_memories_path, f"{base_name}_locked.json"),
                "offset": (self.offsets_path, f"{base_name}_offset.json"),
            }



            base_path, filename = path_map.get(data_type, (self.data_dir, f"{base_name}.txt"))
            return os.path.join(base_path, filename)

    # ========== 兼容旧API的路径方法 ==========

    def get_core_memory_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return self.get_memory_path(user_id, session_id, role_name, "core_memory")

    def get_diary_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return self.get_memory_path(user_id, session_id, role_name, "diary")

    def get_diary_vectors_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return self.get_memory_path(user_id, session_id, role_name, "diary_vectors")

    def get_plot_framework_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return self.get_memory_path(user_id, session_id, role_name, "plot_framework")

    def get_plot_summary_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return self.get_memory_path(user_id, session_id, role_name, "plot_summaries")  


    def get_full_log_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return self.get_memory_path(user_id, session_id, role_name, "full_log")

    def get_locked_memory_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return self.get_memory_path(user_id, session_id, role_name, "locked_memory")

    def get_summary_offset_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return self.get_memory_path(user_id, session_id, role_name, "offset")

    def get_temp_log_path(self, user_id: str, session_id: str, role_name: str) -> str:
        return os.path.join(self.temp_path, f'{user_id}_{session_id}_{role_name}_log.txt')

    def get_core_memory_history_dir(self, user_id: str, session_id: str, role_name: str) -> str:
        """获取核心记忆历史版本的目录路径"""
        mode = self.get_persona_mode(role_name)
        if mode == "persona":
            space_name = self.get_current_space(role_name)
            if space_name:
                history_dir = os.path.join(self._get_persona_user_dir(role_name, space_name, user_id), "core_memory_history")
            else:
                base_name = self._get_session_base_filename(user_id, session_id, role_name)
                history_dir = os.path.join(self.core_memory_history_path, base_name)
        else:
            base_name = self._get_session_base_filename(user_id, session_id, role_name)
            history_dir = os.path.join(self.core_memory_history_path, base_name)
        os.makedirs(history_dir, exist_ok=True)
        return history_dir

    # ========== 按轮数记录的总结进度 ==========

    def read_summary_round(self, user_id: str, session_id: str, role_name: str) -> int:
        """读取上次总结时的轮数，如果文件不存在则自动初始化"""
        file_path = self.get_summary_offset_path(user_id, session_id, role_name)

        if not os.path.exists(file_path):
            current_lines = self.get_full_log_line_count(user_id, session_id, role_name)
            current_rounds = current_lines // 2
            if current_rounds > 0:
                self.write_summary_round(user_id, session_id, role_name, current_rounds)
                logger.info(f"首次使用，已总结轮数初始化为 {current_rounds}（跳过历史对话）")
                return current_rounds
            return 0

        data = self._read_json_file(file_path, default={"round": 0})
        if isinstance(data, dict):
            return data.get("round", data.get("offset", 0) // 2)
        return 0

    def write_summary_round(self, user_id: str, session_id: str, role_name: str, round_num: int):
        """保存已总结的轮数"""
        file_path = self.get_summary_offset_path(user_id, session_id, role_name)
        self._write_json_file(file_path, {
            "round": round_num,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        logger.info(f"已总结轮数更新至 {round_num}")

    def get_lines_for_summary(self, user_id: str, session_id: str, role_name: str, trigger_rounds: int) -> tuple:
        """获取需要总结的对话行（按轮数计算）"""
        all_lines = self.read_full_log_lines(user_id, session_id, role_name)
        total_lines = len(all_lines)
        total_rounds = total_lines // 2

        last_summarized_round = self.read_summary_round(user_id, session_id, role_name)

        start_line = last_summarized_round * 2
        end_line = total_lines

        new_rounds = total_rounds - last_summarized_round

        logger.info(f"对话记录共 {total_rounds} 轮，上次总结到 {last_summarized_round} 轮，新增 {new_rounds} 轮")

        lines_to_summarize = all_lines[start_line:end_line]

        return lines_to_summarize, total_rounds

    # ========== 核心记忆操作 ==========

    def read_core_memory(self, user_id: str, session_id: str, role_name: str) -> Optional[Dict]:
        file_path = self.get_core_memory_path(user_id, session_id, role_name)
        return self._read_json_file(file_path, default={})

    def write_core_memory(self, user_id: str, session_id: str, role_name: str, content: str):
        """写入核心记忆，并在写入前备份旧版本到历史目录"""
        file_path = self.get_core_memory_path(user_id, session_id, role_name)

        if os.path.exists(file_path):
            try:
                old_data = self._read_json_file(file_path)
                if old_data and old_data.get("content"):
                    history_dir = self.get_core_memory_history_dir(user_id, session_id, role_name)
                    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    history_filename = f"{timestamp_str}.json"
                    history_file_path = os.path.join(history_dir, history_filename)
                    self._write_json_file(history_file_path, old_data)
                    logger.info(f"核心记忆历史版本已备份: {history_file_path}")
            except Exception as e:
                logger.warning(f"备份核心记忆历史版本时出错: {e}")

        data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content": content
        }
        self._write_json_file(file_path, data)

    def read_core_memory_history(self, user_id: str, session_id: str, role_name: str) -> List[Dict]:
        """读取核心记忆的所有历史版本，按时间倒序排列"""
        history_dir = self.get_core_memory_history_dir(user_id, session_id, role_name)
        history_list = []

        if not os.path.exists(history_dir):
            return history_list

        try:
            files = sorted(os.listdir(history_dir), reverse=True)
            for filename in files:
                if filename.endswith('.json'):
                    file_path = os.path.join(history_dir, filename)
                    data = self._read_json_file(file_path)
                    if data:
                        data['history_filename'] = filename
                        history_list.append(data)
        except Exception as e:
            logger.error(f"读取核心记忆历史版本时出错: {e}")

        return history_list

    def cleanup_core_memory_history(self, user_id: str, session_id: str, role_name: str, max_count: int):
        """清理核心记忆历史版本，只保留最近的max_count份"""
        if max_count <= 0:
            return 0

        history_dir = self.get_core_memory_history_dir(user_id, session_id, role_name)

        if not os.path.exists(history_dir):
            return 0

        try:
            files = sorted(os.listdir(history_dir), reverse=True)
            json_files = [f for f in files if f.endswith('.json')]

            if len(json_files) <= max_count:
                return 0

            files_to_delete = json_files[max_count:]
            deleted_count = 0

            for filename in files_to_delete:
                file_path = os.path.join(history_dir, filename)
                try:
                    os.remove(file_path)
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"删除历史版本文件失败 {file_path}: {e}")

            logger.info(f"已清理 {deleted_count} 份核心记忆历史版本")
            return deleted_count
        except Exception as e:
            logger.error(f"清理核心记忆历史版本时出错: {e}")
            return 0

    # ========== 锁定记忆操作 ==========

    def read_locked_memories(self, user_id: str, session_id: str, role_name: str) -> List[Dict]:
        """读取锁定记忆列表"""
        file_path = self.get_locked_memory_path(user_id, session_id, role_name)
        data = self._read_json_file(file_path, default={"locked_items": []})
        if isinstance(data, dict) and "locked_items" in data:
            return data["locked_items"]
        return []

    def write_locked_memories(self, user_id: str, session_id: str, role_name: str, locked_items: List[Dict]):
        """写入锁定记忆列表"""
        file_path = self.get_locked_memory_path(user_id, session_id, role_name)
        data = {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "locked_items": locked_items
        }
        self._write_json_file(file_path, data)

    def add_locked_memory(self, user_id: str, session_id: str, role_name: str, content: str) -> bool:
        """添加一条锁定记忆"""
        locked_items = self.read_locked_memories(user_id, session_id, role_name)

        for item in locked_items:
            if item.get("content") == content:
                return False

        new_item = {
            "id": len(locked_items) + 1,
            "content": content,
            "source": "user",
            "locked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        locked_items.append(new_item)
        self.write_locked_memories(user_id, session_id, role_name, locked_items)
        return True

    def remove_locked_memory(self, user_id: str, session_id: str, role_name: str, item_ids: List[int]) -> bool:
        """根据ID列表移除多条锁定记忆"""
        locked_items = self.read_locked_memories(user_id, session_id, role_name)
        original_count = len(locked_items)
        ids_to_remove = set(item_ids)

        filtered_items = [item for item in locked_items if item.get("id") not in ids_to_remove]

        if len(filtered_items) == original_count:
            return False

        for idx, item in enumerate(filtered_items):
            item["id"] = idx + 1

        self.write_locked_memories(user_id, session_id, role_name, filtered_items)
        return True

    def get_locked_memories_text(self, user_id: str, session_id: str, role_name: str) -> str:
        """获取锁定记忆的纯文本格式，用于注入prompt"""
        locked_items = self.read_locked_memories(user_id, session_id, role_name)
        if not locked_items:
            return ""

        lines = []
        for item in locked_items:
            lines.append(f"- {item.get('content', '')}")
        return "\n".join(lines)

    def clear_locked_memories(self, user_id: str, session_id: str, role_name: str):
        """清空所有锁定记忆"""
        self.write_locked_memories(user_id, session_id, role_name, [])

    # ========== 记忆日志操作 ==========

    def read_diaries(self, user_id: str, session_id: str, role_name: str) -> List[Dict]:
        file_path = self.get_diary_path(user_id, session_id, role_name)
        diaries = self._read_json_file(file_path, default=[])
        return diaries if isinstance(diaries, list) else []

    def append_to_diary(self, user_id: str, session_id: str, role_name: str, summary: str,
                        user_label: str = None):
        """追加一条记忆日志"""
        file_path = self.get_diary_path(user_id, session_id, role_name)
        diaries = self.read_diaries(user_id, session_id, role_name)
        new_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary
        }
        if user_label:
            new_entry["user_label"] = user_label
        diaries.append(new_entry)
        self._write_json_file(file_path, diaries)
        logger.info(f"新记忆已保存至: {file_path}")

    def write_diaries(self, user_id: str, session_id: str, role_name: str, diaries: List[Dict]):
        file_path = self.get_diary_path(user_id, session_id, role_name)
        self._write_json_file(file_path, diaries)

    def cleanup_diaries(self, user_id: str, session_id: str, role_name: str, max_count: int) -> int:
        """清理记忆日志，只保留最近的max_count条"""
        if max_count <= 0:
            return 0

        diaries = self.read_diaries(user_id, session_id, role_name)

        if len(diaries) <= max_count:
            return 0

        deleted_count = len(diaries) - max_count
        new_diaries = diaries[-max_count:]
        self.write_diaries(user_id, session_id, role_name, new_diaries)

        vectors = self.read_diary_vectors(user_id, session_id, role_name)
        if len(vectors) > max_count:
            new_vectors = vectors[-max_count:]
            self.write_diary_vectors(user_id, session_id, role_name, new_vectors)

        logger.info(f"已清理 {deleted_count} 条记忆日志")
        return deleted_count

    # ========== 记忆向量操作 ==========

    def read_diary_vectors(self, user_id: str, session_id: str, role_name: str) -> List[List[float]]:
        file_path = self.get_diary_vectors_path(user_id, session_id, role_name)
        vectors = self._read_json_file(file_path, default=[])
        return vectors if isinstance(vectors, list) else []

    def write_diary_vectors(self, user_id: str, session_id: str, role_name: str, vectors: List[List[float]]):
        file_path = self.get_diary_vectors_path(user_id, session_id, role_name)
        self._write_json_file(file_path, vectors)

    def append_to_diary_vectors(self, user_id: str, session_id: str, role_name: str, vector: List[float]):
        file_path = self.get_diary_vectors_path(user_id, session_id, role_name)
        vectors = self.read_diary_vectors(user_id, session_id, role_name)
        vectors.append(vector)
        self._write_json_file(file_path, vectors)
        logger.info(f"新记忆的向量已保存至: {file_path}")

    # ========== 剧情框架操作 ==========

    def read_plot_framework(self, user_id: str, session_id: str, role_name: str):
        """读取剧情框架"""
        file_path = self.get_plot_framework_path(user_id, session_id, role_name)
        if not os.path.exists(file_path):
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if isinstance(data, list) and data:
                return data[-1]
            elif isinstance(data, dict):
                return data
            else:
                return None

        except Exception as e:
            logger.error(f"读取剧情框架失败: {e}")
            return None

    def write_plot_framework(self, user_id: str, session_id: str, role_name: str, content: str):
        """写入剧情框架（覆盖更新）"""
        file_path = self.get_plot_framework_path(user_id, session_id, role_name)
        data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content": content
        }
        self._write_json_file(file_path, data)

    def append_to_plot_framework(self, user_id: str, session_id: str, role_name: str, new_content: str):
        """累积式追加剧情框架内容"""
        raw_data = self.read_plot_framework(user_id, session_id, role_name)
        existing_content = ""

        if isinstance(raw_data, list) and raw_data:
            existing_content = raw_data[-1].get("content", "")
        elif isinstance(raw_data, dict):
            existing_content = raw_data.get("content", "")

        if existing_content:
            combined_content = existing_content.rstrip() + "\n\n" + new_content.strip()
        else:
            combined_content = new_content.strip()

        self.write_plot_framework(user_id, session_id, role_name, combined_content)

    # ========== 剧情总结操作 ==========

    def read_plot_summaries(self, user_id: str, session_id: str, role_name: str) -> List[Dict]:
        """读取剧情总结列表。"""
        file_path = self.get_plot_summary_path(user_id, session_id, role_name)
        summaries = self._read_json_file(file_path, default=[])
        return summaries if isinstance(summaries, list) else []

    def append_to_plot_summaries(self, user_id: str, session_id: str, role_name: str,
                                  summary_content: str, summarized_line_count: int = 0):
        """向剧情总结文件中追加一条新的总结记录"""
        file_path = self.get_plot_summary_path(user_id, session_id, role_name)
        summaries = self.read_plot_summaries(user_id, session_id, role_name)
        new_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary_content,
            "summarized_up_to_line": summarized_line_count
        }
        summaries.append(new_entry)
        self._write_json_file(file_path, summaries)
        logger.info(f"新剧情总结已保存至: {file_path}，已总结至第{summarized_line_count}行")

    def write_plot_summaries(self, user_id: str, session_id: str, role_name: str, summaries: List[Dict]):
        """直接覆写整个剧情总结文件。"""
        file_path = self.get_plot_summary_path(user_id, session_id, role_name)
        self._write_json_file(file_path, summaries)

    def get_latest_plot_summaries(self, user_id: str, session_id: str, role_name: str, count: int) -> List[Dict]:
        """获取最新的N条剧情总结"""
        all_summaries = self.read_plot_summaries(user_id, session_id, role_name)
        if count <= 0 or not all_summaries:
            return []
        return all_summaries[-count:]

    def cleanup_plot_summaries(self, user_id: str, session_id: str, role_name: str, max_count: int) -> int:
        """清理剧情总结，只保留最近的max_count条"""
        if max_count <= 0:
            return 0

        summaries = self.read_plot_summaries(user_id, session_id, role_name)

        if len(summaries) <= max_count:
            return 0

        deleted_count = len(summaries) - max_count
        new_summaries = summaries[-max_count:]
        self.write_plot_summaries(user_id, session_id, role_name, new_summaries)

        logger.info(f"已清理 {deleted_count} 条剧情总结")
        return deleted_count

    # ========== 完整对话记录操作 ==========

    def read_full_log(self, user_id: str, session_id: str, role_name: str) -> str:
        file_path = self.get_full_log_path(user_id, session_id, role_name)
        if not os.path.exists(file_path):
            return ""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except IOError as e:
            logger.error(f"读取完整对话记录文件 {file_path} 失败: {e}")
            return ""

    def read_full_log_lines(self, user_id: str, session_id: str, role_name: str) -> List[str]:
        file_path = self.get_full_log_path(user_id, session_id, role_name)
        if not os.path.exists(file_path):
            return []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except IOError as e:
            logger.error(f"读取完整对话记录文件 {file_path} 失败: {e}")
            return []

    def append_to_full_log(self, user_id: str, session_id: str, role_name: str,
                           user_msg: str, assistant_msg: str,
                           user_label: str = "User", session_label: str = None):
        """
        追加对话到完整记录

        Args:
            user_label: 用户标识，如 "User[123456:小明]"
            session_label: 会话来源标识，如 "Group:789456" 或 "Private:123456"
        """
        file_path = self.get_full_log_path(user_id, session_id, role_name)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'a', encoding='utf-8') as f:
                if session_label:
                    f.write(f"[{timestamp}] [{session_label}] {user_label}: {user_msg}\n")
                else:
                    f.write(f"[{timestamp}] {user_label}: {user_msg}\n")
                f.write(f"[{timestamp}] Assistant: {assistant_msg}\n")
            logger.debug(f"对话已追加到完整记录: {file_path}")
        except IOError as e:
            logger.error(f"写入完整对话记录文件 {file_path} 失败: {e}")

    def clear_full_log(self, user_id: str, session_id: str, role_name: str):
        file_path = self.get_full_log_path(user_id, session_id, role_name)
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.truncate(0)
            logger.info(f"完整对话记录已清空: {file_path}")
        except IOError as e:
            logger.error(f"清空完整对话记录文件 {file_path} 失败: {e}")

    def get_full_log_line_count(self, user_id: str, session_id: str, role_name: str) -> int:
        lines = self.read_full_log_lines(user_id, session_id, role_name)
        return len(lines)

    # ========== 记忆迁移 ==========

    def migrate_session_to_persona(self, user_id: str, session_id: str, role_name: str,
                                    space_name: str) -> Dict[str, Any]:
        """
        将Session模式的记忆迁移到Persona模式的指定空间

        迁移策略：
        - 如果是新建空间：迁移所有数据
        - 如果是加入已有空间：只迁移用户特定数据（核心记忆、锁定记忆）
        """
        result = {
            "success": True,
            "actions": [],
            "warnings": [],
            "errors": []
        }

        if not space_name:
            result["success"] = False
            result["errors"].append("必须指定共享空间名称")
            return result

        # 获取源路径（强制使用session模式）
        def get_session_path(data_type):
            return self.get_memory_path(user_id, session_id, role_name, data_type, mode="session")

        # 获取目标路径（强制使用persona模式）
        def get_persona_path(data_type):
            return self.get_memory_path(user_id, session_id, role_name, data_type,
                                        mode="persona", space_name=space_name)

        # 检查空间是否已存在
        all_spaces = self.get_persona_spaces(role_name)
        is_new_space = space_name not in all_spaces

        if is_new_space:
            result["actions"].append(f"创建新共享空间 '{space_name}'")

            # === 新建空间：迁移共享数据 ===

            # 1. 迁移对话记录
            try:
                source_log = get_session_path("full_log")
                target_log = get_persona_path("full_log")

                if os.path.exists(source_log):
                    with open(source_log, 'r', encoding='utf-8') as f:
                        source_content = f.read()

                    if source_content.strip():
                        os.makedirs(os.path.dirname(target_log), exist_ok=True)
                        with open(target_log, 'w', encoding='utf-8') as f:
                            f.write(source_content)
                        result["actions"].append(f"对话记录已迁移（{len(source_content)}字符）")
            except Exception as e:
                result["errors"].append(f"迁移对话记录失败: {e}")

            # 2. 迁移记忆日志
            try:
                source_diary_path = get_session_path("diary")
                source_diary = self._read_json_file(source_diary_path, default=[])

                if source_diary:
                    target_diary_path = get_persona_path("diary")
                    for entry in source_diary:
                        entry["migrated_from"] = f"user_{user_id}_session_{session_id}"
                        entry["migrated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._write_json_file(target_diary_path, source_diary)
                    result["actions"].append(f"已迁移 {len(source_diary)} 条记忆日志")

                    # 同时迁移向量
                    source_vectors_path = get_session_path("diary_vectors")
                    source_vectors = self._read_json_file(source_vectors_path, default=[])
                    if source_vectors:
                        target_vectors_path = get_persona_path("diary_vectors")
                        self._write_json_file(target_vectors_path, source_vectors)
                        result["actions"].append(f"已迁移 {len(source_vectors)} 条记忆向量")
            except Exception as e:
                result["errors"].append(f"迁移记忆日志失败: {e}")

            # 3. 迁移剧情框架
            try:
                source_framework_path = get_session_path("plot_framework")
                source_framework = self._read_json_file(source_framework_path, default={})

                if source_framework and source_framework.get("content"):
                    target_framework_path = get_persona_path("plot_framework")
                    source_framework["migrated_from"] = f"session_{session_id}"
                    self._write_json_file(target_framework_path, source_framework)
                    result["actions"].append("剧情框架已迁移")
            except Exception as e:
                result["errors"].append(f"迁移剧情框架失败: {e}")

            # 4. 迁移剧情总结
            try:
                source_summaries_path = get_session_path("plot_summaries")  # 改这里
                source_summaries = self._read_json_file(source_summaries_path, default=[])

                if source_summaries:
                    target_summaries_path = get_persona_path("plot_summaries")  # 改这里
                    for entry in source_summaries:
                        entry["migrated_from"] = f"user_{user_id}_session_{session_id}"
                        entry["migrated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._write_json_file(target_summaries_path, source_summaries)
                    result["actions"].append(f"已迁移 {len(source_summaries)} 条剧情总结")
            except Exception as e:
                result["errors"].append(f"迁移剧情总结失败: {e}")


            # 5. 迁移offset
            try:
                source_offset_path = get_session_path("offset")
                source_offset = self._read_json_file(source_offset_path, default={})
                if source_offset:
                    target_offset_path = get_persona_path("offset")
                    self._write_json_file(target_offset_path, source_offset)
            except Exception as e:
                pass  # offset迁移失败不是大问题

        else:
            # === 加入已有空间：不迁移共享数据 ===
            result["actions"].append(f"加入已有共享空间 '{space_name}'")
            result["warnings"].append("共享数据（对话记录、记忆日志、剧情框架、剧情总结）不会迁移，避免数据重复混乱")

        # === 无论新建还是加入，都迁移用户特定数据 ===

        # 6. 迁移核心记忆（到用户特定位置）
        try:
            source_core_path = get_session_path("core_memory")
            source_core = self._read_json_file(source_core_path, default={})

            if source_core and source_core.get("content"):
                target_core_path = get_persona_path("core_memory")

                if os.path.exists(target_core_path):
                    existing = self._read_json_file(target_core_path, default={})
                    if existing.get("content"):
                        result["warnings"].append("用户核心记忆已存在，将被覆盖")

                source_core["migrated_from"] = f"session_{session_id}"
                source_core["migrated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._write_json_file(target_core_path, source_core)
                result["actions"].append("核心记忆已迁移到用户特定位置")
        except Exception as e:
            result["errors"].append(f"迁移核心记忆失败: {e}")

        # 7. 迁移锁定记忆（到用户特定位置，去重合并）
        try:
            source_locked_path = get_session_path("locked_memory")
            source_locked_data = self._read_json_file(source_locked_path, default={"locked_items": []})

            # 兼容两种格式：{"locked_items": [...]} 或直接 [...]
            if isinstance(source_locked_data, dict) and "locked_items" in source_locked_data:
                source_locked = source_locked_data.get("locked_items", [])
            elif isinstance(source_locked_data, list):
                source_locked = source_locked_data
            else:
                source_locked = []

            if source_locked:
                target_locked_path = get_persona_path("locked_memory")
                existing_locked_data = self._read_json_file(target_locked_path, default={"locked_items": []})

                # 同样兼容两种格式
                if isinstance(existing_locked_data, dict) and "locked_items" in existing_locked_data:
                    existing_items = existing_locked_data.get("locked_items", [])
                elif isinstance(existing_locked_data, list):
                    existing_items = existing_locked_data
                else:
                    existing_items = []

                existing_contents = {item.get("content") for item in existing_items}
                added_count = 0
                for item in source_locked:
                    if item.get("content") not in existing_contents:
                        item["id"] = len(existing_items) + 1
                        item["migrated_from"] = f"session_{session_id}"
                        item["migrated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        existing_items.append(item)
                        added_count += 1

                # 保存为标准格式
                save_data = {
                    "locked_items": existing_items,
                    "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                self._write_json_file(target_locked_path, save_data)
                result["actions"].append(f"已合并 {added_count} 条锁定记忆（跳过 {len(source_locked) - added_count} 条重复）")
        except Exception as e:
            result["errors"].append(f"迁移锁定记忆失败: {e}")

        # 设置角色为Persona模式并指定空间
        if not result["errors"]:
            self.set_persona_mode(role_name, "persona", space_name)
            result["actions"].append(f"角色 {role_name} 已切换为Persona模式，空间: {space_name}")

        result["success"] = len(result["errors"]) == 0
        return result


    def create_empty_persona(self, role_name: str, space_name: str) -> Dict[str, Any]:
        """创建一个空的Persona模式记忆空间"""
        result = {
            "success": True,
            "message": "",
            "errors": []
        }

        if not space_name:
            result["success"] = False
            result["message"] = "必须指定共享空间名称"
            return result

        try:
            # 检查是否已存在
            persona_dir = os.path.join(self.persona_memories_path, f"persona_{role_name}_{space_name}")
            if os.path.exists(persona_dir):
                result["success"] = False
                result["message"] = f"共享空间 '{space_name}' 已存在"
                return result

            # 创建目录结构
            shared_dir = self._get_persona_shared_dir(role_name, space_name)

            # 创建空的共享文件
            empty_files = {
                "full_log.txt": "",
                "diary.json": [],
                "diary_vectors.json": [],
                "plot_framework.json": {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "content": ""},
                "plot_summary.json": [],
                "offset.json": {"round": 0, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            }

            for filename, content in empty_files.items():
                file_path = os.path.join(shared_dir, filename)
                if filename.endswith('.json'):
                    self._write_json_file(file_path, content)
                else:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)

            # 设置模式和空间
            self.set_persona_mode(role_name, "persona", space_name)

            result["message"] = f"已创建共享空间 '{space_name}'"
            logger.info(f"角色 {role_name} 的共享空间 '{space_name}' 已创建")

        except Exception as e:
            result["success"] = False
            result["errors"].append(str(e))
            logger.error(f"创建Persona记忆失败: {e}", exc_info=True)

        return result

    # ========== 存储统计 ==========

    def get_storage_stats(self, user_id: str = None, session_id: str = None,
                          role_name: str = None) -> Dict:
        """获取存储统计信息"""
        stats = {
            "total_size": 0,
            "core_memory": {"size": 0, "count": 0},
            "core_memory_history": {"size": 0, "count": 0},
            "diaries": {"size": 0, "count": 0},
            "diary_vectors": {"size": 0, "count": 0},
            "plot_frameworks": {"size": 0, "count": 0},
            "plot_summaries": {"size": 0, "count": 0},
            "full_logs": {"size": 0, "count": 0},
            "locked_memories": {"size": 0, "count": 0},
        }

        def get_dir_stats(dir_path: str) -> tuple:
            """获取目录的总大小和文件数"""
            total_size = 0
            file_count = 0
            if os.path.exists(dir_path):
                for root, dirs, files in os.walk(dir_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            total_size += os.path.getsize(fp)
                            file_count += 1
                        except OSError:
                            pass
            return total_size, file_count

        if user_id and session_id and role_name:
            # 核心记忆
            core_path = self.get_core_memory_path(user_id, session_id, role_name)
            if os.path.exists(core_path):
                stats["core_memory"]["size"] = os.path.getsize(core_path)
                stats["core_memory"]["count"] = 1

            # 核心记忆历史
            history_dir = self.get_core_memory_history_dir(user_id, session_id, role_name)
            size, count = get_dir_stats(history_dir)
            stats["core_memory_history"]["size"] = size
            stats["core_memory_history"]["count"] = count

            # 记忆日志
            diary_path = self.get_diary_path(user_id, session_id, role_name)
            if os.path.exists(diary_path):
                stats["diaries"]["size"] = os.path.getsize(diary_path)
                diaries = self.read_diaries(user_id, session_id, role_name)
                stats["diaries"]["count"] = len(diaries)

            # 记忆向量
            vectors_path = self.get_diary_vectors_path(user_id, session_id, role_name)
            if os.path.exists(vectors_path):
                stats["diary_vectors"]["size"] = os.path.getsize(vectors_path)
                vectors = self.read_diary_vectors(user_id, session_id, role_name)
                stats["diary_vectors"]["count"] = len(vectors)

            # 剧情框架
            framework_path = self.get_plot_framework_path(user_id, session_id, role_name)
            if os.path.exists(framework_path):
                stats["plot_frameworks"]["size"] = os.path.getsize(framework_path)
                stats["plot_frameworks"]["count"] = 1

            # 剧情总结
            summary_path = self.get_plot_summary_path(user_id, session_id, role_name)
            if os.path.exists(summary_path):
                stats["plot_summaries"]["size"] = os.path.getsize(summary_path)
                summaries = self.read_plot_summaries(user_id, session_id, role_name)
                stats["plot_summaries"]["count"] = len(summaries)

            # 完整对话
            log_path = self.get_full_log_path(user_id, session_id, role_name)
            if os.path.exists(log_path):
                stats["full_logs"]["size"] = os.path.getsize(log_path)
                stats["full_logs"]["count"] = self.get_full_log_line_count(user_id, session_id, role_name) // 2

            # 锁定记忆
            locked_path = self.get_locked_memory_path(user_id, session_id, role_name)
            if os.path.exists(locked_path):
                stats["locked_memories"]["size"] = os.path.getsize(locked_path)
                locked = self.read_locked_memories(user_id, session_id, role_name)
                stats["locked_memories"]["count"] = len(locked)
        else:
            # 统计所有数据
            size, count = get_dir_stats(self.summaries_path)
            stats["core_memory"]["size"] = size
            stats["core_memory"]["count"] = count

            size, count = get_dir_stats(self.core_memory_history_path)
            stats["core_memory_history"]["size"] = size
            stats["core_memory_history"]["count"] = count

            size, count = get_dir_stats(self.diaries_path)
            stats["diaries"]["size"] = size
            stats["diaries"]["count"] = count

            size, count = get_dir_stats(self.vectors_path)
            stats["diary_vectors"]["size"] = size
            stats["diary_vectors"]["count"] = count

            size, count = get_dir_stats(self.plot_frameworks_path)
            stats["plot_frameworks"]["size"] = size
            stats["plot_frameworks"]["count"] = count

            size, count = get_dir_stats(self.plot_summaries_path)
            stats["plot_summaries"]["size"] = size
            stats["plot_summaries"]["count"] = count

            size, count = get_dir_stats(self.full_logs_path)
            stats["full_logs"]["size"] = size
            stats["full_logs"]["count"] = count

            size, count = get_dir_stats(self.locked_memories_path)
            stats["locked_memories"]["size"] = size
            stats["locked_memories"]["count"] = count

            # 加上Persona模式的数据
            size, count = get_dir_stats(self.persona_memories_path)
            stats["total_size"] += size

        # 计算总大小
        for key in stats:
            if key != "total_size" and isinstance(stats[key], dict):
                stats["total_size"] += stats[key]["size"]

        return stats

    def get_all_memory_sessions(self) -> List[Dict]:
        """获取所有记忆会话列表（Session模式），按最后更新时间排序"""
        sessions = []
        seen = set()

        def get_latest_mtime(user_id, session_id, role_name):
            """获取该会话所有文件中最新的修改时间"""
            latest = 0
            for data_type in ["core_memory", "diary", "plot_framework", "plot_summary", "full_log", "locked_memory"]:
                try:
                    p = self.get_memory_path(user_id, session_id, role_name, data_type, mode="session")
                    if os.path.exists(p):
                        mtime = os.path.getmtime(p)
                        if mtime > latest:
                            latest = mtime
                except:
                    pass
            return latest

        # 从核心记忆目录扫描
        if os.path.exists(self.summaries_path):
            for filename in os.listdir(self.summaries_path):
                if filename.endswith('.json'):
                    parts = filename[:-5].rsplit('_', 2)
                    if len(parts) >= 3:
                        user_id = parts[0]
                        session_id = parts[1]
                        role_name = parts[2]
                        key = f"{user_id}_{session_id}_{role_name}"
                        if key not in seen:
                            seen.add(key)
                            mtime = get_latest_mtime(user_id, session_id, role_name)
                            sessions.append({
                                "user_id": user_id,
                                "session_id": session_id,
                                "role_name": role_name,
                                "mode": "session",
                                "last_updated": mtime
                            })

        # 从记忆日志目录扫描
        if os.path.exists(self.diaries_path):
            for filename in os.listdir(self.diaries_path):
                if filename.endswith('_diary.json'):
                    base = filename[:-11]
                    parts = base.rsplit('_', 2)
                    if len(parts) >= 3:
                        user_id = parts[0]
                        session_id = parts[1]
                        role_name = parts[2]
                        key = f"{user_id}_{session_id}_{role_name}"
                        if key not in seen:
                            seen.add(key)
                            mtime = get_latest_mtime(user_id, session_id, role_name)
                            sessions.append({
                                "user_id": user_id,
                                "session_id": session_id,
                                "role_name": role_name,
                                "mode": "session",
                                "last_updated": mtime
                            })

        sessions.sort(key=lambda x: x.get('last_updated', 0), reverse=True)
        return sessions

    def get_all_persona_memories(self) -> List[Dict]:
        """获取所有Persona模式的记忆列表"""
        personas = []

        if not os.path.exists(self.persona_memories_path):
            return personas

        for item in os.listdir(self.persona_memories_path):
            if item.startswith("persona_") and os.path.isdir(os.path.join(self.persona_memories_path, item)):
                # 解析 persona_角色名_空间名
                parts = item[8:].rsplit('_', 1)  # 去掉 "persona_" 前缀
                if len(parts) >= 2:
                    role_name = parts[0]
                    space_name = parts[1]
                else:
                    role_name = parts[0]
                    space_name = "default"

                persona_dir = os.path.join(self.persona_memories_path, item)

                # 获取用户列表
                users = self.get_persona_users(role_name, space_name)

                # 获取最后更新时间
                latest_mtime = 0
                shared_dir = os.path.join(persona_dir, "shared")
                if os.path.exists(shared_dir):
                    for f in os.listdir(shared_dir):
                        fp = os.path.join(shared_dir, f)
                        try:
                            mtime = os.path.getmtime(fp)
                            if mtime > latest_mtime:
                                latest_mtime = mtime
                        except:
                            pass

                personas.append({
                    "role_name": role_name,
                    "space_name": space_name,
                    "mode": "persona",
                    "users": users,
                    "user_count": len(users),
                    "last_updated": latest_mtime
                })

        personas.sort(key=lambda x: x.get('last_updated', 0), reverse=True)
        return personas
