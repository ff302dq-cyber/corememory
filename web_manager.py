import os
import json
import asyncio
import uuid
import hashlib
from datetime import datetime
from typing import Optional, Dict, List
from aiohttp import web

from astrbot.api import logger


class WebManager:
    def __init__(self, memory_manager, config, data_dir: str, plugin_instance=None):
        self.memory_manager = memory_manager
        self.config = config
        self.data_dir = data_dir
        self.plugin_instance = plugin_instance
        self.web_runner = None
        self.web_site = None
        self.sessions = {}
        self.running_tasks = {}
        self.user_passwords_path = os.path.join(data_dir, 'user_passwords.json')
        self._ensure_user_passwords_file()

    # ========== 用户密码管理 ==========

    def _ensure_user_passwords_file(self):
        if not os.path.exists(self.user_passwords_path):
            data = {"default_password": "123456", "users": {}}
            os.makedirs(os.path.dirname(self.user_passwords_path), exist_ok=True)
            with open(self.user_passwords_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_user_passwords(self) -> dict:
        try:
            with open(self.user_passwords_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {"default_password": "123456", "users": {}}

    def _save_user_passwords(self, data: dict):
        with open(self.user_passwords_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _hash_password(self, password: str) -> str:
        return hashlib.sha256(password.encode('utf-8')).hexdigest()

    def _verify_user_password(self, qq_number: str, password: str) -> bool:
        pw_data = self._load_user_passwords()
        hashed = self._hash_password(password)
        if qq_number in pw_data.get("users", {}):
            return pw_data["users"][qq_number] == hashed
        return password == pw_data.get("default_password", "123456")

    def _is_default_password(self, qq_number: str) -> bool:
        pw_data = self._load_user_passwords()
        return qq_number not in pw_data.get("users", {})

    def _change_user_password(self, qq_number: str, new_password: str):
        pw_data = self._load_user_passwords()
        if "users" not in pw_data:
            pw_data["users"] = {}
        pw_data["users"][qq_number] = self._hash_password(new_password)
        self._save_user_passwords(pw_data)

    def _get_auth_info(self, request: web.Request) -> Optional[dict]:
        """获取认证信息，返回 None 表示未认证"""
        token = request.headers.get('Authorization', '')
        if token.startswith('Bearer '):
            token = token[7:]
        return self.sessions.get(token)

    def _check_user_permission(self, request: web.Request, user_id: str) -> bool:
        """检查当前登录用户是否有权限访问指定user_id的数据"""
        info = self._get_auth_info(request)
        if not info:
            return False
        if info.get('login_type') == 'admin':
            return True
        return info.get('qq_number') == user_id

    async def start(self):
        # 权限中间件 - 统一拦截用户越权访问
        web_mgr = self  # 闭包引用

        @web.middleware
        async def permission_middleware(request, handler):
            path = request.path

            # 公开端点，不需要认证
            public_paths = ['/', '/favicon.ico', '/api/login', '/api/logout', '/api/check_auth']
            if path in public_paths:
                return await handler(request)

            # 已认证才能继续
            if not web_mgr._check_auth(request):
                # 非API请求直接通过（如静态资源），API请求返回401
                if path.startswith('/api/'):
                    return web.json_response({'error': '未授权'}, status=401)
                return await handler(request)

            info = web_mgr._get_auth_info(request)
            role = info.get('login_type', 'admin') if info else 'admin'

            # 管理员专属端点
            admin_only_prefixes = ['/api/config', '/api/cleanup', '/api/migrate_memory', '/api/batch_memory',
                                   '/api/command/', '/api/persona/create', '/api/persona/migrate']
            if role == 'user':
                for prefix in admin_only_prefixes:
                    if path.startswith(prefix):
                        return web.json_response({'error': '仅管理员可操作'}, status=403)

            # 用户模式下，校验数据访问权限（query参数中的user_id）
            if role == 'user' and path.startswith('/api/'):
                qq = info.get('qq_number', '')
                mode = request.query.get('mode', 'session')
                if mode == 'session':
                    req_user_id = request.query.get('user_id', '')
                    if req_user_id and req_user_id != qq:
                        return web.json_response({'error': '无权访问该用户数据'}, status=403)

            return await handler(request)

        app = web.Application(client_max_size=1024 * 1024 * 50, middlewares=[permission_middleware])

        app.router.add_get('/', self.handle_index)
        app.router.add_get('/favicon.ico', self.handle_favicon)

        app.router.add_post('/api/login', self.handle_login)
        app.router.add_get('/api/check_auth', self.handle_check_auth)
        app.router.add_post('/api/logout', self.handle_logout)
        app.router.add_post('/api/change_password', self.handle_change_password)

        app.router.add_get('/api/sessions', self.handle_get_sessions)
        app.router.add_get('/api/personas', self.handle_get_personas)
        app.router.add_get('/api/persona_list', self.handle_get_persona_list)
        app.router.add_get('/api/persona_users', self.handle_get_persona_users)
        app.router.add_get('/api/stats', self.handle_get_stats)
        app.router.add_get('/api/memory_stats', self.handle_get_memory_stats)

        app.router.add_get('/api/core_memory', self.handle_get_core_memory)
        app.router.add_put('/api/core_memory', self.handle_update_core_memory)
        app.router.add_delete('/api/core_memory', self.handle_delete_core_memory)
        app.router.add_get('/api/core_memory_history', self.handle_get_core_memory_history)
        app.router.add_post('/api/core_memory_restore', self.handle_restore_core_memory)

        app.router.add_get('/api/diaries', self.handle_get_diaries)
        app.router.add_post('/api/diaries', self.handle_add_diary)
        app.router.add_put('/api/diaries/{index}', self.handle_update_diary)
        app.router.add_delete('/api/diaries/{index}', self.handle_delete_diary)
        app.router.add_delete('/api/diaries', self.handle_delete_diaries_batch)

        app.router.add_get('/api/plot_framework', self.handle_get_plot_framework)
        app.router.add_put('/api/plot_framework', self.handle_update_plot_framework)
        app.router.add_delete('/api/plot_framework', self.handle_delete_plot_framework)

        app.router.add_get('/api/plot_summaries', self.handle_get_plot_summaries)
        app.router.add_post('/api/plot_summaries', self.handle_add_plot_summary)
        app.router.add_put('/api/plot_summaries/{index}', self.handle_update_plot_summary)
        app.router.add_delete('/api/plot_summaries/{index}', self.handle_delete_plot_summary)
        app.router.add_delete('/api/plot_summaries', self.handle_delete_plot_summaries_batch)

        app.router.add_get('/api/locked_memories', self.handle_get_locked_memories)
        app.router.add_post('/api/locked_memories', self.handle_add_locked_memory)
        app.router.add_put('/api/locked_memories/{index}', self.handle_update_locked_memory)
        app.router.add_delete('/api/locked_memories/{index}', self.handle_delete_locked_memory)
        app.router.add_delete('/api/locked_memories', self.handle_clear_locked_memories)

        app.router.add_get('/api/full_log', self.handle_get_full_log)
        app.router.add_put('/api/full_log', self.handle_update_full_log)
        app.router.add_delete('/api/full_log', self.handle_delete_full_log)

        app.router.add_get('/api/config', self.handle_get_config)
        app.router.add_post('/api/config', self.handle_save_config)

        app.router.add_post('/api/cleanup_preview', self.handle_cleanup_preview)
        app.router.add_post('/api/cleanup', self.handle_cleanup)

        app.router.add_post('/api/migrate_memory', self.handle_migrate_memory)
        app.router.add_post('/api/batch_memory/preview', self.handle_batch_memory_preview)
        app.router.add_post('/api/batch_memory/execute', self.handle_batch_memory_execute)

        app.router.add_post('/api/command/memory_summary', self.handle_cmd_memory_summary)
        app.router.add_post('/api/command/plot_summary', self.handle_cmd_plot_summary)
        app.router.add_post('/api/command/regenerate_framework', self.handle_cmd_regenerate_framework)
        app.router.add_post('/api/command/summarize_diary', self.handle_cmd_summarize_diary)
        app.router.add_post('/api/command/rebuild_vectors', self.handle_cmd_rebuild_vectors)
        app.router.add_post('/api/command/compress_framework', self.handle_cmd_compress_framework)
        app.router.add_get('/api/command/status', self.handle_get_task_status)

        app.router.add_post('/api/persona/create', self.handle_create_persona_space)
        app.router.add_post('/api/persona/migrate', self.handle_migrate_to_persona)

        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()

        port = self.config.get("web_port", 16130)

        try:
            self.web_site = web.TCPSite(self.web_runner, "0.0.0.0", port)
            await self.web_site.start()
            logger.info(f"[CoreMemory] Web管理界面已启动: http://localhost:{port}")
        except OSError as e:
            logger.error(f"[CoreMemory] Web管理界面启动失败，端口 {port} 可能被占用: {e}")

    async def stop(self):
        if self.web_site:
            await self.web_site.stop()
        if self.web_runner:
            await self.web_runner.cleanup()
        logger.info("[CoreMemory] Web管理界面已停止")

    def _check_auth(self, request: web.Request) -> bool:
        token = request.headers.get('Authorization', '')
        if token.startswith('Bearer '):
            token = token[7:]
        return token in self.sessions

    def _is_admin(self, request: web.Request) -> bool:
        info = self._get_auth_info(request)
        return info is not None and info.get('login_type') == 'admin'

    def _get_login_qq(self, request: web.Request) -> Optional[str]:
        """获取当前登录用户的QQ号，管理员返回None"""
        info = self._get_auth_info(request)
        if info and info.get('login_type') == 'user':
            return info.get('qq_number')
        return None

    def _get_session_params(self, request: web.Request) -> dict:
        return {
            'user_id': request.query.get('user_id', ''),
            'session_id': request.query.get('session_id', ''),
            'role_name': request.query.get('role_name', ''),
            'space_name': request.query.get('space_name', ''),
            'mode': request.query.get('mode', 'session'),
            'persona_user': request.query.get('persona_user', '')
        }

    def _get_persona_path(self, role_name: str, space_name: str, data_type: str, user_id: str = None) -> str:
        """
        获取 Persona 模式的文件路径
        优先使用主插件的方法以确保一致性，失败时回退到本地逻辑
        """
        role_name = role_name.replace('.md', '').replace('.txt', '')

        # 优先使用主插件的方法（确保与保存逻辑一致）
        if self.plugin_instance and hasattr(self.plugin_instance, '_get_persona_memory_path'):
            try:
                return self.plugin_instance._get_persona_memory_path(
                    role_name, space_name, data_type, user_id
                )
            except Exception as e:
                logger.warning(f"[WebManager] 使用主插件路径方法失败: {e}，回退到本地逻辑")

        # 兜底逻辑：使用本地的路径拼接（与主插件保持一致）
        dir_name = f"persona_{role_name}_{space_name}"
        base_path = os.path.join(self.memory_manager.persona_memories_path, dir_name)

        # 用户特定数据（核心记忆、锁定记忆）
        if data_type in ['core_memory', 'locked_memory', 'core_memory_history']:
            if user_id:
                user_dir = os.path.join(base_path, f"user_{user_id}")
            else:
                user_dir = os.path.join(base_path, "shared")
            os.makedirs(user_dir, exist_ok=True)

            if data_type == 'core_memory':
                return os.path.join(user_dir, 'core_memory.json')
            elif data_type == 'locked_memory':
                return os.path.join(user_dir, 'locked_memory.json')
            elif data_type == 'core_memory_history':
                history_dir = os.path.join(user_dir, 'core_memory_history')
                os.makedirs(history_dir, exist_ok=True)
                return history_dir

        # 共享数据
        shared_dir = os.path.join(base_path, "shared")
        os.makedirs(shared_dir, exist_ok=True)

        # ===== 关键修复：剧情总结的兼容性处理 =====
        if data_type == 'plot_summaries':
            # 优先使用新文件名，兼容旧文件名
            new_path = os.path.join(shared_dir, 'plot_summaries.json')
            old_path = os.path.join(shared_dir, 'plot_summary.json')

            # 如果旧文件存在且新文件不存在，返回旧文件路径
            if os.path.exists(old_path) and not os.path.exists(new_path):
                return old_path
            return new_path

        # 其他共享数据类型
        type_map = {
            'full_log': 'full_log.txt',
            'diary': 'diary.json',
            'diary_vectors': 'diary_vectors.json',
            'plot_framework': 'plot_framework.json',
            'summary_round': 'summary_round.json'
        }

        return os.path.join(shared_dir, type_map.get(data_type, f'{data_type}.json'))


    # ========== 新增：强制 Session 模式路径方法 ==========

    def _get_session_path(self, user_id: str, session_id: str, role_name: str, data_type: str) -> str:
        """
        强制使用 Session 模式的路径，不做自动模式检测。
        这样 Web 界面的 Session Tab 就不会被角色的 persona 配置影响。
        """
        role_name_clean = role_name.replace('.md', '').replace('.txt', '')
        base_name = f"{user_id}_{session_id}_{role_name_clean}"

        # 剧情总结兼容两种文件名
        if data_type == "plot_summaries":
            old_path = os.path.join(self.memory_manager.plot_summaries_path, f"{base_name}_plot_summary.json")
            new_path = os.path.join(self.memory_manager.plot_summaries_path, f"{base_name}_plot_summaries.json")
            if os.path.exists(old_path):
                return old_path
            return new_path

        path_map = {
            "core_memory": (self.memory_manager.summaries_path, f"{base_name}.json"),
            "diary": (self.memory_manager.diaries_path, f"{base_name}_diary.json"),
            "diary_vectors": (self.memory_manager.vectors_path, f"{base_name}_diary_vectors.json"),
            "plot_framework": (self.memory_manager.plot_frameworks_path, f"{base_name}_plot.json"),
            "full_log": (self.memory_manager.full_logs_path, f"{base_name}_full.txt"),
            "locked_memory": (self.memory_manager.locked_memories_path, f"{base_name}_locked.json"),
            "offset": (self.memory_manager.offsets_path, f"{base_name}_offset.json"),
        }

        base_path, filename = path_map.get(data_type, (self.data_dir, f"{base_name}.txt"))
        return os.path.join(base_path, filename)

    def _get_session_history_dir(self, user_id: str, session_id: str, role_name: str) -> str:
        """强制获取 Session 模式的核心记忆历史目录"""
        role_name_clean = role_name.replace('.md', '').replace('.txt', '')
        base_name = f"{user_id}_{session_id}_{role_name_clean}"
        history_dir = os.path.join(self.memory_manager.core_memory_history_path, base_name)
        os.makedirs(history_dir, exist_ok=True)
        return history_dir

    def _read_json_file(self, file_path: str, default=None):
        """安全读取 JSON 文件"""
        if not os.path.exists(file_path):
            return default if default is not None else {}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取 JSON 文件失败 {file_path}: {e}")
            return default if default is not None else {}

    def _write_json_file(self, file_path: str, data):
        """安全写入 JSON 文件"""
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.2f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    def _get_batch_memory_targets(self, include_session: bool = True, include_persona: bool = True) -> List[Dict]:
        targets = []

        if include_session:
            for session in self.memory_manager.get_all_memory_sessions():
                user_id = session.get('user_id', '')
                session_id = session.get('session_id', '')
                role_name = session.get('role_name', '')
                if not all([user_id, session_id, role_name]):
                    continue
                targets.append({
                    'mode': 'session',
                    'label': f"会话 {role_name} / {user_id} / {session_id}",
                    'core_path': self._get_session_path(user_id, session_id, role_name, 'core_memory'),
                    'locked_path': self._get_session_path(user_id, session_id, role_name, 'locked_memory'),
                })

        if include_persona:
            for persona in self.memory_manager.get_all_persona_memories():
                role_name = persona.get('role_name', '')
                space_name = persona.get('space_name', '')
                if not role_name or not space_name:
                    continue
                for user_id in persona.get('users', []) or []:
                    targets.append({
                        'mode': 'persona',
                        'label': f"共享 {role_name} / {space_name} / 用户 {user_id}",
                        'core_path': self._get_persona_path(role_name, space_name, 'core_memory', user_id),
                        'locked_path': self._get_persona_path(role_name, space_name, 'locked_memory', user_id),
                    })

        return targets

    def _preview_batch_memory(self, memory_type: str, content: str, targets: List[Dict]) -> Dict:
        preview = {
            'total': len(targets),
            'session_count': sum(1 for t in targets if t['mode'] == 'session'),
            'persona_count': sum(1 for t in targets if t['mode'] == 'persona'),
            'would_add': 0,
            'duplicates': 0,
            'errors': []
        }

        for target in targets:
            try:
                if memory_type == 'core':
                    data = self._read_json_file(target['core_path'], {})
                    existing = str(data.get('content', '') or '') if isinstance(data, dict) else ''
                    if content in existing:
                        preview['duplicates'] += 1
                    else:
                        preview['would_add'] += 1
                else:
                    data = self._read_json_file(target['locked_path'], {"locked_items": []})
                    if isinstance(data, dict):
                        locked = data.get('locked_items', [])
                    elif isinstance(data, list):
                        locked = data
                    else:
                        locked = []
                    if any(item.get('content') == content for item in locked if isinstance(item, dict)):
                        preview['duplicates'] += 1
                    else:
                        preview['would_add'] += 1
            except Exception as e:
                preview['errors'].append({'target': target['label'], 'error': str(e)})

        return preview

    def _append_core_memory_file(self, file_path: str, content: str):
        old_data = self._read_json_file(file_path, {})
        old_content = str(old_data.get('content', '') or '') if isinstance(old_data, dict) else ''
        if content in old_content:
            return False

        if old_content:
            history_dir = os.path.join(os.path.dirname(file_path), 'core_memory_history')
            if file_path.startswith(self.memory_manager.summaries_path):
                base_name = os.path.basename(file_path)[:-5]
                history_dir = os.path.join(self.memory_manager.core_memory_history_path, base_name)
            os.makedirs(history_dir, exist_ok=True)
            history_file = os.path.join(history_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json")
            self._write_json_file(history_file, old_data)
            new_content = old_content.rstrip() + "\n\n" + content
        else:
            new_content = content

        self._write_json_file(file_path, {
            'content': new_content,
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        return True

    def _append_locked_memory_file(self, file_path: str, content: str):
        file_data = self._read_json_file(file_path, {"locked_items": []})
        if isinstance(file_data, dict):
            locked = file_data.get('locked_items', [])
        elif isinstance(file_data, list):
            locked = file_data
        else:
            locked = []

        for item in locked:
            if isinstance(item, dict) and item.get('content') == content:
                return False

        new_id = max([item.get('id', 0) for item in locked if isinstance(item, dict)], default=0) + 1
        locked.append({
            'id': new_id,
            'content': content,
            'source': 'admin_batch',
            'locked_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        self._write_json_file(file_path, {
            'locked_items': locked,
            'last_updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        return True

    async def handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def handle_login(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            login_type = data.get('login_type', 'admin')
            password = data.get('password', '')

            if login_type == 'admin':
                correct_password = self.config.get('web_password', 'irisqiyuan')
                if password == correct_password:
                    token = str(uuid.uuid4())
                    self.sessions[token] = {
                        'created_at': datetime.now().isoformat(),
                        'last_active': datetime.now().isoformat(),
                        'login_type': 'admin',
                        'qq_number': ''
                    }
                    return web.json_response({'success': True, 'token': token, 'login_type': 'admin'})
                else:
                    return web.json_response({'success': False, 'error': '管理员密码错误'}, status=401)

            elif login_type == 'user':
                qq_number = data.get('qq_number', '').strip()
                if not qq_number:
                    return web.json_response({'success': False, 'error': '请输入QQ号'}, status=400)

                if not self._verify_user_password(qq_number, password):
                    return web.json_response({'success': False, 'error': 'QQ号或密码错误'}, status=401)

                token = str(uuid.uuid4())
                is_default = self._is_default_password(qq_number)
                self.sessions[token] = {
                    'created_at': datetime.now().isoformat(),
                    'last_active': datetime.now().isoformat(),
                    'login_type': 'user',
                    'qq_number': qq_number
                }
                return web.json_response({
                    'success': True,
                    'token': token,
                    'login_type': 'user',
                    'qq_number': qq_number,
                    'is_default_password': is_default
                })
            else:
                return web.json_response({'success': False, 'error': '未知登录类型'}, status=400)

        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)

    async def handle_check_auth(self, request: web.Request) -> web.Response:
        auth = self._get_auth_info(request)
        if auth:
            resp = {'authenticated': True, 'login_type': auth.get('login_type', 'admin')}
            if auth.get('login_type') == 'user':
                resp['qq_number'] = auth.get('qq_number', '')
            return web.json_response(resp)
        return web.json_response({'authenticated': False}, status=401)

    async def handle_logout(self, request: web.Request) -> web.Response:
        token = request.headers.get('Authorization', '')
        if token.startswith('Bearer '):
            token = token[7:]
        if token in self.sessions:
            del self.sessions[token]
        return web.json_response({'success': True})

    async def handle_change_password(self, request: web.Request) -> web.Response:
        auth = self._get_auth_info(request)
        if not auth:
            return web.json_response({'error': '未授权'}, status=401)
        if auth.get('login_type') != 'user':
            return web.json_response({'error': '仅用户可修改密码'}, status=403)
        try:
            data = await request.json()
            old_password = data.get('old_password', '')
            new_password = data.get('new_password', '')
            qq_number = auth.get('qq_number', '')
            if not new_password or len(new_password) < 4:
                return web.json_response({'success': False, 'error': '新密码至少4位'}, status=400)
            if not self._verify_user_password(qq_number, old_password):
                return web.json_response({'success': False, 'error': '原密码错误'}, status=400)
            self._change_user_password(qq_number, new_password)
            return web.json_response({'success': True, 'message': '密码修改成功'})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)

    async def handle_get_sessions(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)

        try:
            sessions = self.memory_manager.get_all_memory_sessions()
            auth = self._get_auth_info(request)
            if auth and auth.get('login_type') == 'user':
                qq = auth.get('qq_number', '')
                sessions = [s for s in sessions if s.get('user_id') == qq]
            return web.json_response({'sessions': sessions})
        except Exception as e:
            return web.json_response({'sessions': [], 'error': str(e)})

    async def handle_get_personas(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)

        try:
            personas = self.memory_manager.get_all_persona_memories()
            auth = self._get_auth_info(request)
            if auth and auth.get('login_type') == 'user':
                qq = auth.get('qq_number', '')
                filtered = []
                for p in personas:
                    persona_dir = os.path.join(
                        self.memory_manager.persona_memories_path,
                        f"persona_{p['role_name']}_{p['space_name']}"
                    )
                    if os.path.exists(persona_dir):
                        for item in os.listdir(persona_dir):
                            if item == f"user_{qq}":
                                filtered.append(p)
                                break
                personas = filtered
            return web.json_response({'personas': personas})
        except Exception as e:
            return web.json_response({'personas': [], 'error': str(e)})

    async def handle_get_persona_list(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)

        try:
            personas = []

            if self.plugin_instance and hasattr(self.plugin_instance, 'context'):
                try:
                    persona_manager = self.plugin_instance.context.persona_manager
                    if hasattr(persona_manager, 'personas'):
                        if isinstance(persona_manager.personas, dict):
                            for persona_id in persona_manager.personas.keys():
                                name = persona_id.replace('.md', '').replace('.txt', '')
                                personas.append({'id': persona_id, 'name': name})
                        elif isinstance(persona_manager.personas, list):
                            for p in persona_manager.personas:
                                if hasattr(p, 'persona_id'):
                                    pid = p.persona_id
                                elif hasattr(p, 'id'):
                                    pid = p.id
                                elif isinstance(p, str):
                                    pid = p
                                else:
                                    pid = str(p)
                                name = pid.replace('.md', '').replace('.txt', '')
                                personas.append({'id': pid, 'name': name})
                except Exception as e:
                    logger.warning(f"从persona_manager获取人格列表失败: {e}")

            if not personas:
                try:
                    sessions = self.memory_manager.get_all_memory_sessions()
                    role_names = set()
                    for s in sessions:
                        if s.get('role_name'):
                            role_names.add(s['role_name'])
                    for rn in role_names:
                        name = rn.replace('.md', '').replace('.txt', '')
                        personas.append({'id': rn, 'name': name})
                except Exception as e:
                    logger.warning(f"从session数据提取角色名失败: {e}")

            return web.json_response({'personas': personas})
        except Exception as e:
            return web.json_response({'personas': [], 'error': str(e)})

    async def handle_get_persona_users(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)

        try:
            role_name = request.query.get('role_name', '').replace('.md', '').replace('.txt', '')
            space_name = request.query.get('space_name', '')

            if not role_name or not space_name:
                return web.json_response({'users': []})

            users = self.memory_manager.get_persona_users(role_name, space_name)
            return web.json_response({'users': users})
        except Exception as e:
            return web.json_response({'users': [], 'error': str(e)})

    async def handle_get_stats(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona' and params['role_name'] and params['space_name']:
                stats = self._get_persona_stats(params['role_name'], params['space_name'])
            elif params['mode'] == 'session' and params['user_id'] and params['session_id'] and params['role_name']:
                stats = self._get_session_stats(params['user_id'], params['session_id'], params['role_name'])
            else:
                stats = self.memory_manager.get_storage_stats()

            formatted_stats = {
                'total_size': self._format_size(stats.get('total_size', 0)),
                'total_size_bytes': stats.get('total_size', 0),
                'details': {}
            }

            for key, value in stats.items():
                if key != 'total_size' and isinstance(value, dict):
                    formatted_stats['details'][key] = {
                        'size': self._format_size(value.get('size', 0)),
                        'size_bytes': value.get('size', 0),
                        'count': value.get('count', 0)
                    }

            return web.json_response(formatted_stats)
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return web.json_response({'total_size': '0 B', 'details': {}})

    def _get_session_stats(self, user_id: str, session_id: str, role_name: str) -> dict:
        """获取 Session 模式的存储统计（强制使用 session 路径）"""
        stats = {'total_size': 0}

        type_stats = {
            'diaries': {'size': 0, 'count': 0},
            'plot_summaries': {'size': 0, 'count': 0},
            'locked_memories': {'size': 0, 'count': 0},
            'full_log': {'size': 0, 'count': 0},
            'core_memory': {'size': 0, 'count': 0}
        }

        # 核心记忆
        core_path = self._get_session_path(user_id, session_id, role_name, 'core_memory')
        if os.path.exists(core_path):
            size = os.path.getsize(core_path)
            type_stats['core_memory']['size'] = size
            type_stats['core_memory']['count'] = 1
            stats['total_size'] += size

        # 记忆日志
        diary_path = self._get_session_path(user_id, session_id, role_name, 'diary')
        if os.path.exists(diary_path):
            size = os.path.getsize(diary_path)
            type_stats['diaries']['size'] = size
            stats['total_size'] += size
            try:
                data = self._read_json_file(diary_path, [])
                type_stats['diaries']['count'] = len(data) if isinstance(data, list) else 0
            except:
                pass

        # 剧情总结（关键！）
        summaries_path = self._get_session_path(user_id, session_id, role_name, 'plot_summaries')
        if os.path.exists(summaries_path):
            size = os.path.getsize(summaries_path)
            type_stats['plot_summaries']['size'] = size
            stats['total_size'] += size
            try:
                data = self._read_json_file(summaries_path, [])
                type_stats['plot_summaries']['count'] = len(data) if isinstance(data, list) else 0
            except:
                pass

        # 锁定记忆
        locked_path = self._get_session_path(user_id, session_id, role_name, 'locked_memory')
        if os.path.exists(locked_path):
            size = os.path.getsize(locked_path)
            type_stats['locked_memories']['size'] = size
            stats['total_size'] += size
            try:
                data = self._read_json_file(locked_path, {})
                if isinstance(data, dict) and 'locked_items' in data:
                    type_stats['locked_memories']['count'] = len(data['locked_items'])
                elif isinstance(data, list):
                    type_stats['locked_memories']['count'] = len(data)
            except:
                pass

        # 完整对话
        log_path = self._get_session_path(user_id, session_id, role_name, 'full_log')
        if os.path.exists(log_path):
            size = os.path.getsize(log_path)
            type_stats['full_log']['size'] = size
            stats['total_size'] += size

        stats.update(type_stats)
        return stats

    def _get_persona_stats(self, role_name: str, space_name: str) -> dict:
        stats = {'total_size': 0}
        role_name = role_name.replace('.md', '').replace('.txt', '')

        type_stats = {
            'diaries': {'size': 0, 'count': 0},
            'plot_summaries': {'size': 0, 'count': 0},
            'locked_memories': {'size': 0, 'count': 0},
            'full_log': {'size': 0, 'count': 0}
        }

        # ===== 使用统一的路径获取方法 =====

        # 记忆日志
        diary_path = self._get_persona_path(role_name, space_name, 'diary')
        if os.path.exists(diary_path):
            size = os.path.getsize(diary_path)
            type_stats['diaries']['size'] = size
            stats['total_size'] += size
            try:
                data = self._read_json_file(diary_path, [])
                type_stats['diaries']['count'] = len(data) if isinstance(data, list) else 0
            except:
                pass

        # ===== 剧情总结（关键修复）=====
        summaries_path = self._get_persona_path(role_name, space_name, 'plot_summaries')
        if os.path.exists(summaries_path):
            size = os.path.getsize(summaries_path)
            type_stats['plot_summaries']['size'] = size
            stats['total_size'] += size
            try:
                data = self._read_json_file(summaries_path, [])
                type_stats['plot_summaries']['count'] = len(data) if isinstance(data, list) else 0
            except:
                pass

        # 完整对话记录
        log_path = self._get_persona_path(role_name, space_name, 'full_log')
        if os.path.exists(log_path):
            size = os.path.getsize(log_path)
            type_stats['full_log']['size'] = size
            stats['total_size'] += size

        # 锁定记忆（用户特定数据，需要遍历所有用户）
        # 获取 base_path 用于遍历用户目录
        dir_name = f"persona_{role_name}_{space_name}"
        base_path = os.path.join(self.memory_manager.persona_memories_path, dir_name)

        try:
            for item in os.listdir(base_path):
                if item.startswith("user_") and os.path.isdir(os.path.join(base_path, item)):
                    user_id = item.replace("user_", "")

                    # 核心记忆
                    core_path = self._get_persona_path(role_name, space_name, 'core_memory', user_id)
                    if os.path.exists(core_path):
                        stats['total_size'] += os.path.getsize(core_path)

                    # 锁定记忆
                    locked_path = self._get_persona_path(role_name, space_name, 'locked_memory', user_id)
                    if os.path.exists(locked_path):
                        size = os.path.getsize(locked_path)
                        type_stats['locked_memories']['size'] += size
                        stats['total_size'] += size
                        try:
                            data = self._read_json_file(locked_path, {})
                            if isinstance(data, dict) and 'locked_items' in data:
                                type_stats['locked_memories']['count'] += len(data['locked_items'])
                            elif isinstance(data, list):
                                type_stats['locked_memories']['count'] += len(data)
                        except:
                            pass
        except:
            pass

        stats.update(type_stats)
        return stats


    async def handle_get_memory_stats(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            stats = {
                'core_memory': {'count': 0, 'chars': 0},
                'diaries': {'count': 0, 'chars': 0},
                'plot_framework': {'count': 0, 'chars': 0},
                'plot_summaries': {'count': 0, 'chars': 0},
                'locked_memories': {'count': 0, 'chars': 0},
                'full_log': {'count': 0, 'chars': 0}
            }

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'stats': stats})

                role_name = params['role_name'].replace('.md', '').replace('.txt', '')
                space_name = params['space_name']

                if params.get('persona_user'):
                    core_path = self._get_persona_path(role_name, space_name, 'core_memory', params['persona_user'])
                    if os.path.exists(core_path):
                        data = self._read_json_file(core_path, {})
                        content = data.get('content', '')
                        stats['core_memory']['count'] = 1 if content else 0
                        stats['core_memory']['chars'] = len(content)

                diary_path = self._get_persona_path(role_name, space_name, 'diary')
                if os.path.exists(diary_path):
                    diaries = self._read_json_file(diary_path, [])
                    stats['diaries']['count'] = len(diaries)
                    stats['diaries']['chars'] = sum(len(d.get('summary', '')) for d in diaries)

                framework_path = self._get_persona_path(role_name, space_name, 'plot_framework')
                if os.path.exists(framework_path):
                    data = self._read_json_file(framework_path, {})
                    content = data.get('content', '') if isinstance(data, dict) else ''
                    stats['plot_framework']['count'] = 1 if content else 0
                    stats['plot_framework']['chars'] = len(content)

                summaries_path = self._get_persona_path(role_name, space_name, 'plot_summaries')
                if os.path.exists(summaries_path):
                    summaries = self._read_json_file(summaries_path, [])
                    stats['plot_summaries']['count'] = len(summaries)
                    stats['plot_summaries']['chars'] = sum(len(s.get('summary', '')) for s in summaries)

                if params.get('persona_user'):
                    locked_path = self._get_persona_path(role_name, space_name, 'locked_memory', params['persona_user'])
                    if os.path.exists(locked_path):
                        data = self._read_json_file(locked_path, {})
                        if isinstance(data, dict) and 'locked_items' in data:
                            items = data['locked_items']
                        elif isinstance(data, list):
                            items = data
                        else:
                            items = []
                        stats['locked_memories']['count'] = len(items)
                        stats['locked_memories']['chars'] = sum(len(item.get('content', '')) for item in items)

                log_path = self._get_persona_path(role_name, space_name, 'full_log')
                if os.path.exists(log_path):
                    with open(log_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        lines = [l for l in content.split('\n') if l.strip()]
                        stats['full_log']['count'] = len(lines) // 2
                        stats['full_log']['chars'] = len(content)

            else:
                # Session 模式：使用强制路径
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'stats': stats})

                user_id = params['user_id']
                session_id = params['session_id']
                role_name = params['role_name']

                core_path = self._get_session_path(user_id, session_id, role_name, 'core_memory')
                if os.path.exists(core_path):
                    data = self._read_json_file(core_path, {})
                    content = data.get('content', '')
                    stats['core_memory']['count'] = 1 if content else 0
                    stats['core_memory']['chars'] = len(content)

                diary_path = self._get_session_path(user_id, session_id, role_name, 'diary')
                if os.path.exists(diary_path):
                    diaries = self._read_json_file(diary_path, [])
                    stats['diaries']['count'] = len(diaries)
                    stats['diaries']['chars'] = sum(len(d.get('summary', '')) for d in diaries)

                framework_path = self._get_session_path(user_id, session_id, role_name, 'plot_framework')
                if os.path.exists(framework_path):
                    data = self._read_json_file(framework_path, {})
                    content = data.get('content', '') if isinstance(data, dict) else ''
                    stats['plot_framework']['count'] = 1 if content else 0
                    stats['plot_framework']['chars'] = len(content)

                summaries_path = self._get_session_path(user_id, session_id, role_name, 'plot_summaries')
                if os.path.exists(summaries_path):
                    summaries = self._read_json_file(summaries_path, [])
                    stats['plot_summaries']['count'] = len(summaries)
                    stats['plot_summaries']['chars'] = sum(len(s.get('summary', '')) for s in summaries)

                locked_path = self._get_session_path(user_id, session_id, role_name, 'locked_memory')
                if os.path.exists(locked_path):
                    data = self._read_json_file(locked_path, {})
                    if isinstance(data, dict) and 'locked_items' in data:
                        items = data['locked_items']
                    elif isinstance(data, list):
                        items = data
                    else:
                        items = []
                    stats['locked_memories']['count'] = len(items)
                    stats['locked_memories']['chars'] = sum(len(item.get('content', '')) for item in items)

                log_path = self._get_session_path(user_id, session_id, role_name, 'full_log')
                if os.path.exists(log_path):
                    with open(log_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        lines = [l for l in content.split('\n') if l.strip()]
                        stats['full_log']['count'] = len(lines) // 2
                        stats['full_log']['chars'] = len(content)

            return web.json_response({'stats': stats})
        except Exception as e:
            return web.json_response({'stats': {}, 'error': str(e)})

    # ========== 核心记忆操作 ==========

    async def handle_get_core_memory(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'core_memory', params.get('persona_user'))
                data = self._read_json_file(file_path, {})
                return web.json_response({'data': data})
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'core_memory')
                data = self._read_json_file(file_path, {})
                return web.json_response({'data': data})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_update_core_memory(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            content = data.get('content', '')

            if not content.strip():
                return web.json_response({'error': '内容不能为空'}, status=400)

            save_data = {'content': content, 'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'core_memory', params.get('persona_user'))
                self._write_json_file(file_path, save_data)
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'core_memory')

                # 备份旧版本
                if os.path.exists(file_path):
                    try:
                        old_data = self._read_json_file(file_path, {})
                        if old_data and old_data.get("content"):
                            history_dir = self._get_session_history_dir(params['user_id'], params['session_id'], params['role_name'])
                            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                            history_file = os.path.join(history_dir, f"{timestamp_str}.json")
                            self._write_json_file(history_file, old_data)
                    except Exception as e:
                        logger.warning(f"备份核心记忆历史版本失败: {e}")

                self._write_json_file(file_path, save_data)

            return web.json_response({'success': True, 'message': '核心记忆已更新'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_core_memory(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'core_memory', params.get('persona_user'))
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'core_memory')

            if os.path.exists(file_path):
                os.remove(file_path)

            return web.json_response({'success': True, 'message': '核心记忆已清空'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_get_core_memory_history(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'history': []})
                history_dir = self._get_persona_path(params['role_name'], params['space_name'], 'core_memory_history', params.get('persona_user'))
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'history': []})
                history_dir = self._get_session_history_dir(params['user_id'], params['session_id'], params['role_name'])

            history = []
            if os.path.exists(history_dir):
                for filename in sorted(os.listdir(history_dir), reverse=True):
                    if filename.endswith('.json'):
                        file_path = os.path.join(history_dir, filename)
                        try:
                            data = self._read_json_file(file_path, {})
                            if data:
                                data['history_filename'] = filename
                                history.append(data)
                        except:
                            pass
            return web.json_response({'history': history})
        except Exception as e:
            return web.json_response({'history': []})

    async def handle_restore_core_memory(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            history_filename = data.get('filename', '')

            if not history_filename:
                return web.json_response({'error': '请指定要恢复的历史版本'}, status=400)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                history_dir = self._get_persona_path(params['role_name'], params['space_name'], 'core_memory_history', params.get('persona_user'))
                target_path = self._get_persona_path(params['role_name'], params['space_name'], 'core_memory', params.get('persona_user'))
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                history_dir = self._get_session_history_dir(params['user_id'], params['session_id'], params['role_name'])
                target_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'core_memory')

            history_file = os.path.join(history_dir, history_filename)
            if not os.path.exists(history_file):
                return web.json_response({'error': '历史版本不存在'}, status=404)

            history_data = self._read_json_file(history_file, {})
            content = history_data.get('content', '')

            if content:
                save_data = {'content': content, 'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                self._write_json_file(target_path, save_data)
                return web.json_response({'success': True, 'message': '已恢复到历史版本'})
            else:
                return web.json_response({'error': '历史版本内容为空'}, status=400)
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 记忆日志操作 ==========

    async def handle_get_diaries(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            page = int(request.query.get('page', 1))
            page_size = int(request.query.get('page_size', 20))

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'diaries': [], 'total': 0, 'page': 1, 'total_pages': 1})
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'diary')
                all_diaries = self._read_json_file(file_path, [])
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'diaries': [], 'total': 0, 'page': 1, 'total_pages': 1})
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'diary')
                all_diaries = self._read_json_file(file_path, [])

            total = len(all_diaries)
            for i, diary in enumerate(all_diaries):
                diary['index'] = i

            all_diaries_reversed = list(reversed(all_diaries))
            start = (page - 1) * page_size
            end = start + page_size
            diaries = all_diaries_reversed[start:end]

            return web.json_response({
                'diaries': diaries,
                'total': total,
                'page': page,
                'page_size': page_size,
                'total_pages': (total + page_size - 1) // page_size if total > 0 else 1
            })
        except Exception as e:
            return web.json_response({'diaries': [], 'total': 0, 'page': 1, 'total_pages': 1})

    async def handle_add_diary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            content = data.get('content', '').strip()
            timestamp = data.get('timestamp', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            user_label = data.get('user_label', '')

            if not content:
                return web.json_response({'error': '内容不能为空'}, status=400)

            new_entry = {"timestamp": timestamp, "summary": content}
            if user_label:
                new_entry["user_label"] = user_label

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'diary')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'diary')

            diaries = self._read_json_file(file_path, [])
            diaries.append(new_entry)
            self._write_json_file(file_path, diaries)

            return web.json_response({'success': True, 'message': '记忆日志已添加'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_update_diary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            index = int(request.match_info['index'])
            data = await request.json()
            new_summary = data.get('summary', '')

            if not new_summary.strip():
                return web.json_response({'error': '内容不能为空'}, status=400)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'diary')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'diary')

            all_diaries = self._read_json_file(file_path, [])

            if index < 0 or index >= len(all_diaries):
                return web.json_response({'error': '索引超出范围'}, status=400)

            all_diaries[index]['summary'] = new_summary
            all_diaries[index]['edited_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self._write_json_file(file_path, all_diaries)
            return web.json_response({'success': True, 'message': '记忆日志已更新'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_diary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            index = int(request.match_info['index'])

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                diary_path = self._get_persona_path(params['role_name'], params['space_name'], 'diary')
                vector_path = self._get_persona_path(params['role_name'], params['space_name'], 'diary_vectors')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                diary_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'diary')
                vector_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'diary_vectors')

            all_diaries = self._read_json_file(diary_path, [])
            all_vectors = self._read_json_file(vector_path, [])

            if index < 0 or index >= len(all_diaries):
                return web.json_response({'error': '索引超出范围'}, status=400)

            del all_diaries[index]
            if index < len(all_vectors):
                del all_vectors[index]

            self._write_json_file(diary_path, all_diaries)
            self._write_json_file(vector_path, all_vectors)

            return web.json_response({'success': True, 'message': '记忆日志已删除'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_diaries_batch(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            indices = data.get('indices', [])
            clear_all = data.get('clear_all', False)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                diary_path = self._get_persona_path(params['role_name'], params['space_name'], 'diary')
                vector_path = self._get_persona_path(params['role_name'], params['space_name'], 'diary_vectors')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                diary_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'diary')
                vector_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'diary_vectors')

            if clear_all:
                self._write_json_file(diary_path, [])
                self._write_json_file(vector_path, [])
                return web.json_response({'success': True, 'message': '所有记忆日志已清空'})

            if not indices:
                return web.json_response({'error': '请指定要删除的索引'}, status=400)

            all_diaries = self._read_json_file(diary_path, [])
            all_vectors = self._read_json_file(vector_path, [])

            for index in sorted(indices, reverse=True):
                if 0 <= index < len(all_diaries):
                    del all_diaries[index]
                    if index < len(all_vectors):
                        del all_vectors[index]

            self._write_json_file(diary_path, all_diaries)
            self._write_json_file(vector_path, all_vectors)

            return web.json_response({'success': True, 'message': f'已删除 {len(indices)} 条记忆日志'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 剧情框架操作 ==========

    async def handle_get_plot_framework(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'data': {}})
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'plot_framework')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'data': {}})
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'plot_framework')

            data = self._read_json_file(file_path, {})
            return web.json_response({'data': data})
        except Exception as e:
            return web.json_response({'data': {}})

    async def handle_update_plot_framework(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            content = data.get('content', '')

            save_data = {'content': content, 'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'plot_framework')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'plot_framework')

            self._write_json_file(file_path, save_data)
            return web.json_response({'success': True, 'message': '剧情框架已更新'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_plot_framework(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'plot_framework')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'plot_framework')

            if os.path.exists(file_path):
                os.remove(file_path)

            return web.json_response({'success': True, 'message': '剧情框架已清空'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 剧情总结操作 ==========

    async def handle_get_plot_summaries(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            page = int(request.query.get('page', 1))
            page_size = int(request.query.get('page_size', 10))

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'summaries': [], 'total': 0, 'page': 1, 'total_pages': 1})
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'plot_summaries')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'summaries': [], 'total': 0, 'page': 1, 'total_pages': 1})
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'plot_summaries')

            all_summaries = self._read_json_file(file_path, [])
            if not isinstance(all_summaries, list):
                all_summaries = []

            total = len(all_summaries)
            for i, summary in enumerate(all_summaries):
                summary['index'] = i

            all_summaries_reversed = list(reversed(all_summaries))
            start = (page - 1) * page_size
            end = start + page_size
            summaries = all_summaries_reversed[start:end]

            return web.json_response({
                'summaries': summaries,
                'total': total,
                'page': page,
                'page_size': page_size,
                'total_pages': (total + page_size - 1) // page_size if total > 0 else 1
            })
        except Exception as e:
            logger.error(f"获取剧情总结失败: {e}")
            return web.json_response({'summaries': [], 'total': 0, 'page': 1, 'total_pages': 1})

    async def handle_add_plot_summary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            content = data.get('content', '').strip()
            timestamp = data.get('timestamp', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

            if not content:
                return web.json_response({'error': '内容不能为空'}, status=400)

            new_entry = {"timestamp": timestamp, "summary": content}

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'plot_summaries')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'plot_summaries')

            summaries = self._read_json_file(file_path, [])
            summaries.append(new_entry)
            self._write_json_file(file_path, summaries)

            return web.json_response({'success': True, 'message': '剧情总结已添加'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_update_plot_summary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            index = int(request.match_info['index'])
            data = await request.json()
            new_summary = data.get('summary', '')

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'plot_summaries')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'plot_summaries')

            all_summaries = self._read_json_file(file_path, [])

            if index < 0 or index >= len(all_summaries):
                return web.json_response({'error': '索引超出范围'}, status=400)

            all_summaries[index]['summary'] = new_summary
            all_summaries[index]['edited_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self._write_json_file(file_path, all_summaries)
            return web.json_response({'success': True, 'message': '剧情总结已更新'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_plot_summary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            index = int(request.match_info['index'])

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'plot_summaries')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'plot_summaries')

            all_summaries = self._read_json_file(file_path, [])

            if index < 0 or index >= len(all_summaries):
                return web.json_response({'error': '索引超出范围'}, status=400)

            del all_summaries[index]
            self._write_json_file(file_path, all_summaries)

            return web.json_response({'success': True, 'message': '剧情总结已删除'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_plot_summaries_batch(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            indices = data.get('indices', [])
            clear_all = data.get('clear_all', False)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'plot_summaries')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'plot_summaries')

            if clear_all:
                self._write_json_file(file_path, [])
                return web.json_response({'success': True, 'message': '所有剧情总结已清空'})

            if not indices:
                return web.json_response({'error': '请指定要删除的索引'}, status=400)

            all_summaries = self._read_json_file(file_path, [])

            for index in sorted(indices, reverse=True):
                if 0 <= index < len(all_summaries):
                    del all_summaries[index]

            self._write_json_file(file_path, all_summaries)

            return web.json_response({'success': True, 'message': f'已删除 {len(indices)} 条剧情总结'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 锁定记忆操作 ==========

    async def handle_get_locked_memories(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'locked_memories': []})
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'locked_memory', params.get('persona_user'))
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'locked_memories': []})
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'locked_memory')

            data = self._read_json_file(file_path, {"locked_items": []})
            if isinstance(data, dict) and "locked_items" in data:
                locked = data["locked_items"]
            elif isinstance(data, list):
                locked = data
            else:
                locked = []

            return web.json_response({'locked_memories': locked})
        except Exception as e:
            return web.json_response({'locked_memories': []})

    async def handle_add_locked_memory(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            content = data.get('content', '')

            if not content.strip():
                return web.json_response({'error': '内容不能为空'}, status=400)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'locked_memory', params.get('persona_user'))
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'locked_memory')

            file_data = self._read_json_file(file_path, {"locked_items": []})
            if isinstance(file_data, dict) and "locked_items" in file_data:
                locked = file_data["locked_items"]
            elif isinstance(file_data, list):
                locked = file_data
            else:
                locked = []

            for item in locked:
                if item.get('content') == content:
                    return web.json_response({'error': '该内容已存在'}, status=400)

            new_id = max([item.get('id', 0) for item in locked], default=0) + 1
            locked.append({
                'id': new_id,
                'content': content,
                'locked_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

            save_data = {'locked_items': locked, 'last_updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            self._write_json_file(file_path, save_data)

            return web.json_response({'success': True, 'message': '锁定记忆已添加'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_update_locked_memory(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            index = int(request.match_info['index'])
            data = await request.json()
            new_content = data.get('content', '')

            if not new_content.strip():
                return web.json_response({'error': '内容不能为空'}, status=400)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'locked_memory', params.get('persona_user'))
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'locked_memory')

            file_data = self._read_json_file(file_path, {"locked_items": []})
            if isinstance(file_data, dict) and "locked_items" in file_data:
                locked = file_data["locked_items"]
            elif isinstance(file_data, list):
                locked = file_data
            else:
                locked = []

            found = False
            for item in locked:
                if item.get('id') == index:
                    item['content'] = new_content
                    item['edited_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    found = True
                    break

            if not found:
                return web.json_response({'error': '未找到指定的锁定记忆'}, status=404)

            save_data = {'locked_items': locked, 'last_updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            self._write_json_file(file_path, save_data)

            return web.json_response({'success': True, 'message': '锁定记忆已更新'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_locked_memory(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            index = int(request.match_info['index'])

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'locked_memory', params.get('persona_user'))
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'locked_memory')

            file_data = self._read_json_file(file_path, {"locked_items": []})
            if isinstance(file_data, dict) and "locked_items" in file_data:
                locked = file_data["locked_items"]
            elif isinstance(file_data, list):
                locked = file_data
            else:
                locked = []

            locked = [item for item in locked if item.get('id') != index]

            save_data = {'locked_items': locked, 'last_updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            self._write_json_file(file_path, save_data)

            return web.json_response({'success': True, 'message': '锁定记忆已删除'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_clear_locked_memories(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'locked_memory', params.get('persona_user'))
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'locked_memory')

            if os.path.exists(file_path):
                os.remove(file_path)

            return web.json_response({'success': True, 'message': '所有锁定记忆已清空'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 完整对话记录操作 ==========

    async def handle_get_full_log(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'content': '', 'line_count': 0, 'conversation_count': 0, 'summary_offset': 0})

                log_path = self._get_persona_path(params['role_name'], params['space_name'], 'full_log')
                round_path = self._get_persona_path(params['role_name'], params['space_name'], 'summary_round')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'content': '', 'line_count': 0, 'conversation_count': 0, 'summary_offset': 0})

                log_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'full_log')
                round_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'offset')

            content = ''
            if os.path.exists(log_path):
                with open(log_path, 'r', encoding='utf-8') as f:
                    content = f.read()

            line_count = len(content.strip().split('\n')) if content.strip() else 0

            summary_round = 0
            if os.path.exists(round_path):
                try:
                    data = self._read_json_file(round_path, {})
                    summary_round = data.get('round', 0)
                except:
                    pass

            return web.json_response({
                'content': content,
                'line_count': line_count,
                'conversation_count': line_count // 2,
                'summary_offset': summary_round
            })
        except Exception as e:
            return web.json_response({'content': '', 'line_count': 0, 'conversation_count': 0, 'summary_offset': 0})

    async def handle_update_full_log(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)
            data = await request.json()
            content = data.get('content', '')

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_persona_path(params['role_name'], params['space_name'], 'full_log')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                file_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'full_log')

            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)

            return web.json_response({'success': True, 'message': '对话记录已更新'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_full_log(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        # 用户权限校验
        auth = self._get_auth_info(request)
        if auth and auth.get('login_type') == 'user':
            params = self._get_session_params(request)
            if params.get('mode') == 'session' and params.get('user_id') and params.get('user_id') != auth.get('qq_number'):
                return web.json_response({'error': '无权访问此数据'}, status=403)

        try:
            params = self._get_session_params(request)

            if params['mode'] == 'persona':
                if not params['role_name'] or not params['space_name']:
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                log_path = self._get_persona_path(params['role_name'], params['space_name'], 'full_log')
                round_path = self._get_persona_path(params['role_name'], params['space_name'], 'summary_round')
            else:
                if not all([params['user_id'], params['session_id'], params['role_name']]):
                    return web.json_response({'error': '缺少必要参数'}, status=400)
                log_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'full_log')
                round_path = self._get_session_path(params['user_id'], params['session_id'], params['role_name'], 'offset')

            if os.path.exists(log_path):
                os.remove(log_path)
            if os.path.exists(round_path):
                os.remove(round_path)

            return web.json_response({'success': True, 'message': '完整对话记录已清空'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 记忆迁移 ==========

    async def handle_migrate_memory(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()
            memory_type = data.get('memory_type', '')
            memory_content = data.get('content', {})

            source_mode = data.get('source_mode', 'session')
            source_user_id = data.get('source_user_id', '')
            source_session_id = data.get('source_session_id', '')
            source_role_name = data.get('source_role_name', '')
            source_space_name = data.get('source_space_name', '')

            target_mode = data.get('target_mode', 'session')
            target_user_id = data.get('target_user_id', '')
            target_session_id = data.get('target_session_id', '')
            target_role_name = data.get('target_role_name', '')
            target_space_name = data.get('target_space_name', '')
            target_persona_user = data.get('target_persona_user', '')

            if not memory_type or not memory_content:
                return web.json_response({'error': '缺少记忆类型或内容'}, status=400)

            memory_content['migrated_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if source_mode == 'persona':
                memory_content['migrated_from'] = f"persona_{source_role_name}_{source_space_name}"
            else:
                memory_content['migrated_from'] = f"session_{source_user_id}_{source_session_id}"

            if memory_type == 'diary':
                if target_mode == 'persona':
                    file_path = self._get_persona_path(target_role_name, target_space_name, 'diary')
                else:
                    file_path = self._get_session_path(target_user_id, target_session_id, target_role_name, 'diary')
                target_list = self._read_json_file(file_path, [])
                target_list.append(memory_content)
                self._write_json_file(file_path, target_list)

            elif memory_type == 'plot_summary':
                if target_mode == 'persona':
                    file_path = self._get_persona_path(target_role_name, target_space_name, 'plot_summaries')
                else:
                    file_path = self._get_session_path(target_user_id, target_session_id, target_role_name, 'plot_summaries')
                target_list = self._read_json_file(file_path, [])
                target_list.append(memory_content)
                self._write_json_file(file_path, target_list)

            elif memory_type == 'locked_memory':
                if target_mode == 'persona':
                    file_path = self._get_persona_path(target_role_name, target_space_name, 'locked_memory', target_persona_user)
                else:
                    file_path = self._get_session_path(target_user_id, target_session_id, target_role_name, 'locked_memory')

                file_data = self._read_json_file(file_path, {"locked_items": []})
                if isinstance(file_data, dict) and 'locked_items' in file_data:
                    target_list = file_data['locked_items']
                elif isinstance(file_data, list):
                    target_list = file_data
                else:
                    target_list = []

                existing_contents = {item.get('content') for item in target_list}
                if memory_content.get('content') not in existing_contents:
                    memory_content['id'] = len(target_list) + 1
                    target_list.append(memory_content)
                    save_data = {'locked_items': target_list, 'last_updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    self._write_json_file(file_path, save_data)
                else:
                    return web.json_response({'error': '目标位置已存在相同内容'}, status=400)

            return web.json_response({'success': True, 'message': '记忆已迁移'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_batch_memory_preview(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()
            memory_type = data.get('memory_type', '')
            content = str(data.get('content', '') or '').strip()
            include_session = bool(data.get('include_session', True))
            include_persona = bool(data.get('include_persona', True))

            if memory_type not in ('core', 'locked'):
                return web.json_response({'error': '无效的记忆类型'}, status=400)
            if not content:
                return web.json_response({'error': '内容不能为空'}, status=400)
            if not include_session and not include_persona:
                return web.json_response({'error': '至少选择一种目标范围'}, status=400)

            targets = self._get_batch_memory_targets(include_session, include_persona)
            preview = self._preview_batch_memory(memory_type, content, targets)
            return web.json_response({'success': True, 'preview': preview})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_batch_memory_execute(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()
            memory_type = data.get('memory_type', '')
            content = str(data.get('content', '') or '').strip()
            include_session = bool(data.get('include_session', True))
            include_persona = bool(data.get('include_persona', True))
            confirm = bool(data.get('confirm', False))

            if memory_type not in ('core', 'locked'):
                return web.json_response({'error': '无效的记忆类型'}, status=400)
            if not content:
                return web.json_response({'error': '内容不能为空'}, status=400)
            if not include_session and not include_persona:
                return web.json_response({'error': '至少选择一种目标范围'}, status=400)
            if not confirm:
                return web.json_response({'error': '缺少确认标记'}, status=400)

            targets = self._get_batch_memory_targets(include_session, include_persona)
            result = {
                'total': len(targets),
                'added': 0,
                'duplicates': 0,
                'errors': []
            }

            for target in targets:
                try:
                    if memory_type == 'core':
                        changed = self._append_core_memory_file(target['core_path'], content)
                    else:
                        changed = self._append_locked_memory_file(target['locked_path'], content)

                    if changed:
                        result['added'] += 1
                    else:
                        result['duplicates'] += 1
                except Exception as e:
                    result['errors'].append({'target': target['label'], 'error': str(e)})

            return web.json_response({'success': True, 'result': result})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 配置操作 ==========

    async def handle_get_config(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            config = {
                'auto_cleanup_enabled': self.config.get('auto_cleanup_enabled', False),
                'max_core_memory_history': self.config.get('max_core_memory_history', 100),
                'max_diary_entries': self.config.get('max_diary_entries', 1000),
                'max_plot_summaries': self.config.get('max_plot_summaries', 200)
            }
            return web.json_response(config)
        except Exception as e:
            return web.json_response({'auto_cleanup_enabled': False})

    async def handle_save_config(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()

            if 'auto_cleanup_enabled' in data:
                self.config['auto_cleanup_enabled'] = bool(data['auto_cleanup_enabled'])
            if 'max_core_memory_history' in data:
                self.config['max_core_memory_history'] = int(data['max_core_memory_history'])
            if 'max_diary_entries' in data:
                self.config['max_diary_entries'] = int(data['max_diary_entries'])
            if 'max_plot_summaries' in data:
                self.config['max_plot_summaries'] = int(data['max_plot_summaries'])

            if hasattr(self.config, 'save_config'):
                self.config.save_config()

            return web.json_response({'success': True})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 清理操作 ==========

    async def handle_cleanup_preview(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()
            user_id = data.get('user_id')
            session_id = data.get('session_id')
            role_name = data.get('role_name')

            if not all([user_id, session_id, role_name]):
                return web.json_response({'error': '缺少必要参数'}, status=400)

            max_core_history = self.config.get('max_core_memory_history', 100)
            max_diaries = self.config.get('max_diary_entries', 1000)
            max_summaries = self.config.get('max_plot_summaries', 200)

            preview = {'core_memory_history': 0, 'diaries': 0, 'plot_summaries': 0}

            history_dir = self._get_session_history_dir(user_id, session_id, role_name)
            if os.path.exists(history_dir):
                history_count = len([f for f in os.listdir(history_dir) if f.endswith('.json')])
                if history_count > max_core_history:
                    preview['core_memory_history'] = history_count - max_core_history

            diary_path = self._get_session_path(user_id, session_id, role_name, 'diary')
            diaries = self._read_json_file(diary_path, [])
            if len(diaries) > max_diaries:
                preview['diaries'] = len(diaries) - max_diaries

            summaries_path = self._get_session_path(user_id, session_id, role_name, 'plot_summaries')
            summaries = self._read_json_file(summaries_path, [])
            if len(summaries) > max_summaries:
                preview['plot_summaries'] = len(summaries) - max_summaries

            return web.json_response({'preview': preview})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_cleanup(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()
            user_id = data.get('user_id')
            session_id = data.get('session_id')
            role_name = data.get('role_name')
            confirm = data.get('confirm', False)

            if not confirm:
                return web.json_response({'error': '需要确认才能执行清理'}, status=400)

            if not all([user_id, session_id, role_name]):
                return web.json_response({'error': '缺少必要参数'}, status=400)

            result = {'core_memory_history_deleted': 0, 'diaries_deleted': 0, 'plot_summaries_deleted': 0}

            max_core_history = self.config.get('max_core_memory_history', 100)
            max_diaries = self.config.get('max_diary_entries', 1000)
            max_summaries = self.config.get('max_plot_summaries', 200)

            # 清理核心记忆历史
            history_dir = self._get_session_history_dir(user_id, session_id, role_name)
            if os.path.exists(history_dir):
                files = sorted([f for f in os.listdir(history_dir) if f.endswith('.json')], reverse=True)
                if len(files) > max_core_history:
                    for f in files[max_core_history:]:
                        try:
                            os.remove(os.path.join(history_dir, f))
                            result['core_memory_history_deleted'] += 1
                        except:
                            pass

            # 清理记忆日志
            diary_path = self._get_session_path(user_id, session_id, role_name, 'diary')
            diaries = self._read_json_file(diary_path, [])
            if len(diaries) > max_diaries:
                result['diaries_deleted'] = len(diaries) - max_diaries
                self._write_json_file(diary_path, diaries[-max_diaries:])

            # 清理剧情总结
            summaries_path = self._get_session_path(user_id, session_id, role_name, 'plot_summaries')
            summaries = self._read_json_file(summaries_path, [])
            if len(summaries) > max_summaries:
                result['plot_summaries_deleted'] = len(summaries) - max_summaries
                self._write_json_file(summaries_path, summaries[-max_summaries:])

            return web.json_response({'success': True, 'result': result})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== Persona模式操作 ==========

    async def handle_create_persona_space(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()
            role_name = data.get('role_name', '')
            space_name = data.get('space_name', '')

            if not role_name or not space_name:
                return web.json_response({'error': '角色名和空间名不能为空'}, status=400)

            role_name = role_name.replace('.md', '').replace('.txt', '')

            result = self.memory_manager.create_empty_persona(role_name, space_name)

            if result['success']:
                return web.json_response({'success': True, 'message': result['message']})
            else:
                return web.json_response({'error': result.get('message', '创建失败')}, status=400)
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_migrate_to_persona(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()
            user_id = data.get('user_id', '')
            session_id = data.get('session_id', '')
            role_name = data.get('role_name', '')
            space_name = data.get('space_name', '')

            if not all([user_id, session_id, role_name, space_name]):
                return web.json_response({'error': '缺少必要参数'}, status=400)

            role_name = role_name.replace('.md', '').replace('.txt', '')

            result = self.memory_manager.migrate_session_to_persona(user_id, session_id, role_name, space_name)

            return web.json_response({
                'success': result['success'],
                'actions': result.get('actions', []),
                'warnings': result.get('warnings', []),
                'errors': result.get('errors', [])
            })
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 快捷命令 ==========

    async def handle_cmd_memory_summary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)

        try:
            data = await request.json()
            task_id = str(uuid.uuid4())
            self.running_tasks[task_id] = {'type': 'memory_summary', 'status': 'running', 'message': '正在执行记忆总结...'}
            asyncio.create_task(self._run_memory_summary(task_id, data))
            return web.json_response({'success': True, 'task_id': task_id, 'message': '记忆总结任务已启动'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def _run_memory_summary(self, task_id, data):
        try:
            user_id = data.get('user_id')
            session_id = data.get('session_id')
            role_name = data.get('role_name')

            if self.plugin_instance and hasattr(self.plugin_instance, 'summarize_and_save'):
                success, result_data = await self.plugin_instance.summarize_and_save(user_id, role_name, session_id, None, force=True)
                if success:
                    self.running_tasks[task_id] = {'status': 'completed', 'message': '记忆总结完成', 'result': result_data}
                else:
                    self.running_tasks[task_id] = {'status': 'failed', 'message': '记忆总结失败'}
            else:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '插件实例不可用'}
        except Exception as e:
            self.running_tasks[task_id] = {'status': 'failed', 'message': f'执行出错: {str(e)}'}

    async def handle_cmd_plot_summary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)
        try:
            data = await request.json()
            task_id = str(uuid.uuid4())
            self.running_tasks[task_id] = {'status': 'running', 'message': '正在执行剧情总结...'}
            asyncio.create_task(self._run_plot_summary(task_id, data))
            return web.json_response({'success': True, 'task_id': task_id, 'message': '剧情总结任务已启动'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def _run_plot_summary(self, task_id, data):
        try:
            user_id = data.get('user_id')
            session_id = data.get('session_id')
            role_name = data.get('role_name')

            if self.plugin_instance and hasattr(self.plugin_instance, '_summarize_plot_framework'):
                success, result_content = await self.plugin_instance._summarize_plot_framework(user_id, role_name, session_id, None)
                if success:
                    self.running_tasks[task_id] = {'status': 'completed', 'message': '剧情总结完成'}
                else:
                    self.running_tasks[task_id] = {'status': 'failed', 'message': '剧情总结失败'}
            else:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '插件实例不可用'}
        except Exception as e:
            self.running_tasks[task_id] = {'status': 'failed', 'message': f'执行出错: {str(e)}'}

    async def handle_cmd_regenerate_framework(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)
        try:
            data = await request.json()
            task_id = str(uuid.uuid4())
            self.running_tasks[task_id] = {'status': 'running', 'message': '正在重新生成剧情框架...'}
            asyncio.create_task(self._run_regenerate_framework(task_id, data))
            return web.json_response({'success': True, 'task_id': task_id, 'message': '重新生成剧情框架任务已启动'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def _run_regenerate_framework(self, task_id, data):
        try:
            user_id = data.get('user_id')
            session_id = data.get('session_id')
            role_name = data.get('role_name')

            if self.plugin_instance and hasattr(self.plugin_instance, '_regenerate_plot_framework'):
                success = await self.plugin_instance._regenerate_plot_framework(user_id, role_name, session_id, None, None)
                if success:
                    self.running_tasks[task_id] = {'status': 'completed', 'message': '剧情框架重新生成完成'}
                else:
                    self.running_tasks[task_id] = {'status': 'failed', 'message': '剧情框架重新生成失败'}
            else:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '插件实例不可用'}
        except Exception as e:
            self.running_tasks[task_id] = {'status': 'failed', 'message': f'执行出错: {str(e)}'}

    async def handle_cmd_summarize_diary(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)
        try:
            data = await request.json()
            params = data
            mode = params.get('mode', 'session')

            if mode == 'persona':
                role_name = params.get('role_name', '').replace('.md', '').replace('.txt', '')
                space_name = params.get('space_name', '')
                if not role_name or not space_name:
                    return web.json_response({'error': '缺少必要参数'}, status=400)

                file_path = self._get_persona_path(role_name, space_name, 'diary')
                all_diaries = self._read_json_file(file_path, [])
            else:
                user_id, session_id, role_name = params.get('user_id'), params.get('session_id'), params.get('role_name')
                file_path = self._get_session_path(user_id, session_id, role_name, 'diary')
                all_diaries = self._read_json_file(file_path, [])

            if not all_diaries:
                return web.json_response({'error': '记忆日志为空'}, status=400)

            diary_texts = []
            for diary in all_diaries:
                timestamp = diary.get('timestamp', '未知时间')
                summary = diary.get('summary', '')
                if summary:
                    diary_texts.append(f"【{timestamp}】\n{summary}")

            full_diary_content = "\n\n---\n\n".join(diary_texts)

            task_id = str(uuid.uuid4())
            self.running_tasks[task_id] = {'status': 'running', 'message': '正在从记忆日志生成剧情框架...'}
            asyncio.create_task(self._run_summarize_diary(task_id, params, full_diary_content))
            return web.json_response({'success': True, 'task_id': task_id, 'message': '总结记忆日志任务已启动'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def _run_summarize_diary(self, task_id, params, full_diary_content):
        try:
            user_id = params.get('user_id')
            session_id = params.get('session_id')
            role_name = params.get('role_name')

            if self.plugin_instance and hasattr(self.plugin_instance, '_summarize_diary_to_framework'):
                success = await self.plugin_instance._summarize_diary_to_framework(user_id, role_name, session_id, None, None, full_diary_content)
                if success:
                    self.running_tasks[task_id] = {'status': 'completed', 'message': '从记忆日志生成剧情框架完成'}
                else:
                    self.running_tasks[task_id] = {'status': 'failed', 'message': '从记忆日志生成剧情框架失败'}
            else:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '插件实例不可用'}
        except Exception as e:
            self.running_tasks[task_id] = {'status': 'failed', 'message': f'执行出错: {str(e)}'}

    async def handle_cmd_compress_framework(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)
        try:
            data = await request.json()
            task_id = str(uuid.uuid4())
            self.running_tasks[task_id] = {'status': 'running', 'message': '正在整理浓缩剧情框架...'}
            asyncio.create_task(self._run_compress_framework(task_id, data))
            return web.json_response({'success': True, 'task_id': task_id, 'message': '整理任务已启动'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def _run_compress_framework(self, task_id, data):
        try:
            mode = data.get('mode', 'session')
            role_name = data.get('role_name', '').replace('.md', '').replace('.txt', '')

            if mode == 'persona':
                space_name = data.get('space_name', '')
                file_path = self._get_persona_path(role_name, space_name, 'plot_framework')
            else:
                user_id = data.get('user_id')
                session_id = data.get('session_id')
                file_path = self._get_session_path(user_id, session_id, role_name, 'plot_framework')

            raw_data = self._read_json_file(file_path, {})

            content = ""
            if isinstance(raw_data, dict):
                content = raw_data.get("content", "")
            elif isinstance(raw_data, list) and raw_data:
                content = raw_data[-1].get("content", "")

            if not content:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '剧情框架为空'}
                return

            persona_content = ""
            if self.plugin_instance:
                try:
                    full_role_name = data.get('role_name', '')
                    persona = await self.plugin_instance.context.persona_manager.get_persona(full_role_name)
                    persona_content = persona.system_prompt if persona else ""
                except:
                    pass

            if self.plugin_instance and hasattr(self.plugin_instance, '_compress_plot_framework'):
                new_content = await self.plugin_instance._compress_plot_framework(content, role_name, persona_content)

                save_data = {'content': new_content, 'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                self._write_json_file(file_path, save_data)

                self.running_tasks[task_id] = {'status': 'completed', 'message': f'整理完成，字数从 {len(content)} 缩减至 {len(new_content)}'}
            else:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '插件实例不可用'}

        except Exception as e:
            self.running_tasks[task_id] = {'status': 'failed', 'message': f'执行出错: {str(e)}'}

    async def handle_cmd_rebuild_vectors(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)
        if not self._is_admin(request):
            return web.json_response({'error': '仅管理员可操作'}, status=403)
        try:
            data = await request.json()
            task_id = str(uuid.uuid4())
            self.running_tasks[task_id] = {'status': 'running', 'message': '正在重建记忆向量索引...'}
            asyncio.create_task(self._run_rebuild_vectors(task_id, data))
            return web.json_response({'success': True, 'task_id': task_id, 'message': '重建向量任务已启动'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def _run_rebuild_vectors(self, task_id, data):
        try:
            mode = data.get('mode', 'session')

            if mode == 'persona':
                role_name = data.get('role_name', '').replace('.md', '').replace('.txt', '')
                space_name = data.get('space_name', '')
                diary_path = self._get_persona_path(role_name, space_name, 'diary')
                vector_path = self._get_persona_path(role_name, space_name, 'diary_vectors')
            else:
                user_id = data.get('user_id')
                session_id = data.get('session_id')
                role_name = data.get('role_name')
                diary_path = self._get_session_path(user_id, session_id, role_name, 'diary')
                vector_path = self._get_session_path(user_id, session_id, role_name, 'diary_vectors')

            all_diaries = self._read_json_file(diary_path, [])

            if not all_diaries:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '没有找到记忆日志'}
                return

            summaries = [diary['summary'] for diary in all_diaries if 'summary' in diary and diary['summary'].strip()]
            if not summaries:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '记忆日志内容为空'}
                return

            if self.plugin_instance and hasattr(self.plugin_instance, 'semantic_searcher'):
                vectors = await self.plugin_instance.semantic_searcher.get_embeddings(summaries)

                if vectors and len(vectors) == len(summaries):
                    self._write_json_file(vector_path, vectors)
                    self.running_tasks[task_id] = {'status': 'completed', 'message': f'已为 {len(vectors)} 条记忆重建向量索引'}
                else:
                    self.running_tasks[task_id] = {'status': 'failed', 'message': '向量生成失败或数量不匹配'}
            else:
                self.running_tasks[task_id] = {'status': 'failed', 'message': '语义搜索模块不可用'}
        except Exception as e:
            self.running_tasks[task_id] = {'status': 'failed', 'message': f'执行出错: {str(e)}'}

    async def handle_get_task_status(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({'error': '未授权'}, status=401)

        task_id = request.query.get('task_id', '')
        if task_id and task_id in self.running_tasks:
            return web.json_response(self.running_tasks[task_id])
        elif task_id:
            return web.json_response({'error': '任务不存在'}, status=404)
        else:
            return web.json_response({'tasks': self.running_tasks})

    # ========== HTML页面 ==========

    async def handle_index(self, request: web.Request) -> web.Response:
        html = self._get_html_template()
        return web.Response(text=html, content_type='text/html', charset='utf-8')

    def _get_html_template(self) -> str:
        return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>核心记忆管理</title>
<style>
:root{--primary-orange:#FF8C42;--primary-orange-dark:#E67332;--primary-blue:#4A90D9;--primary-blue-dark:#3A7BC8;--bg-light:#FFF9F5;--bg-card:#FFFFFF;--text-primary:#2D3748;--text-secondary:#718096;--text-muted:#A0AEC0;--border-color:#E8E8E8;--success:#48BB78;--warning:#ED8936;--danger:#F56565;--shadow-sm:0 1px 3px rgba(0,0,0,0.08);--shadow-md:0 4px 12px rgba(0,0,0,0.1);--shadow-lg:0 8px 25px rgba(0,0,0,0.12);--radius-sm:8px;--radius-md:12px;--radius-lg:16px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Roboto,sans-serif;background:linear-gradient(135deg,var(--bg-light) 0%,#F0F7FF 100%);min-height:100vh;color:var(--text-primary);line-height:1.6}
.login-container{display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.login-box{background:var(--bg-card);padding:48px 40px;border-radius:var(--radius-lg);box-shadow:var(--shadow-lg);width:100%;max-width:420px;text-align:center}
.login-logo{font-size:48px;margin-bottom:16px;color:var(--primary-orange);font-weight:bold}
.login-box h1{font-size:24px;font-weight:600;margin-bottom:8px}
.login-box .subtitle{color:var(--text-secondary);margin-bottom:32px;font-size:14px}
.login-box input{width:100%;padding:14px 18px;border:2px solid var(--border-color);border-radius:var(--radius-sm);font-size:15px;margin-bottom:20px}
.login-box input:focus{border-color:var(--primary-orange);outline:none}
.login-box button{width:100%;padding:14px;background:linear-gradient(135deg,var(--primary-orange),var(--primary-orange-dark));color:white;border:none;border-radius:var(--radius-sm);font-size:16px;cursor:pointer}
.error-msg{color:var(--danger);font-size:14px;margin-bottom:16px;padding:10px;background:#FFF5F5;border-radius:var(--radius-sm);display:none}
.error-msg.show{display:block}
.main-container{display:none;padding:24px;max-width:1600px;margin:0 auto}
.header{background:var(--bg-card);padding:20px 28px;border-radius:var(--radius-lg);margin-bottom:24px;box-shadow:var(--shadow-sm);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px}
.header-left{display:flex;align-items:center;gap:16px}
.header-logo{font-size:32px;color:var(--primary-orange);font-weight:bold}
.header h1{font-size:22px;font-weight:600}
.header-controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.header select{padding:10px 16px;border:2px solid var(--border-color);border-radius:var(--radius-sm);font-size:14px;min-width:220px}
.btn{padding:10px 18px;border:none;border-radius:var(--radius-sm);font-size:14px;font-weight:500;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:all 0.2s}
.btn:hover{opacity:0.9;transform:translateY(-1px)}
.btn-primary{background:linear-gradient(135deg,var(--primary-blue),var(--primary-blue-dark));color:white}
.btn-orange{background:linear-gradient(135deg,var(--primary-orange),var(--primary-orange-dark));color:white}
.btn-danger{background:var(--danger);color:white}
.btn-secondary{background:#F7FAFC;color:var(--text-primary);border:1px solid var(--border-color)}
.btn-sm{padding:6px 12px;font-size:13px}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px}
.stat-card{background:var(--bg-card);padding:20px;border-radius:var(--radius-md);box-shadow:var(--shadow-sm);border-left:4px solid var(--primary-orange)}
.stat-card:nth-child(2){border-left-color:var(--primary-blue)}
.stat-card:nth-child(3){border-left-color:var(--success)}
.stat-card:nth-child(4){border-left-color:var(--warning)}
.stat-card h3{color:var(--text-secondary);font-size:13px;margin-bottom:8px}
.stat-card .value{font-size:28px;font-weight:700}
.stat-card .sub{color:var(--text-muted);font-size:12px;margin-top:4px}
.content-wrapper{display:grid;grid-template-columns:240px 1fr;gap:24px}
.sidebar{background:var(--bg-card);border-radius:var(--radius-lg);padding:20px 16px;box-shadow:var(--shadow-sm);height:fit-content;position:sticky;top:24px}
.sidebar h3{font-size:12px;text-transform:uppercase;color:var(--text-muted);margin-bottom:12px;padding:0 12px}
.nav-item{display:flex;align-items:center;padding:12px 16px;border-radius:var(--radius-sm);cursor:pointer;margin-bottom:4px;font-size:14px;color:var(--text-secondary);transition:all 0.2s}
.nav-item:hover{background:#FFF5EE;color:var(--primary-orange)}
.nav-item.active{background:linear-gradient(135deg,var(--primary-orange),var(--primary-orange-dark));color:white}
.nav-divider{height:1px;background:var(--border-color);margin:16px 0}
.main-panel{background:var(--bg-card);border-radius:var(--radius-lg);box-shadow:var(--shadow-sm);overflow:hidden}
.panel-header{padding:20px 24px;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
.panel-header h2{font-size:18px;font-weight:600}
.panel-actions{display:flex;gap:8px;flex-wrap:wrap}
.panel-content{padding:24px;display:none}
.panel-content.active{display:block}
.content-editor{background:#FAFAFA;border:1px solid var(--border-color);border-radius:var(--radius-sm)}
.content-editor-header{padding:12px 16px;background:white;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between}
.content-editor-header .timestamp{font-size:13px;color:var(--text-muted)}
.content-editor textarea{width:100%;min-height:300px;padding:16px;border:none;font-size:14px;line-height:1.8;resize:vertical;font-family:inherit}
.content-editor textarea:focus{outline:none}
.list-container{display:flex;flex-direction:column;gap:12px}
.list-item{background:#FAFAFA;border:1px solid var(--border-color);border-radius:var(--radius-sm);padding:16px;transition:all 0.2s}
.list-item:hover{border-color:var(--primary-orange);box-shadow:var(--shadow-sm)}
.list-item-header{display:flex;justify-content:space-between;margin-bottom:10px}
.list-item-meta{display:flex;align-items:center;gap:12px}
.list-item-time{font-size:13px;color:var(--text-muted)}
.list-item-index{background:var(--primary-blue);color:white;font-size:11px;padding:2px 8px;border-radius:10px}
.list-item-actions{display:flex;gap:6px}
.list-item-content{font-size:14px;line-height:1.7;white-space:pre-wrap}
.list-item-content.editing textarea{width:100%;min-height:100px;padding:12px;border:1px solid var(--primary-blue);border-radius:var(--radius-sm);font-size:14px}
.history-container{display:grid;grid-template-columns:280px 1fr;gap:20px;min-height:400px}
.history-list{border:1px solid var(--border-color);border-radius:var(--radius-sm);overflow:hidden}
.history-list-header{padding:12px 16px;background:#F7FAFC;border-bottom:1px solid var(--border-color);font-weight:500}
.history-list-content{max-height:400px;overflow-y:auto}
.history-item{padding:14px 16px;border-bottom:1px solid var(--border-color);cursor:pointer;transition:all 0.2s}
.history-item:hover{background:#FFF5EE}
.history-item.selected{background:rgba(255,140,66,0.1);border-left:3px solid var(--primary-orange)}
.history-item-time{font-weight:500;font-size:13px;margin-bottom:4px}
.history-item-preview{font-size:12px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.history-detail{border:1px solid var(--border-color);border-radius:var(--radius-sm);overflow:hidden}
.history-detail-header{padding:12px 16px;background:#F7FAFC;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between;align-items:center}
.history-detail-content{padding:16px;max-height:400px;overflow-y:auto;white-space:pre-wrap;font-size:14px;line-height:1.7}
.locked-item{display:flex;gap:12px;padding:14px 16px;background:#FFFBF5;border:1px solid #FFE4CC;border-radius:var(--radius-sm);margin-bottom:10px}
.locked-item-id{background:var(--primary-orange);color:white;font-size:12px;padding:4px 10px;border-radius:12px;height:fit-content}
.locked-item-content{flex:1;font-size:14px}
.locked-item-time{font-size:11px;color:var(--text-muted);margin-top:6px}
.locked-item-actions{display:flex;gap:6px;align-items:flex-start}
.add-locked-form{display:flex;gap:12px;margin-bottom:20px}
.add-locked-form input{flex:1;padding:12px 16px;border:2px solid var(--border-color);border-radius:var(--radius-sm);font-size:14px}
.add-locked-form input:focus{border-color:var(--primary-orange);outline:none}
.command-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.command-card{background:#FAFAFA;border:1px solid var(--border-color);border-radius:var(--radius-md);padding:20px;transition:all 0.2s}
.command-card:hover{border-color:var(--primary-blue);box-shadow:var(--shadow-sm)}
.command-card h4{font-size:15px;margin-bottom:8px}
.command-card p{font-size:13px;color:var(--text-secondary);margin-bottom:16px}
.command-card .btn{width:100%;justify-content:center}
.settings-section{margin-bottom:24px}
.settings-section h3{font-size:15px;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid var(--border-color)}
.setting-item{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;background:#FAFAFA;border-radius:var(--radius-sm);margin-bottom:10px}
.setting-item label{font-size:14px}
.setting-item input[type="number"]{width:100px;padding:8px 12px;border:1px solid var(--border-color);border-radius:var(--radius-sm);text-align:center}
.switch{position:relative;width:48px;height:26px}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#CBD5E0;border-radius:26px;transition:0.3s}
.slider:before{position:absolute;content:"";height:20px;width:20px;left:3px;bottom:3px;background:white;border-radius:50%;transition:0.3s}
input:checked+.slider{background:var(--primary-orange)}
input:checked+.slider:before{transform:translateX(22px)}
.pagination{display:flex;justify-content:center;gap:8px;margin-top:24px;padding-top:20px;border-top:1px solid var(--border-color)}
.pagination button{padding:8px 14px;border:1px solid var(--border-color);background:white;border-radius:var(--radius-sm);cursor:pointer;transition:all 0.2s}
.pagination button:hover:not(:disabled){border-color:var(--primary-blue);color:var(--primary-blue)}
.pagination button:disabled{opacity:0.5;cursor:not-allowed}
.pagination .current{background:var(--primary-blue);color:white;border-color:var(--primary-blue)}
.tabs{display:flex;gap:4px;margin-bottom:20px;background:#F7FAFC;padding:4px;border-radius:var(--radius-sm)}
.tab{padding:10px 20px;border:none;background:transparent;cursor:pointer;border-radius:var(--radius-sm);font-size:14px;color:var(--text-secondary);transition:all 0.2s}
.tab:hover{color:var(--text-primary)}
.tab.active{background:white;color:var(--primary-orange);box-shadow:var(--shadow-sm)}
.persona-section{margin-top:20px;padding:20px;background:#F0F7FF;border-radius:var(--radius-md);border:1px solid #BEE3F8}
.persona-section h4{color:var(--primary-blue);margin-bottom:12px}
.persona-form{display:grid;gap:12px}
.persona-form .form-row{display:flex;gap:12px;align-items:center}
.persona-form select,.persona-form input{padding:10px 14px;border:1px solid var(--border-color);border-radius:var(--radius-sm);font-size:14px;flex:1}
.persona-form select:focus,.persona-form input:focus{border-color:var(--primary-blue);outline:none}
.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.4);z-index:1000;justify-content:center;align-items:center;padding:20px}
.modal.active{display:flex}
.modal-content{background:white;border-radius:var(--radius-lg);width:100%;max-width:560px;max-height:90vh;overflow-y:auto}
.modal-header{padding:20px 24px;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between;align-items:center}
.modal-header h2{font-size:18px}
.modal-close{background:none;border:none;font-size:24px;cursor:pointer;color:var(--text-muted);transition:color 0.2s}
.modal-close:hover{color:var(--text-primary)}
.modal-body{padding:24px}
.modal-footer{padding:16px 24px;border-top:1px solid var(--border-color);display:flex;justify-content:flex-end;gap:10px}
.alert{padding:14px 18px;border-radius:var(--radius-sm);margin-bottom:16px;font-size:14px}
.alert-warning{background:#FFFAF0;border:1px solid #FBD38D;color:#C05621}
.alert-danger{background:#FFF5F5;border:1px solid #FEB2B2;color:#C53030}
.alert-info{background:#EBF8FF;border:1px solid #90CDF4;color:#2B6CB0}
.alert-success{background:#F0FFF4;border:1px solid #9AE6B4;color:#276749}
.loading{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(255,255,255,0.9);z-index:999;justify-content:center;align-items:center;flex-direction:column;gap:16px}
.loading.active{display:flex}
.spinner{width:48px;height:48px;border:4px solid #F0F0F0;border-top:4px solid var(--primary-orange);border-radius:50%;animation:spin 0.8s linear infinite}
@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
.toast{position:fixed;bottom:30px;left:50%;transform:translateX(-50%) translateY(100px);background:var(--text-primary);color:white;padding:14px 28px;border-radius:var(--radius-sm);z-index:2000;opacity:0;transition:all 0.3s}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.success{background:var(--success)}
.toast.error{background:var(--danger)}
.empty-state{text-align:center;padding:60px 20px;color:var(--text-muted)}
.empty-state p{margin-bottom:8px}
.task-status{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:20px;font-size:13px;margin-bottom:16px}
.task-status.running{background:#EBF8FF;color:var(--primary-blue)}
.task-status.completed{background:#F0FFF4;color:var(--success)}
.task-status.failed{background:#FFF5F5;color:var(--danger)}
.task-status .dot{width:8px;height:8px;border-radius:50%;background:currentColor;animation:pulse 1.5s infinite}
.task-status.completed .dot,.task-status.failed .dot{animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.persona-list{display:grid;gap:12px;margin-top:16px}
.persona-card{background:white;border:1px solid var(--border-color);border-radius:var(--radius-sm);padding:16px;display:flex;justify-content:space-between;align-items:center}
.persona-card-info h5{font-size:15px;margin-bottom:4px}
.persona-card-info p{font-size:13px;color:var(--text-muted)}
.persona-card-meta{text-align:right;font-size:12px;color:var(--text-muted)}
.user-select-row{display:flex;align-items:center;gap:12px;margin-bottom:16px;padding:12px 16px;background:#F0F7FF;border-radius:var(--radius-sm);border:1px solid #BEE3F8}
.user-select-row label{font-size:14px;color:var(--primary-blue);font-weight:500}
.user-select-row select{flex:1;max-width:300px;padding:8px 12px;border:1px solid var(--border-color);border-radius:var(--radius-sm)}
.mode-badge{display:inline-block;padding:4px 10px;border-radius:12px;font-size:12px;font-weight:500;margin-left:8px}
.mode-badge.session{background:#E8F5E9;color:#2E7D32}
.mode-badge.persona{background:#E3F2FD;color:#1565C0}
@media(max-width:1024px){.content-wrapper{grid-template-columns:1fr}.sidebar{position:static;display:flex;flex-wrap:wrap;gap:8px;padding:16px}.sidebar h3{width:100%}.nav-divider{display:none}.nav-item{flex:1;min-width:120px;justify-content:center}.history-container{grid-template-columns:1fr}}
@media(max-width:768px){.header{flex-direction:column;align-items:stretch}.header-controls{width:100%;flex-direction:column}.header select{width:100%}.stats-grid{grid-template-columns:1fr 1fr}.command-grid{grid-template-columns:1fr}.add-locked-form{flex-direction:column}.persona-form .form-row{flex-direction:column}}
.login-tabs{display:flex;gap:0;margin-bottom:24px;background:#F7FAFC;border-radius:var(--radius-sm);overflow:hidden}
.login-tab{flex:1;padding:12px;border:none;background:transparent;cursor:pointer;font-size:14px;font-weight:500;color:var(--text-secondary);transition:all 0.2s}
.login-tab.active{background:var(--primary-orange);color:white}
.login-tab:first-child{border-radius:var(--radius-sm) 0 0 var(--radius-sm)}
.login-tab:last-child{border-radius:0 var(--radius-sm) var(--radius-sm) 0}
.login-field{margin-bottom:16px}
.login-field input{width:100%;padding:14px 18px;border:2px solid var(--border-color);border-radius:var(--radius-sm);font-size:15px}
.login-field input:focus{border-color:var(--primary-orange);outline:none}
.login-field label{display:block;text-align:left;font-size:13px;color:var(--text-secondary);margin-bottom:6px}
.user-info-bar{background:linear-gradient(135deg,#EBF8FF,#F0F7FF);padding:12px 20px;border-radius:var(--radius-sm);margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;border:1px solid #BEE3F8}
.user-info-bar .user-info-left{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:500;color:var(--primary-blue)}
.user-info-bar .user-info-icon{font-size:18px}
.user-info-bar .user-badge{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:500;color:var(--primary-blue)}
.user-info-bar .user-actions{display:flex;gap:8px}
.pwd-modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:2000;justify-content:center;align-items:center;padding:20px}
.pwd-modal-overlay.active{display:flex}
.pwd-modal{background:white;border-radius:var(--radius-lg);width:100%;max-width:400px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,0.2)}
.pwd-modal-header{padding:24px 24px 0;text-align:center}
.pwd-modal-header h2{font-size:20px;margin-bottom:8px}
.pwd-modal-header p{font-size:13px;color:var(--text-secondary)}
.pwd-modal-body{padding:20px 24px}
.pwd-modal-body .pwd-field{margin-bottom:14px}
.pwd-modal-body .pwd-field label{display:block;font-size:13px;color:var(--text-secondary);margin-bottom:6px}
.pwd-modal-body .pwd-field input{width:100%;padding:12px 14px;border:2px solid var(--border-color);border-radius:var(--radius-sm);font-size:14px}
.pwd-modal-body .pwd-field input:focus{border-color:var(--primary-orange);outline:none}
.pwd-modal-footer{padding:16px 24px 24px;display:flex;gap:10px}
.pwd-modal-footer button{flex:1;padding:12px;border:none;border-radius:var(--radius-sm);font-size:14px;font-weight:500;cursor:pointer}
.pwd-modal-footer .btn-skip{background:#F7FAFC;color:var(--text-secondary);border:1px solid var(--border-color)}
.pwd-modal-footer .btn-confirm{background:linear-gradient(135deg,var(--primary-orange),var(--primary-orange-dark));color:white}
.pwd-msg{font-size:13px;padding:8px 12px;border-radius:var(--radius-sm);margin-bottom:12px;display:none}
.pwd-msg.show{display:block}
.pwd-msg.error{background:#FFF5F5;color:var(--danger)}
.pwd-msg.success{background:#F0FFF4;color:var(--success)}
@media(max-width:480px){.login-box{padding:32px 20px}.login-logo{font-size:36px}.login-box h1{font-size:20px}.header-logo{font-size:24px}.header h1{font-size:18px}.stat-card .value{font-size:22px}.panel-header{padding:14px 16px}.panel-header h2{font-size:16px}.panel-content>div{padding:16px !important}.sidebar .nav-item{padding:10px 12px;font-size:13px}.content-editor textarea{min-height:200px;font-size:13px}.user-info-bar{flex-direction:column;align-items:flex-start}}
</style>
</head>
<body>
<div class="login-container" id="loginContainer">
<div class="login-box">
<div class="login-logo">M</div>
<h1>核心记忆管理</h1>
<p class="subtitle">查看和管理AI角色的记忆数据</p>
<div class="login-tabs">
<button class="login-tab active" id="loginTabUser" onclick="switchLoginTab('user')">用户登录</button>
<button class="login-tab" id="loginTabAdmin" onclick="switchLoginTab('admin')">管理员登录</button>
</div>
<div class="error-msg" id="loginError"></div>
<div id="loginUserFields">
<div class="login-field"><label>QQ号</label><input type="text" id="qqInput" placeholder="请输入你的QQ号" inputmode="numeric"></div>
<div class="login-field"><label>密码</label><input type="password" id="passwordInput" placeholder="请输入密码（默认123456）"></div>
</div>
<div id="loginAdminFields" style="display:none">
<div class="login-field"><label>管理员密码</label><input type="password" id="adminPasswordInput" placeholder="请输入管理员密码"></div>
</div>
<button id="loginBtn">登录</button>
</div>
</div>
<div class="main-container" id="mainContainer">
<div class="header">
<div class="header-left"><span class="header-logo">M</span><h1>核心记忆管理</h1></div>
<div class="header-controls">
<div class="tabs" id="modeTabs">
<button class="tab active" data-mode="session">会话模式</button>
<button class="tab" data-mode="persona">共享模式</button>
</div>
<select id="sessionSelect"><option value="">-- 请选择记忆会话 --</option></select>
<button class="btn btn-secondary" id="refreshBtn">🔄 刷新</button>
<button class="btn btn-secondary" id="logoutBtn">退出</button>
</div>
</div>
<div class="user-info-bar" id="userInfoBar" style="display:none">
<div class="user-info-left"><span class="user-info-icon">👤</span><span id="userInfoQQ"></span></div>
<button class="btn btn-secondary btn-sm" id="changePwBtn">🔑 修改密码</button>
</div>
<div id="personaUserRow" class="user-select-row" style="display:none">
<label>👤 选择用户查看核心/锁定记忆:</label>
<select id="personaUserSelect"><option value="">-- 共享数据(无用户隔离) --</option></select>
<span style="font-size:12px;color:var(--text-muted)">记忆日志、剧情框架等为共享数据，核心记忆和锁定记忆按用户隔离</span>
</div>
<div class="stats-grid">
<div class="stat-card"><h3>总存储空间</h3><div class="value" id="statTotal">-</div><div class="sub" id="statTotalChars">-</div></div>
<div class="stat-card"><h3>记忆日志</h3><div class="value" id="statDiaries">-</div><div class="sub" id="statDiariesChars">-</div></div>
<div class="stat-card"><h3>剧情总结</h3><div class="value" id="statSummaries">-</div><div class="sub" id="statSummariesChars">-</div></div>
<div class="stat-card"><h3>锁定记忆</h3><div class="value" id="statLocked">-</div><div class="sub" id="statLockedChars">-</div></div>
</div>
<div class="content-wrapper">
<div class="sidebar">
<h3>记忆管理</h3>
<div class="nav-item active" data-panel="core">📝 核心记忆</div>
<div class="nav-item" data-panel="diaries">📖 记忆日志</div>
<div class="nav-item" data-panel="framework">🎭 剧情框架</div>
<div class="nav-item" data-panel="summaries">📋 剧情总结</div>
<div class="nav-item" data-panel="locked">🔒 锁定记忆</div>
<div class="nav-item" data-panel="fulllog">💬 对话记录</div>
<div class="nav-item" data-panel="history">⏰ 历史版本</div>
<div class="nav-divider admin-only"></div>
<h3 class="admin-only">工具</h3>
<div class="nav-item admin-only" data-panel="commands">⚡ 快捷命令</div>
<div class="nav-item admin-only" data-panel="batch">📌 批量注入</div>
<div class="nav-item admin-only" data-panel="persona">🔗 共享空间</div>
<div class="nav-item admin-only" data-panel="settings">⚙️ 清理设置</div>
</div>
<div class="main-panel">
<div class="panel-content active" id="panel-core">
<div class="panel-header"><h2>📝 核心记忆</h2><div class="panel-actions"><button class="btn btn-primary btn-sm" id="saveCoreBtn">💾 保存修改</button><button class="btn btn-danger btn-sm" id="deleteCoreBtn">🗑️ 清空</button></div></div>
<div style="padding:24px"><div class="content-editor"><div class="content-editor-header"><span>编辑核心记忆内容</span><span class="timestamp" id="coreMemoryTime">-</span></div><textarea id="coreMemoryEditor" placeholder="暂无核心记忆数据"></textarea></div></div>
</div>
<div class="panel-content" id="panel-diaries">
<div class="panel-header"><h2>📖 记忆日志</h2><div class="panel-actions"><button class="btn btn-orange btn-sm" id="addDiaryBtn">➕ 添加</button><button class="btn btn-danger btn-sm" id="clearDiariesBtn">🗑️ 清空全部</button></div></div>
<div style="padding:24px"><div class="list-container" id="diariesList"></div><div class="pagination" id="diariesPagination"></div></div>
</div>
<div class="panel-content" id="panel-framework">
<div class="panel-header"><h2>🎭 剧情框架</h2><div class="panel-actions"><button class="btn btn-primary btn-sm" id="saveFrameworkBtn">💾 保存修改</button><button class="btn btn-danger btn-sm" id="deleteFrameworkBtn">🗑️ 清空</button></div></div>
<div style="padding:24px"><div class="content-editor"><div class="content-editor-header"><span>编辑剧情框架内容</span><span class="timestamp" id="frameworkTime">-</span></div><textarea id="frameworkEditor" placeholder="暂无剧情框架数据"></textarea></div></div>
</div>
<div class="panel-content" id="panel-summaries">
<div class="panel-header"><h2>📋 剧情总结</h2><div class="panel-actions"><button class="btn btn-orange btn-sm" id="addSummaryBtn">➕ 添加</button><button class="btn btn-danger btn-sm" id="clearSummariesBtn">🗑️ 清空全部</button></div></div>
<div style="padding:24px"><div class="list-container" id="summariesList"></div><div class="pagination" id="summariesPagination"></div></div>
</div>
<div class="panel-content" id="panel-locked">
<div class="panel-header"><h2>🔒 锁定记忆</h2><div class="panel-actions"><button class="btn btn-danger btn-sm" id="clearLockedBtn">🗑️ 清空全部</button></div></div>
<div style="padding:24px"><div class="add-locked-form"><input type="text" id="newLockedContent" placeholder="输入要锁定的记忆内容..."><button class="btn btn-orange" id="addLockedBtn">➕ 添加锁定</button></div><div id="lockedList"></div></div>
</div>
<div class="panel-content" id="panel-fulllog">
<div class="panel-header"><h2>💬 完整对话记录</h2><div class="panel-actions"><button class="btn btn-secondary btn-sm" id="scrollTopBtn">⬆️ 顶部</button><button class="btn btn-secondary btn-sm" id="scrollBottomBtn">⬇️ 底部</button><button class="btn btn-primary btn-sm" id="saveFullLogBtn">💾 保存修改</button><button class="btn btn-danger btn-sm" id="clearFullLogBtn">🗑️ 清空</button></div></div>
<div style="padding:24px"><div class="alert alert-info">📊 对话记录是所有记忆的数据源。记忆总结每30轮触发，剧情总结每150轮触发。<br>当前共 <strong><span id="fullLogCount">0</span></strong> 轮对话，已总结至第 <strong><span id="summaryOffset">0</span></strong> 轮</div><div class="content-editor"><div class="content-editor-header"><span>编辑对话记录</span><span class="timestamp" id="fullLogTime">-</span></div><textarea id="fullLogEditor" placeholder="暂无对话记录" style="min-height:500px"></textarea></div></div>
</div>
<div class="panel-content" id="panel-history">
<div class="panel-header"><h2>⏰ 核心记忆历史版本</h2></div>
<div style="padding:24px"><div class="history-container"><div class="history-list"><div class="history-list-header">📁 版本列表</div><div class="history-list-content" id="historyList"></div></div><div class="history-detail"><div class="history-detail-header"><span id="historyDetailTitle">选择一个版本查看详情</span><button class="btn btn-orange btn-sm" id="restoreBtn" style="display:none">🔄 恢复此版本</button></div><div class="history-detail-content" id="historyDetailContent"><div class="empty-state"><p>👈 点击左侧列表查看历史版本内容</p></div></div></div></div></div>
</div>
<div class="panel-content" id="panel-commands">
<div class="panel-header"><h2>⚡ 快捷命令</h2></div>
<div style="padding:24px">
<div class="alert alert-info">💡 这些命令会在后台异步执行，执行期间可以继续其他操作</div>
<div id="taskStatus"></div>
<div class="command-grid">
<div class="command-card"><h4>📝 记忆总结</h4><p>手动触发一次记忆日志和核心记忆的生成</p><button class="btn btn-primary" id="cmdMemorySummary">执行</button></div>
<div class="command-card"><h4>📋 剧情总结</h4><p>生成一次剧情总结，记录当前故事进展</p><button class="btn btn-primary" id="cmdPlotSummary">执行</button></div>
<div class="command-card"><h4>🔄 重新生成剧情框架</h4><p>从完整对话记录重新生成剧情框架</p><button class="btn btn-orange" id="cmdRegenFramework">执行</button></div>
<div class="command-card"><h4>📖 从日志生成框架</h4><p>读取全部记忆日志生成剧情框架</p><button class="btn btn-orange" id="cmdSummarizeDiary">执行</button></div>
<div class="command-card"><h4>🔍 重建记忆向量</h4><p>为历史记忆日志重新建立语义索引</p><button class="btn btn-primary" id="cmdRebuildVectors">执行</button></div>
<div class="command-card"><h4>✨ 整理剧情框架</h4><p>基于现有内容进行浓缩精简优化</p><button class="btn btn-orange" id="cmdCompressFramework">执行</button></div>
</div>
</div>
</div>
<div class="panel-content" id="panel-batch">
<div class="panel-header"><h2>📌 批量注入记忆</h2></div>
<div style="padding:24px">
<div class="alert alert-warning">⚠️ 管理员工具：将同一条内容追加到所有选中范围的记忆中。执行前请先预览。</div>
<div class="settings-section">
<h3>注入内容</h3>
<div class="setting-item"><label>记忆类型</label><select id="batchMemoryType"><option value="core">核心记忆</option><option value="locked">锁定记忆</option></select></div>
<div style="margin-top:12px"><label style="display:block;margin-bottom:6px;font-weight:500">内容</label><textarea id="batchMemoryContent" rows="7" style="width:100%;padding:10px;border:1px solid var(--border-color);border-radius:var(--radius-sm);resize:vertical" placeholder="输入要统一注入的一条记忆..."></textarea></div>
</div>
<div class="settings-section">
<h3>目标范围</h3>
<div class="setting-item"><label>包含会话模式记忆</label><label class="switch"><input type="checkbox" id="batchIncludeSession" checked><span class="slider"></span></label></div>
<div class="setting-item"><label>包含共享模式用户记忆</label><label class="switch"><input type="checkbox" id="batchIncludePersona" checked><span class="slider"></span></label></div>
</div>
<div id="batchPreviewResult" class="alert alert-info" style="display:none"></div>
<div style="display:flex;gap:12px;margin-top:24px"><button class="btn btn-secondary" id="previewBatchMemoryBtn">👁️ 预览影响范围</button><button class="btn btn-danger" id="executeBatchMemoryBtn">📌 确认批量注入</button></div>
</div>
</div>
<div class="panel-content" id="panel-persona">
<div class="panel-header"><h2>🔗 共享空间管理</h2></div>
<div style="padding:24px">
<div class="alert alert-info">💡 共享空间允许同一角色在多个会话中共享记忆数据。创建空间后，可将现有会话迁移到共享模式。</div>
<div class="persona-section">
<h4>➕ 创建新共享空间</h4>
<div class="persona-form">
<div class="form-row">
<select id="createPersonaRole"><option value="">-- 选择角色 --</option></select>
<input type="text" id="createSpaceName" placeholder="输入空间名称（如：群A冒险）">
<button class="btn btn-orange" id="createPersonaBtn">创建空间</button>
</div>
</div>
</div>
<div class="persona-section" style="background:#FFF5EE;border-color:#FBD38D">
<h4>📦 迁移会话到共享空间</h4>
<div class="persona-form">
<div class="form-row">
<select id="migrateSession"><option value="">-- 选择要迁移的会话 --</option></select>
</div>
<div class="form-row">
<input type="text" id="migrateSpaceName" placeholder="目标空间名称（不存在则自动创建）">
<button class="btn btn-primary" id="migrateBtn">迁移</button>
</div>
</div>
<p style="font-size:12px;color:#C05621;margin-top:12px">⚠️ 首次迁移会复制所有数据，后续加入已有空间只迁移核心记忆和锁定记忆</p>
</div>
<h4 style="margin-top:24px;margin-bottom:16px">📋 现有共享空间</h4>
<div id="personaList" class="persona-list"><div class="empty-state"><p>暂无共享空间</p></div></div>
</div>
</div>
<div class="panel-content" id="panel-settings">
<div class="panel-header"><h2>⚙️ 清理设置</h2></div>
<div style="padding:24px">
<div class="alert alert-warning">⚠️ 清理功能默认关闭，开启后会自动删除超出限制的旧数据</div>
<div class="settings-section"><h3>基本设置</h3><div class="setting-item"><label>启用自动清理功能</label><label class="switch"><input type="checkbox" id="autoCleanupEnabled"><span class="slider"></span></label></div></div>
<div class="settings-section"><h3>保留数量设置</h3><div class="setting-item"><label>核心记忆历史版本保留数量</label><input type="number" id="maxCoreHistory" value="100" min="1"></div><div class="setting-item"><label>记忆日志保留条数</label><input type="number" id="maxDiaries" value="1000" min="1"></div><div class="setting-item"><label>剧情总结保留条数</label><input type="number" id="maxSummaries" value="200" min="1"></div></div>
<div style="display:flex;gap:12px;margin-top:24px"><button class="btn btn-primary" id="saveSettingsBtn">💾 保存设置</button><button class="btn btn-secondary" id="previewCleanupBtn">👁️ 预览清理</button><button class="btn btn-danger" id="executeCleanupBtn">🗑️ 执行清理</button></div>
</div>
</div>
</div>
</div>
</div>

<div class="modal" id="addMemoryModal">
<div class="modal-content">
<div class="modal-header"><h2 id="addMemoryTitle">添加记忆</h2><button class="modal-close" onclick="closeAddMemoryModal()">&times;</button></div>
<div class="modal-body">
<div style="margin-bottom:16px"><label style="display:block;margin-bottom:6px;font-weight:500">时间</label><input type="datetime-local" id="addMemoryTime" style="width:100%;padding:10px;border:1px solid var(--border-color);border-radius:var(--radius-sm)"></div>
<div style="margin-bottom:16px" id="addMemoryUserLabelRow"><label style="display:block;margin-bottom:6px;font-weight:500">用户标识（可选，仅记忆日志）</label><input type="text" id="addMemoryUserLabel" placeholder="如: User[123456:小明]" style="width:100%;padding:10px;border:1px solid var(--border-color);border-radius:var(--radius-sm)"></div>
<div><label style="display:block;margin-bottom:6px;font-weight:500">内容</label><textarea id="addMemoryContent" rows="6" style="width:100%;padding:10px;border:1px solid var(--border-color);border-radius:var(--radius-sm);resize:vertical" placeholder="输入记忆内容..."></textarea></div>
</div>
<div class="modal-footer"><button class="btn btn-secondary" onclick="closeAddMemoryModal()">取消</button><button class="btn btn-orange" id="confirmAddMemoryBtn">添加</button></div>
</div>
</div>

<div class="modal" id="migrateModal">
<div class="modal-content" style="max-width:640px">
<div class="modal-header"><h2>迁移记忆</h2><button class="modal-close" onclick="closeMigrateModal()">&times;</button></div>
<div class="modal-body">
<div class="alert alert-info">📦 将此条记忆复制到其他会话或共享空间</div>
<div id="migratePreview" style="background:#f9f9f9;padding:12px;border-radius:8px;margin-bottom:16px;max-height:100px;overflow-y:auto;font-size:13px"></div>
<div style="margin-bottom:16px"><label style="display:block;margin-bottom:6px;font-weight:500">目标类型</label><select id="migrateTargetMode" style="width:100%;padding:10px;border:1px solid var(--border-color);border-radius:var(--radius-sm)"><option value="session">会话模式</option><option value="persona">共享模式</option></select></div>
<div id="migrateSessionFields"><div style="margin-bottom:16px"><label style="display:block;margin-bottom:6px;font-weight:500">目标会话</label><select id="migrateTargetSession" style="width:100%;padding:10px;border:1px solid var(--border-color);border-radius:var(--radius-sm)"></select></div></div>
<div id="migratePersonaFields" style="display:none"><div style="margin-bottom:16px"><label style="display:block;margin-bottom:6px;font-weight:500">目标共享空间</label><select id="migrateTargetPersona" style="width:100%;padding:10px;border:1px solid var(--border-color);border-radius:var(--radius-sm)"></select></div><div style="margin-bottom:16px" id="migrateUserField"><label style="display:block;margin-bottom:6px;font-weight:500">目标用户（仅锁定记忆需要）</label><input type="text" id="migrateTargetUser" placeholder="输入用户ID" style="width:100%;padding:10px;border:1px solid var(--border-color);border-radius:var(--radius-sm)"></div></div>
</div>
<div class="modal-footer"><button class="btn btn-secondary" onclick="closeMigrateModal()">取消</button><button class="btn btn-primary" id="confirmMigrateBtn">迁移</button></div>
</div>
</div>

<div class="modal" id="confirmModal">
<div class="modal-content">
<div class="modal-header"><h2 id="confirmModalTitle">确认操作</h2><button class="modal-close" id="modalCloseBtn">&times;</button></div>
<div class="modal-body" id="confirmModalBody"></div>
<div class="modal-footer"><button class="btn btn-secondary" id="modalCancelBtn">取消</button><button class="btn btn-danger" id="confirmModalBtn">确认</button></div>
</div>
</div>
<div class="modal" id="changePwGuideModal">
<div class="modal-content" style="max-width:420px">
<div class="modal-header"><h2>🔐 安全提醒</h2><button class="modal-close" onclick="closeChangePasswordGuide()">&times;</button></div>
<div class="modal-body" style="text-align:center">
<div style="font-size:48px;margin-bottom:16px">🔑</div>
<p style="font-size:16px;font-weight:500;margin-bottom:12px">你正在使用默认密码</p>
<p style="color:var(--text-secondary);margin-bottom:24px;font-size:14px">为了保护你的记忆数据安全，<br>建议修改为你自己的密码</p>
<div style="display:flex;gap:12px;justify-content:center">
<button class="btn btn-secondary" id="guidePwSkipBtn">下次再说</button>
<button class="btn btn-orange" id="guidePwChangeBtn">立即修改</button>
</div>
</div>
</div>
</div>
<div class="modal" id="changePwModal">
<div class="modal-content" style="max-width:420px">
<div class="modal-header"><h2>🔑 修改密码</h2><button class="modal-close" onclick="closeChangePasswordModal()">&times;</button></div>
<div class="modal-body">
<div class="error-msg" id="changePwError" style="display:none"></div>
<div class="login-field"><label>原密码</label><input type="password" id="changePwOld" placeholder="请输入原密码"></div>
<div class="login-field"><label>新密码</label><input type="password" id="changePwNew" placeholder="至少4位"></div>
<div class="login-field"><label>确认新密码</label><input type="password" id="changePwConfirm" placeholder="再次输入新密码"></div>
</div>
<div class="modal-footer"><button class="btn btn-secondary" onclick="closeChangePasswordModal()">取消</button><button class="btn btn-orange" id="submitChangePwBtn">确认修改</button></div>
</div>
</div>
<div class="loading" id="loading"><div class="spinner"></div><div class="loading-text" id="loadingText">加载中...</div></div>
<div class="toast" id="toast"></div>
<script>
var authToken=localStorage.getItem("corememory_token")||"";
var loginType=localStorage.getItem("corememory_login_type")||"";
var loginQQ=localStorage.getItem("corememory_login_qq")||"";
var currentLoginTab="user";
var currentSession=null;
var currentMode="session";
var diariesPage=1;
var summariesPage=1;
var historyData=[];
var selectedHistoryFilename=null;
var pollingInterval=null;
var pendingConfirmAction=null;
var allSessions=[];
var allPersonas=[];
var personaList=[];
var personaUsers=[];
var selectedPersonaUser="";
var addMemoryType="";
var migrateData=null;
var allDiariesCache=[];
var allSummariesCache=[];

function api(url,opt){opt=opt||{};var h={"Content-Type":"application/json"};if(authToken)h["Authorization"]="Bearer "+authToken;opt.headers=h;return fetch(url,opt).then(function(r){if(r.status===401){logout();throw new Error("Unauthorized");}return r.json();});}
function showLoading(t){document.getElementById("loadingText").textContent=t||"加载中...";document.getElementById("loading").classList.add("active");}
function hideLoading(){document.getElementById("loading").classList.remove("active");}
function showToast(m,t){var e=document.getElementById("toast");e.textContent=m;e.className="toast show "+(t||"");setTimeout(function(){e.classList.remove("show");},3000);}
function escapeHtml(s){if(!s)return"";var d=document.createElement("div");d.textContent=s;return d.innerHTML;}

function switchLoginTab(tab){
currentLoginTab=tab;
document.getElementById("loginTabUser").classList.toggle("active",tab==="user");
document.getElementById("loginTabAdmin").classList.toggle("active",tab==="admin");
document.getElementById("loginUserFields").style.display=tab==="user"?"block":"none";
document.getElementById("loginAdminFields").style.display=tab==="admin"?"block":"none";
document.getElementById("loginError").classList.remove("show");
}

function showMain(){
document.getElementById("loginContainer").style.display="none";
document.getElementById("mainContainer").style.display="block";
applyLoginRestrictions();
}

function applyLoginRestrictions(){
var isAdmin=loginType==="admin";
var adminEls=document.querySelectorAll(".admin-only");
adminEls.forEach(function(el){el.style.display=isAdmin?"":"none";});
var userBar=document.getElementById("userInfoBar");
if(userBar){
if(loginType==="user"){
userBar.style.display="flex";
document.getElementById("userInfoQQ").textContent="QQ: "+loginQQ;
}else{
userBar.style.display="none";
}
}
}

function login(){
var e=document.getElementById("loginError");
var body={};
if(currentLoginTab==="admin"){
body={login_type:"admin",password:document.getElementById("adminPasswordInput").value};
}else{
var qq=document.getElementById("qqInput").value.trim();
var pw=document.getElementById("passwordInput").value;
if(!qq){e.textContent="请输入QQ号";e.classList.add("show");return;}
if(!pw){e.textContent="请输入密码";e.classList.add("show");return;}
body={login_type:"user",qq_number:qq,password:pw};
}
showLoading("正在登录...");
api("/api/login",{method:"POST",body:JSON.stringify(body)}).then(function(r){
hideLoading();
if(r.success){
authToken=r.token;
loginType=r.login_type;
loginQQ=r.qq_number||"";
localStorage.setItem("corememory_token",authToken);
localStorage.setItem("corememory_login_type",loginType);
localStorage.setItem("corememory_login_qq",loginQQ);
e.classList.remove("show");
showMain();
loadSessions();loadPersonas();loadPersonaList();
if(r.is_default_password){showChangePasswordGuide();}
}else{e.textContent=r.error||"登录失败";e.classList.add("show");}
}).catch(function(){hideLoading();e.textContent="登录失败，请检查网络";e.classList.add("show");});
}

function checkAuth(){
api("/api/check_auth").then(function(r){
if(r.authenticated){
loginType=r.login_type||"admin";
loginQQ=r.qq_number||"";
localStorage.setItem("corememory_login_type",loginType);
localStorage.setItem("corememory_login_qq",loginQQ);
showMain();loadSessions();loadPersonas();loadPersonaList();
}
}).catch(function(){});
}

function logout(){
api("/api/logout",{method:"POST"}).catch(function(){});
authToken="";loginType="";loginQQ="";
localStorage.removeItem("corememory_token");
localStorage.removeItem("corememory_login_type");
localStorage.removeItem("corememory_login_qq");
document.getElementById("loginContainer").style.display="flex";
document.getElementById("mainContainer").style.display="none";
if(pollingInterval){clearInterval(pollingInterval);pollingInterval=null;}
}

function showChangePasswordGuide(){
document.getElementById("changePwGuideModal").classList.add("active");
}
function closeChangePasswordGuide(){
document.getElementById("changePwGuideModal").classList.remove("active");
}
function showChangePasswordModal(){
closeChangePasswordGuide();
document.getElementById("changePwModal").classList.add("active");
document.getElementById("changePwOld").value="";
document.getElementById("changePwNew").value="";
document.getElementById("changePwConfirm").value="";
document.getElementById("changePwError").style.display="none";
}
function closeChangePasswordModal(){
document.getElementById("changePwModal").classList.remove("active");
}
function submitChangePassword(){
var oldPw=document.getElementById("changePwOld").value;
var newPw=document.getElementById("changePwNew").value;
var confirmPw=document.getElementById("changePwConfirm").value;
var errEl=document.getElementById("changePwError");
if(!oldPw||!newPw){errEl.textContent="请填写完整";errEl.style.display="block";return;}
if(newPw.length<4){errEl.textContent="新密码至少4位";errEl.style.display="block";return;}
if(newPw!==confirmPw){errEl.textContent="两次密码不一致";errEl.style.display="block";return;}
showLoading("修改密码中...");
api("/api/change_password",{method:"POST",body:JSON.stringify({old_password:oldPw,new_password:newPw})}).then(function(r){
hideLoading();
if(r.success){closeChangePasswordModal();showToast("密码修改成功！","success");}
else{errEl.textContent=r.error||"修改失败";errEl.style.display="block";}
}).catch(function(){hideLoading();errEl.textContent="网络错误";errEl.style.display="block";});
}

function getQueryParams(){
    if(!currentSession)return"";
    if(currentMode==="persona"){
        var p=new URLSearchParams();
        p.set("mode","persona");
        p.set("role_name",currentSession.role_name||"");
        p.set("space_name",currentSession.space_name||"");
        if(selectedPersonaUser)p.set("persona_user",selectedPersonaUser);
        return p.toString();
    }else{
        var p=new URLSearchParams();
        p.set("mode","session");
        p.set("user_id",currentSession.user_id||"");
        p.set("session_id",currentSession.session_id||"");
        p.set("role_name",currentSession.role_name||"");
        return p.toString();
    }
}
function getCommandData(){
    if(!currentSession)return{};
    if(currentMode==="persona"){
        return{mode:"persona",role_name:currentSession.role_name,space_name:currentSession.space_name,persona_user:selectedPersonaUser};
    }else{
        return{mode:"session",user_id:currentSession.user_id,session_id:currentSession.session_id,role_name:currentSession.role_name};
    }
}
function loadSessions(){api("/api/sessions").then(function(r){allSessions=r.sessions||[];updateSessionSelect();loadGlobalStats();}).catch(function(){showToast("加载会话失败","error");});}
function loadPersonas(){api("/api/personas").then(function(r){allPersonas=r.personas||[];updatePersonaList();if(currentMode==="persona")updateSessionSelect();}).catch(function(){});}
function loadPersonaList(){api("/api/persona_list").then(function(r){personaList=r.personas||[];updatePersonaSelects();}).catch(function(){});}

function updateSessionSelect(){
    var s=document.getElementById("sessionSelect");
    s.innerHTML="<option value=''>-- 请选择记忆会话 --</option>";
    var list=currentMode==="session"?allSessions:allPersonas;
    list.forEach(function(x){
        var o=document.createElement("option");
        o.value=JSON.stringify(x);
        var label;
        if(currentMode==="session"){
            label=x.role_name+" ("+x.user_id.substring(0,8)+"...)";
        }else{
            label=x.role_name+" / "+x.space_name+" ("+x.user_count+"人)";
        }
        if(x.last_updated){
            var d=new Date(x.last_updated*1000);
            label+=" - "+d.toLocaleDateString();
        }
        o.textContent=label;
        s.appendChild(o);
    });
    updateMigrateSelect();
}

function updatePersonaSelects(){var sel1=document.getElementById("createPersonaRole");sel1.innerHTML="<option value=''>-- 选择角色 --</option>";personaList.forEach(function(p){var o=document.createElement("option");o.value=p.id;o.textContent=p.name;sel1.appendChild(o);});}

function updateMigrateSelect(){var sel=document.getElementById("migrateSession");sel.innerHTML="<option value=''>-- 选择要迁移的会话 --</option>";allSessions.forEach(function(x){var o=document.createElement("option");o.value=JSON.stringify(x);o.textContent=x.role_name+" ("+x.user_id.substring(0,8)+"... / "+x.session_id.substring(0,8)+"...)";sel.appendChild(o);});}

function updatePersonaList(){var el=document.getElementById("personaList");if(allPersonas.length===0){el.innerHTML="<div class='empty-state'><p>暂无共享空间，请先创建</p></div>";return;}var h="";allPersonas.forEach(function(p){var d=p.last_updated?new Date(p.last_updated*1000).toLocaleString():"未知";h+="<div class='persona-card'><div class='persona-card-info'><h5>"+escapeHtml(p.role_name)+" / "+escapeHtml(p.space_name)+"</h5><p>👥 "+p.user_count+" 个用户</p></div><div class='persona-card-meta'>最后更新<br>"+d+"</div></div>";});el.innerHTML=h;}

function switchMode(mode){
    currentMode=mode;
    currentSession=null;
    selectedPersonaUser="";
    document.getElementById("sessionSelect").value="";
    document.getElementById("personaUserRow").style.display="none";
    document.querySelectorAll("#modeTabs .tab").forEach(function(t){
        t.classList.toggle("active",t.dataset.mode===mode);
    });
    if(mode==="persona"){
        loadPersonas();
    }else{
        updateSessionSelect();
    }
}

function loadPersonaUsers(){
    if(currentMode!=="persona"||!currentSession)return;
    var p=new URLSearchParams();
    p.set("role_name",currentSession.role_name);
    p.set("space_name",currentSession.space_name);
    api("/api/persona_users?"+p).then(function(r){
        personaUsers=r.users||[];
        var sel=document.getElementById("personaUserSelect");
        sel.innerHTML="<option value=''>-- 共享数据(无用户隔离) --</option>";
        personaUsers.forEach(function(u){
            var o=document.createElement("option");
            o.value=u;
            o.textContent="用户: "+u;
            sel.appendChild(o);
        });
        document.getElementById("personaUserRow").style.display="flex";
    }).catch(function(){});
}

function loadGlobalStats(){api("/api/stats").then(function(r){document.getElementById("statTotal").textContent=r.total_size||"-";document.getElementById("statDiaries").textContent=((r.details&&r.details.diaries)?r.details.diaries.count:0)+" 条";document.getElementById("statSummaries").textContent=((r.details&&r.details.plot_summaries)?r.details.plot_summaries.count:0)+" 条";document.getElementById("statLocked").textContent=((r.details&&r.details.locked_memories)?r.details.locked_memories.count:0)+" 条";}).catch(function(){});}

function loadSessionData(){
    var s=document.getElementById("sessionSelect");
    if(!s.value){
        currentSession=null;
        document.getElementById("personaUserRow").style.display="none";
        return;
    }
    currentSession=JSON.parse(s.value);
    selectedPersonaUser="";

    if(currentMode==="persona"){
        loadPersonaUsers();
    }else{
        document.getElementById("personaUserRow").style.display="none";
    }

    showLoading("加载数据中...");
    Promise.all([loadStats(),loadCoreMemory(),loadDiaries(1),loadPlotFramework(),loadPlotSummaries(1),loadLockedMemories(),loadCoreMemoryHistory(),loadFullLog(),loadConfig(),loadMemoryStats()]).then(function(){hideLoading();}).catch(function(){hideLoading();showToast("加载失败","error");});
}

function onPersonaUserChange(){
    selectedPersonaUser=document.getElementById("personaUserSelect").value;
    loadCoreMemory();
    loadLockedMemories();
    loadCoreMemoryHistory();
    loadMemoryStats();
}

function refreshData(){
    if(currentSession){
        loadSessionData();
    }else{
        loadSessions();
        loadPersonas();
    }
}
function switchPanel(n){document.querySelectorAll(".nav-item").forEach(function(x){x.classList.remove("active");});document.querySelectorAll(".panel-content").forEach(function(x){x.classList.remove("active");});var nav=document.querySelector(".nav-item[data-panel='"+n+"']");if(nav)nav.classList.add("active");var panel=document.getElementById("panel-"+n);if(panel)panel.classList.add("active");}

function loadStats(){if(!currentSession)return Promise.resolve();return api("/api/stats?"+getQueryParams()).then(function(r){document.getElementById("statTotal").textContent=r.total_size||"-";document.getElementById("statDiaries").textContent=((r.details&&r.details.diaries)?r.details.diaries.count:0)+" 条";document.getElementById("statSummaries").textContent=((r.details&&r.details.plot_summaries)?r.details.plot_summaries.count:0)+" 条";document.getElementById("statLocked").textContent=((r.details&&r.details.locked_memories)?r.details.locked_memories.count:0)+" 条";});}

function loadMemoryStats(){
if(!currentSession)return;
api("/api/memory_stats?"+getQueryParams()).then(function(r){
var s=r.stats||{};
document.getElementById("statTotalChars").textContent="共 "+formatChars(getTotalChars(s))+" 字";
document.getElementById("statDiariesChars").textContent=formatChars(s.diaries?s.diaries.chars:0)+" 字";
document.getElementById("statSummariesChars").textContent=formatChars(s.plot_summaries?s.plot_summaries.chars:0)+" 字";
document.getElementById("statLockedChars").textContent=formatChars(s.locked_memories?s.locked_memories.chars:0)+" 字";
}).catch(function(){});
}
function getTotalChars(s){
var total=0;
for(var k in s){if(s[k]&&s[k].chars)total+=s[k].chars;}
return total;
}
function formatChars(n){
if(n>=10000)return(n/10000).toFixed(1)+"万";
if(n>=1000)return(n/1000).toFixed(1)+"千";
return n;
}

function loadCoreMemory(){if(!currentSession)return Promise.resolve();return api("/api/core_memory?"+getQueryParams()).then(function(r){document.getElementById("coreMemoryEditor").value=(r.data&&r.data.content)?r.data.content:"";document.getElementById("coreMemoryTime").textContent=(r.data&&r.data.timestamp)?("更新于: "+r.data.timestamp):"-";});}
function saveCoreMemory(){if(!currentSession){showToast("请先选择会话","error");return;}var c=document.getElementById("coreMemoryEditor").value;if(!c.trim()){showToast("内容不能为空","error");return;}showLoading("保存中...");api("/api/core_memory?"+getQueryParams(),{method:"PUT",body:JSON.stringify({content:c})}).then(function(){hideLoading();showToast("已保存","success");loadCoreMemory();loadCoreMemoryHistory();}).catch(function(){hideLoading();showToast("保存失败","error");});}
function confirmDeleteCoreMemory(){showConfirmModal("清空核心记忆","此操作无法恢复！确定要清空核心记忆吗？",function(){showLoading("删除中...");api("/api/core_memory?"+getQueryParams(),{method:"DELETE"}).then(function(){hideLoading();closeModal();showToast("已清空","success");loadCoreMemory();}).catch(function(){hideLoading();closeModal();showToast("删除失败","error");});});}

function loadCoreMemoryHistory(){if(!currentSession)return Promise.resolve();return api("/api/core_memory_history?"+getQueryParams()).then(function(r){historyData=r.history||[];var el=document.getElementById("historyList");if(historyData.length>0){var h="";for(var i=0;i<historyData.length;i++){var item=historyData[i];h+="<div class='history-item' data-index='"+i+"'><div class='history-item-time'>"+escapeHtml(item.timestamp)+"</div><div class='history-item-preview'>"+escapeHtml((item.content||"").substring(0,80))+"...</div></div>";}el.innerHTML=h;}else{el.innerHTML="<div class='empty-state'><p>暂无历史版本</p></div>";}document.getElementById("historyDetailTitle").textContent="选择一个版本查看详情";document.getElementById("historyDetailContent").innerHTML="<div class='empty-state'><p>👈 点击左侧列表查看</p></div>";document.getElementById("restoreBtn").style.display="none";selectedHistoryFilename=null;});}
function showHistoryDetail(idx){var item=historyData[idx];if(!item)return;document.querySelectorAll(".history-item").forEach(function(x){x.classList.remove("selected");});var el=document.querySelector(".history-item[data-index='"+idx+"']");if(el)el.classList.add("selected");document.getElementById("historyDetailTitle").textContent="版本: "+item.timestamp;document.getElementById("historyDetailContent").textContent=item.content||"(空)";document.getElementById("restoreBtn").style.display="inline-flex";selectedHistoryFilename=item.history_filename;}
function restoreHistory(){if(!selectedHistoryFilename)return;showConfirmModal("恢复历史版本","当前版本会被覆盖，确定要恢复吗？",function(){showLoading("恢复中...");api("/api/core_memory_restore?"+getQueryParams(),{method:"POST",body:JSON.stringify({filename:selectedHistoryFilename})}).then(function(){hideLoading();closeModal();showToast("已恢复","success");loadCoreMemory();loadCoreMemoryHistory();}).catch(function(){hideLoading();closeModal();showToast("恢复失败","error");});});}

function loadDiaries(page){if(!currentSession)return Promise.resolve();diariesPage=page;var p=getQueryParams()+"&page="+page+"&page_size=15";return api("/api/diaries?"+p).then(function(r){allDiariesCache=r.diaries||[];var el=document.getElementById("diariesList");if(r.diaries&&r.diaries.length>0){var h="";for(var i=0;i<r.diaries.length;i++){var item=r.diaries[i];h+="<div class='list-item' data-index='"+item.index+"'><div class='list-item-header'><div class='list-item-meta'><span class='list-item-index'>#"+(item.index+1)+"</span><span class='list-item-time'>"+escapeHtml(item.timestamp)+"</span></div><div class='list-item-actions'><button class='btn btn-primary btn-sm migrate-diary-btn' data-idx='"+item.index+"'>迁移</button><button class='btn btn-secondary btn-sm edit-diary-btn' data-idx='"+item.index+"'>编辑</button><button class='btn btn-danger btn-sm delete-diary-btn' data-idx='"+item.index+"'>删除</button></div></div><div class='list-item-content' id='diary-content-"+item.index+"'>"+escapeHtml(item.summary)+"</div></div>";}el.innerHTML=h;renderPagination("diariesPagination",r.page,r.total_pages,"diaries");}else{el.innerHTML="<div class='empty-state'><p>暂无记忆日志</p></div>";document.getElementById("diariesPagination").innerHTML="";}});}
function editDiary(idx){var el=document.getElementById("diary-content-"+idx);var txt=el.textContent;el.classList.add("editing");el.innerHTML="<textarea>"+escapeHtml(txt)+"</textarea><div style='margin-top:8px'><button class='btn btn-primary btn-sm save-diary-btn' data-idx='"+idx+"'>保存</button> <button class='btn btn-secondary btn-sm cancel-diary-btn'>取消</button></div>";}
function saveDiaryEdit(idx){var ta=document.querySelector("#diary-content-"+idx+" textarea");showLoading("保存中...");api("/api/diaries/"+idx+"?"+getQueryParams(),{method:"PUT",body:JSON.stringify({summary:ta.value})}).then(function(){hideLoading();showToast("已更新","success");loadDiaries(diariesPage);}).catch(function(){hideLoading();showToast("保存失败","error");});}
function deleteDiary(idx){showConfirmModal("删除记忆日志","确定要删除这条记忆日志吗？",function(){showLoading("删除中...");api("/api/diaries/"+idx+"?"+getQueryParams(),{method:"DELETE"}).then(function(){hideLoading();closeModal();showToast("已删除","success");loadDiaries(diariesPage);loadStats();loadMemoryStats();}).catch(function(){hideLoading();closeModal();showToast("删除失败","error");});});}
function confirmClearDiaries(){showConfirmModal("清空所有记忆日志","此操作无法恢复！确定要清空吗？",function(){showLoading("清空中...");api("/api/diaries?"+getQueryParams(),{method:"DELETE",body:JSON.stringify({clear_all:true})}).then(function(){hideLoading();closeModal();showToast("已清空","success");loadDiaries(1);loadStats();loadMemoryStats();}).catch(function(){hideLoading();closeModal();showToast("清空失败","error");});});}

function loadPlotFramework(){if(!currentSession)return Promise.resolve();return api("/api/plot_framework?"+getQueryParams()).then(function(r){document.getElementById("frameworkEditor").value=(r.data&&r.data.content)?r.data.content:"";document.getElementById("frameworkTime").textContent=(r.data&&r.data.timestamp)?("更新于: "+r.data.timestamp):"-";});}
function savePlotFramework(){if(!currentSession){showToast("请先选择会话","error");return;}showLoading("保存中...");api("/api/plot_framework?"+getQueryParams(),{method:"PUT",body:JSON.stringify({content:document.getElementById("frameworkEditor").value})}).then(function(){hideLoading();showToast("已保存","success");loadPlotFramework();}).catch(function(){hideLoading();showToast("保存失败","error");});}
function confirmDeleteFramework(){showConfirmModal("清空剧情框架","此操作无法恢复！确定要清空吗？",function(){showLoading("删除中...");api("/api/plot_framework?"+getQueryParams(),{method:"DELETE"}).then(function(){hideLoading();closeModal();showToast("已清空","success");loadPlotFramework();}).catch(function(){hideLoading();closeModal();showToast("删除失败","error");});});}

function loadPlotSummaries(page){if(!currentSession)return Promise.resolve();summariesPage=page;var p=getQueryParams()+"&page="+page+"&page_size=10";return api("/api/plot_summaries?"+p).then(function(r){allSummariesCache=r.summaries||[];var el=document.getElementById("summariesList");if(r.summaries&&r.summaries.length>0){var h="";for(var i=0;i<r.summaries.length;i++){var item=r.summaries[i];h+="<div class='list-item' data-index='"+item.index+"'><div class='list-item-header'><div class='list-item-meta'><span class='list-item-index'>#"+(item.index+1)+"</span><span class='list-item-time'>"+escapeHtml(item.timestamp)+"</span></div><div class='list-item-actions'><button class='btn btn-primary btn-sm migrate-summary-btn' data-idx='"+item.index+"'>迁移</button><button class='btn btn-secondary btn-sm edit-summary-btn' data-idx='"+item.index+"'>编辑</button><button class='btn btn-danger btn-sm delete-summary-btn' data-idx='"+item.index+"'>删除</button></div></div><div class='list-item-content' id='summary-content-"+item.index+"'>"+escapeHtml(item.summary)+"</div></div>";}el.innerHTML=h;renderPagination("summariesPagination",r.page,r.total_pages,"summaries");}else{el.innerHTML="<div class='empty-state'><p>暂无剧情总结</p></div>";document.getElementById("summariesPagination").innerHTML="";}});}
function editSummary(idx){var el=document.getElementById("summary-content-"+idx);var txt=el.textContent;el.classList.add("editing");el.innerHTML="<textarea>"+escapeHtml(txt)+"</textarea><div style='margin-top:8px'><button class='btn btn-primary btn-sm save-summary-btn' data-idx='"+idx+"'>保存</button> <button class='btn btn-secondary btn-sm cancel-summary-btn'>取消</button></div>";}
function saveSummaryEdit(idx){var ta=document.querySelector("#summary-content-"+idx+" textarea");showLoading("保存中...");api("/api/plot_summaries/"+idx+"?"+getQueryParams(),{method:"PUT",body:JSON.stringify({summary:ta.value})}).then(function(){hideLoading();showToast("已更新","success");loadPlotSummaries(summariesPage);}).catch(function(){hideLoading();showToast("保存失败","error");});}
function deleteSummary(idx){showConfirmModal("删除剧情总结","确定要删除吗？",function(){showLoading("删除中...");api("/api/plot_summaries/"+idx+"?"+getQueryParams(),{method:"DELETE"}).then(function(){hideLoading();closeModal();showToast("已删除","success");loadPlotSummaries(summariesPage);loadStats();loadMemoryStats();}).catch(function(){hideLoading();closeModal();showToast("删除失败","error");});});}
function confirmClearSummaries(){showConfirmModal("清空所有剧情总结","此操作无法恢复！",function(){showLoading("清空中...");api("/api/plot_summaries?"+getQueryParams(),{method:"DELETE",body:JSON.stringify({clear_all:true})}).then(function(){hideLoading();closeModal();showToast("已清空","success");loadPlotSummaries(1);loadStats();loadMemoryStats();}).catch(function(){hideLoading();closeModal();showToast("清空失败","error");});});}

function loadLockedMemories(){if(!currentSession)return Promise.resolve();return api("/api/locked_memories?"+getQueryParams()).then(function(r){var el=document.getElementById("lockedList");if(r.locked_memories&&r.locked_memories.length>0){var h="";for(var i=0;i<r.locked_memories.length;i++){var item=r.locked_memories[i];h+="<div class='locked-item'><span class='locked-item-id'>"+item.id+"</span><div class='locked-item-content'><div id='locked-text-"+item.id+"'>"+escapeHtml(item.content)+"</div><div class='locked-item-time'>锁定于: "+escapeHtml(item.locked_at)+"</div></div><div class='locked-item-actions'><button class='btn btn-primary btn-sm migrate-locked-btn' data-id='"+item.id+"'>迁移</button><button class='btn btn-secondary btn-sm edit-locked-btn' data-id='"+item.id+"'>编辑</button><button class='btn btn-danger btn-sm delete-locked-btn' data-id='"+item.id+"'>删除</button></div></div>";}el.innerHTML=h;}else{el.innerHTML="<div class='empty-state'><p>暂无锁定记忆</p></div>";}});}
function addLockedMemory(){var inp=document.getElementById("newLockedContent");var c=inp.value.trim();if(!c){showToast("请输入内容","error");return;}if(!currentSession){showToast("请先选择会话","error");return;}showLoading("添加中...");api("/api/locked_memories?"+getQueryParams(),{method:"POST",body:JSON.stringify({content:c})}).then(function(){hideLoading();inp.value="";showToast("已添加","success");loadLockedMemories();loadStats();loadMemoryStats();}).catch(function(){hideLoading();showToast("添加失败","error");});}
function editLocked(id){var el=document.getElementById("locked-text-"+id);var txt=el.textContent;el.innerHTML="<input type='text' value='"+escapeHtml(txt)+"' style='width:100%;padding:8px;border:1px solid var(--primary-orange);border-radius:4px'><div style='margin-top:6px'><button class='btn btn-primary btn-sm save-locked-btn' data-id='"+id+"'>保存</button> <button class='btn btn-secondary btn-sm cancel-locked-btn'>取消</button></div>";}
function saveLockedEdit(id){var inp=document.querySelector("#locked-text-"+id+" input");showLoading("保存中...");api("/api/locked_memories/"+id+"?"+getQueryParams(),{method:"PUT",body:JSON.stringify({content:inp.value})}).then(function(){hideLoading();showToast("已更新","success");loadLockedMemories();}).catch(function(){hideLoading();showToast("保存失败","error");});}
function deleteLocked(id){showConfirmModal("删除锁定记忆","确定删除？",function(){showLoading("删除中...");api("/api/locked_memories/"+id+"?"+getQueryParams(),{method:"DELETE"}).then(function(){hideLoading();closeModal();showToast("已删除","success");loadLockedMemories();loadStats();loadMemoryStats();}).catch(function(){hideLoading();closeModal();showToast("删除失败","error");});});}
function confirmClearLocked(){showConfirmModal("清空所有锁定记忆","此操作无法恢复！",function(){showLoading("清空中...");api("/api/locked_memories?"+getQueryParams(),{method:"DELETE"}).then(function(){hideLoading();closeModal();showToast("已清空","success");loadLockedMemories();loadStats();loadMemoryStats();}).catch(function(){hideLoading();closeModal();showToast("清空失败","error");});});}

function loadFullLog(){if(!currentSession)return Promise.resolve();return api("/api/full_log?"+getQueryParams()).then(function(r){document.getElementById("fullLogEditor").value=r.content||"";document.getElementById("fullLogCount").textContent=r.conversation_count||0;document.getElementById("summaryOffset").textContent=r.summary_offset||0;document.getElementById("fullLogTime").textContent=r.line_count?(r.line_count+" 行"):"-";});}
function saveFullLog(){if(!currentSession){showToast("请先选择会话","error");return;}showLoading("保存中...");api("/api/full_log?"+getQueryParams(),{method:"PUT",body:JSON.stringify({content:document.getElementById("fullLogEditor").value})}).then(function(){hideLoading();showToast("已保存","success");loadFullLog();}).catch(function(){hideLoading();showToast("保存失败","error");});}
function confirmClearFullLog(){showConfirmModal("清空对话记录","此操作无法恢复！清空后记忆总结功能将重新开始计数。",function(){showLoading("清空中...");api("/api/full_log?"+getQueryParams(),{method:"DELETE"}).then(function(){hideLoading();closeModal();showToast("已清空","success");loadFullLog();}).catch(function(){hideLoading();closeModal();showToast("清空失败","error");});});}

function showAddMemoryModal(type){
addMemoryType=type;
document.getElementById("addMemoryTitle").textContent=type==="diary"?"添加记忆日志":"添加剧情总结";
document.getElementById("addMemoryUserLabelRow").style.display=type==="diary"?"block":"none";
var now=new Date();
now.setMinutes(now.getMinutes()-now.getTimezoneOffset());
document.getElementById("addMemoryTime").value=now.toISOString().slice(0,16);
document.getElementById("addMemoryContent").value="";
document.getElementById("addMemoryUserLabel").value="";
document.getElementById("addMemoryModal").classList.add("active");
}
function closeAddMemoryModal(){document.getElementById("addMemoryModal").classList.remove("active");}
function confirmAddMemory(){
var content=document.getElementById("addMemoryContent").value.trim();
if(!content){showToast("内容不能为空","error");return;}
var timeInput=document.getElementById("addMemoryTime").value;
var timestamp=timeInput?new Date(timeInput).toLocaleString("zh-CN",{year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit"}).replace(/\//g,"-"):new Date().toLocaleString();
var userLabel=document.getElementById("addMemoryUserLabel").value.trim();
var url=addMemoryType==="diary"?"/api/diaries":"/api/plot_summaries";
var body={content:content,timestamp:timestamp};
if(addMemoryType==="diary"&&userLabel)body.user_label=userLabel;
showLoading("添加中...");
api(url+"?"+getQueryParams(),{method:"POST",body:JSON.stringify(body)}).then(function(r){
hideLoading();
if(r.success){closeAddMemoryModal();showToast("添加成功","success");if(addMemoryType==="diary")loadDiaries(1);else loadPlotSummaries(1);loadStats();loadMemoryStats();}
else showToast(r.error||"添加失败","error");
}).catch(function(){hideLoading();showToast("添加失败","error");});
}

function showMigrateModal(type,data){
migrateData={type:type,content:data};
document.getElementById("migratePreview").textContent=(data.summary||data.content||"").substring(0,200)+"...";
document.getElementById("migrateUserField").style.display=type==="locked_memory"?"block":"none";
updateMigrateSelects2();
document.getElementById("migrateModal").classList.add("active");
}
function closeMigrateModal(){document.getElementById("migrateModal").classList.remove("active");migrateData=null;}
function onMigrateTargetModeChange(){
var mode=document.getElementById("migrateTargetMode").value;
document.getElementById("migrateSessionFields").style.display=mode==="session"?"block":"none";
document.getElementById("migratePersonaFields").style.display=mode==="persona"?"block":"none";
}
function updateMigrateSelects2(){
var sessionSel=document.getElementById("migrateTargetSession");
sessionSel.innerHTML="<option value=''>-- 选择目标会话 --</option>";
allSessions.forEach(function(s){
var o=document.createElement("option");
o.value=JSON.stringify(s);
o.textContent=s.role_name+" ("+s.user_id.substring(0,8)+"...)";
sessionSel.appendChild(o);
});
var personaSel=document.getElementById("migrateTargetPersona");
personaSel.innerHTML="<option value=''>-- 选择目标共享空间 --</option>";
allPersonas.forEach(function(p){
var o=document.createElement("option");
o.value=JSON.stringify(p);
o.textContent=p.role_name+" / "+p.space_name;
personaSel.appendChild(o);
});
}
function confirmMigrate(){
if(!migrateData){showToast("没有要迁移的数据","error");return;}
var targetMode=document.getElementById("migrateTargetMode").value;
var body={memory_type:migrateData.type,content:migrateData.content,source_mode:currentMode};
if(currentMode==="persona"){
body.source_role_name=currentSession.role_name;
body.source_space_name=currentSession.space_name;
body.source_persona_user=selectedPersonaUser;
}else{
body.source_user_id=currentSession.user_id;
body.source_session_id=currentSession.session_id;
body.source_role_name=currentSession.role_name;
}
body.target_mode=targetMode;
if(targetMode==="session"){
var ts=document.getElementById("migrateTargetSession").value;
if(!ts){showToast("请选择目标会话","error");return;}
var target=JSON.parse(ts);
body.target_user_id=target.user_id;
body.target_session_id=target.session_id;
body.target_role_name=target.role_name;
}else{
var tp=document.getElementById("migrateTargetPersona").value;
if(!tp){showToast("请选择目标共享空间","error");return;}
var target=JSON.parse(tp);
body.target_role_name=target.role_name;
body.target_space_name=target.space_name;
if(migrateData.type==="locked_memory"){
var tu=document.getElementById("migrateTargetUser").value.trim();
if(!tu){showToast("锁定记忆需要指定目标用户","error");return;}
body.target_persona_user=tu;
}
}
showLoading("迁移中...");
api("/api/migrate_memory",{method:"POST",body:JSON.stringify(body)}).then(function(r){
hideLoading();
if(r.success){closeMigrateModal();showToast("迁移成功","success");}
else showToast(r.error||"迁移失败","error");
}).catch(function(){hideLoading();showToast("迁移失败","error");});
}

function runCommand(cmd){if(!currentSession){showToast("请先选择会话","error");return;}showLoading("启动任务...");api("/api/command/"+cmd,{method:"POST",body:JSON.stringify(getCommandData())}).then(function(r){hideLoading();if(r.success){showToast(r.message||"任务已启动","success");startPollingTaskStatus(r.task_id);}else{showToast(r.error||"启动失败","error");}}).catch(function(){hideLoading();showToast("启动失败","error");});}
function startPollingTaskStatus(tid){if(pollingInterval)clearInterval(pollingInterval);updateTaskStatusUI(tid);pollingInterval=setInterval(function(){updateTaskStatusUI(tid);},3000);}
function updateTaskStatusUI(tid){api("/api/command/status?task_id="+tid).then(function(r){var el=document.getElementById("taskStatus");var txt=r.status==="running"?"⏳ 执行中":(r.status==="completed"?"✅ 已完成":"❌ 失败");el.innerHTML="<div class='task-status "+r.status+"'><span class='dot'></span>"+txt+": "+escapeHtml(r.message)+"</div>";if(r.status!=="running"){if(pollingInterval){clearInterval(pollingInterval);pollingInterval=null;}if(r.status==="completed")refreshData();}}).catch(function(){});}

function getBatchMemoryPayload(){
var content=document.getElementById("batchMemoryContent").value.trim();
return {
memory_type:document.getElementById("batchMemoryType").value,
content:content,
include_session:document.getElementById("batchIncludeSession").checked,
include_persona:document.getElementById("batchIncludePersona").checked
};
}
function renderBatchResult(data,executed){
var el=document.getElementById("batchPreviewResult");
var r=executed?data.result:data.preview;
if(!r){el.style.display="none";return;}
var h=(executed?"执行结果":"预览结果")+"：目标 "+r.total+" 个；会话 "+(r.session_count||"-")+" 个；共享用户 "+(r.persona_count||"-")+" 个；";
if(executed){h+="新增 "+r.added+" 个；跳过重复 "+r.duplicates+" 个。";}
else{h+="预计新增 "+r.would_add+" 个；重复 "+r.duplicates+" 个。";}
if(r.errors&&r.errors.length){h+="<br>失败 "+r.errors.length+" 个："+escapeHtml(r.errors.slice(0,5).map(function(e){return e.target+': '+e.error;}).join("；"));}
el.innerHTML=h;
el.style.display="block";
}
function previewBatchMemory(){
var body=getBatchMemoryPayload();
if(!body.content){showToast("请输入注入内容","error");return;}
showLoading("计算影响范围...");
api("/api/batch_memory/preview",{method:"POST",body:JSON.stringify(body)}).then(function(r){hideLoading();if(r.success){renderBatchResult(r,false);showToast("预览完成","success");}else showToast(r.error||"预览失败","error");}).catch(function(){hideLoading();showToast("预览失败","error");});
}
function executeBatchMemory(){
var body=getBatchMemoryPayload();
if(!body.content){showToast("请输入注入内容","error");return;}
showConfirmModal("批量注入记忆","确认将这条内容追加到所有选中范围的记忆中？执行前建议先预览。",function(){
body.confirm=true;
showLoading("批量注入中...");
api("/api/batch_memory/execute",{method:"POST",body:JSON.stringify(body)}).then(function(r){hideLoading();closeModal();if(r.success){renderBatchResult(r,true);showToast("批量注入完成","success");loadSessions();loadPersonas();refreshData();}else showToast(r.error||"批量注入失败","error");}).catch(function(){hideLoading();closeModal();showToast("批量注入失败","error");});
});
}

function createPersonaSpace(){var role=document.getElementById("createPersonaRole").value;var space=document.getElementById("createSpaceName").value.trim();if(!role){showToast("请选择角色","error");return;}if(!space){showToast("请输入空间名称","error");return;}showLoading("创建中...");api("/api/persona/create",{method:"POST",body:JSON.stringify({role_name:role,space_name:space})}).then(function(r){hideLoading();if(r.success){showToast(r.message||"创建成功","success");document.getElementById("createSpaceName").value="";loadPersonas();}else{showToast(r.error||"创建失败","error");}}).catch(function(){hideLoading();showToast("创建失败","error");});}

function migrateToPersona(){var sel=document.getElementById("migrateSession");var space=document.getElementById("migrateSpaceName").value.trim();if(!sel.value){showToast("请选择要迁移的会话","error");return;}if(!space){showToast("请输入目标空间名称","error");return;}var session=JSON.parse(sel.value);showConfirmModal("迁移到共享空间","将会话 ["+session.role_name+"] 迁移到共享空间 ["+space+"]？",function(){showLoading("迁移中...");api("/api/persona/migrate",{method:"POST",body:JSON.stringify({user_id:session.user_id,session_id:session.session_id,role_name:session.role_name,space_name:space})}).then(function(r){hideLoading();closeModal();if(r.success){var msg="迁移完成！\\n"+r.actions.join("\\n");if(r.warnings&&r.warnings.length)msg+="\\n\\n⚠️ "+r.warnings.join("\\n");showToast("迁移成功","success");alert(msg);document.getElementById("migrateSpaceName").value="";loadPersonas();}else{showToast((r.errors&&r.errors.join(", "))||"迁移失败","error");}}).catch(function(){hideLoading();closeModal();showToast("迁移失败","error");});});}

function loadConfig(){return api("/api/config").then(function(r){document.getElementById("autoCleanupEnabled").checked=r.auto_cleanup_enabled;document.getElementById("maxCoreHistory").value=r.max_core_memory_history||100;document.getElementById("maxDiaries").value=r.max_diary_entries||1000;document.getElementById("maxSummaries").value=r.max_plot_summaries||200;}).catch(function(){});}
function saveSettings(){showLoading("保存中...");api("/api/config",{method:"POST",body:JSON.stringify({auto_cleanup_enabled:document.getElementById("autoCleanupEnabled").checked,max_core_memory_history:parseInt(document.getElementById("maxCoreHistory").value),max_diary_entries:parseInt(document.getElementById("maxDiaries").value),max_plot_summaries:parseInt(document.getElementById("maxSummaries").value)})}).then(function(){hideLoading();showToast("设置已保存","success");}).catch(function(){hideLoading();showToast("保存失败","error");});}
function previewCleanup(){if(!currentSession){showToast("请先选择会话","error");return;}if(currentMode==="persona"){showToast("共享模式暂不支持清理预览","error");return;}showLoading("计算中...");api("/api/cleanup_preview",{method:"POST",body:JSON.stringify(currentSession)}).then(function(r){hideLoading();var v=r.preview;alert("将清理:\\n• 核心记忆历史: "+v.core_memory_history+" 份\\n• 记忆日志: "+v.diaries+" 条\\n• 剧情总结: "+v.plot_summaries+" 条");}).catch(function(){hideLoading();showToast("预览失败","error");});}
function showCleanupConfirm(){if(!currentSession){showToast("请先选择会话","error");return;}if(currentMode==="persona"){showToast("共享模式暂不支持清理","error");return;}showConfirmModal("执行清理","此操作无法恢复！确定要执行清理吗？",function(){showLoading("清理中...");var d=JSON.parse(JSON.stringify(currentSession));d.confirm=true;api("/api/cleanup",{method:"POST",body:JSON.stringify(d)}).then(function(){hideLoading();closeModal();showToast("清理完成","success");loadSessionData();}).catch(function(){hideLoading();closeModal();showToast("清理失败","error");});});}

function renderPagination(cid,cur,total,type){var el=document.getElementById(cid);if(total<=1){el.innerHTML="";return;}var pd=cur<=1?"disabled":"";var nd=cur>=total?"disabled":"";el.innerHTML="<button class='page-btn' data-type='"+type+"' data-page='"+(cur-1)+"' "+pd+">上一页</button><button class='current'>"+cur+" / "+total+"</button><button class='page-btn' data-type='"+type+"' data-page='"+(cur+1)+"' "+nd+">下一页</button>";}
function showConfirmModal(title,body,fn){document.getElementById("confirmModalTitle").textContent=title;document.getElementById("confirmModalBody").innerHTML="<div class='alert alert-danger'>⚠️ "+body+"</div>";pendingConfirmAction=fn;document.getElementById("confirmModal").classList.add("active");}
function closeModal(){document.getElementById("confirmModal").classList.remove("active");pendingConfirmAction=null;}

document.addEventListener("DOMContentLoaded",function(){
document.getElementById("loginBtn").onclick=login;
document.getElementById("passwordInput").onkeypress=function(e){if(e.keyCode===13)login();};
document.getElementById("adminPasswordInput").onkeypress=function(e){if(e.keyCode===13)login();};
document.getElementById("qqInput").onkeypress=function(e){if(e.keyCode===13)document.getElementById("passwordInput").focus();};
document.getElementById("sessionSelect").onchange=loadSessionData;
document.getElementById("personaUserSelect").onchange=onPersonaUserChange;
document.getElementById("refreshBtn").onclick=refreshData;
document.getElementById("logoutBtn").onclick=logout;
var changePwBtn=document.getElementById("changePwBtn");if(changePwBtn)changePwBtn.onclick=showChangePasswordModal;
var submitPwBtn=document.getElementById("submitChangePwBtn");if(submitPwBtn)submitPwBtn.onclick=submitChangePassword;
var guidePwBtn=document.getElementById("guidePwChangeBtn");if(guidePwBtn)guidePwBtn.onclick=showChangePasswordModal;
var guideSkipBtn=document.getElementById("guidePwSkipBtn");if(guideSkipBtn)guideSkipBtn.onclick=closeChangePasswordGuide;
document.getElementById("sessionSelect").onchange=loadSessionData;
document.getElementById("personaUserSelect").onchange=onPersonaUserChange;
document.getElementById("refreshBtn").onclick=refreshData;
document.getElementById("logoutBtn").onclick=logout;
document.getElementById("saveCoreBtn").onclick=saveCoreMemory;
document.getElementById("deleteCoreBtn").onclick=confirmDeleteCoreMemory;
document.getElementById("addDiaryBtn").onclick=function(){showAddMemoryModal("diary");};
document.getElementById("clearDiariesBtn").onclick=confirmClearDiaries;
document.getElementById("saveFrameworkBtn").onclick=savePlotFramework;
document.getElementById("deleteFrameworkBtn").onclick=confirmDeleteFramework;
document.getElementById("addSummaryBtn").onclick=function(){showAddMemoryModal("plot_summary");};
document.getElementById("clearSummariesBtn").onclick=confirmClearSummaries;
document.getElementById("clearLockedBtn").onclick=confirmClearLocked;
document.getElementById("addLockedBtn").onclick=addLockedMemory;
document.getElementById("newLockedContent").onkeypress=function(e){if(e.keyCode===13)addLockedMemory();};
document.getElementById("restoreBtn").onclick=restoreHistory;
document.getElementById("saveFullLogBtn").onclick=saveFullLog;
document.getElementById("clearFullLogBtn").onclick=confirmClearFullLog;
document.getElementById("scrollTopBtn").onclick=function(){var ed=document.getElementById("fullLogEditor");ed.scrollTop=0;ed.setSelectionRange(0,0);ed.focus();};
document.getElementById("scrollBottomBtn").onclick=function(){var ed=document.getElementById("fullLogEditor");ed.scrollTop=ed.scrollHeight;ed.setSelectionRange(ed.value.length,ed.value.length);ed.focus();};
document.getElementById("cmdMemorySummary").onclick=function(){runCommand("memory_summary");};
document.getElementById("cmdPlotSummary").onclick=function(){runCommand("plot_summary");};
document.getElementById("cmdRegenFramework").onclick=function(){runCommand("regenerate_framework");};
document.getElementById("cmdSummarizeDiary").onclick=function(){runCommand("summarize_diary");};
document.getElementById("cmdRebuildVectors").onclick=function(){runCommand("rebuild_vectors");};
document.getElementById("cmdCompressFramework").onclick=function(){runCommand("compress_framework");};
document.getElementById("previewBatchMemoryBtn").onclick=previewBatchMemory;
document.getElementById("executeBatchMemoryBtn").onclick=executeBatchMemory;
document.getElementById("createPersonaBtn").onclick=createPersonaSpace;
document.getElementById("migrateBtn").onclick=migrateToPersona;
document.getElementById("saveSettingsBtn").onclick=saveSettings;
document.getElementById("previewCleanupBtn").onclick=previewCleanup;
document.getElementById("executeCleanupBtn").onclick=showCleanupConfirm;
document.getElementById("modalCloseBtn").onclick=closeModal;
document.getElementById("modalCancelBtn").onclick=closeModal;
document.getElementById("confirmModalBtn").onclick=function(){if(pendingConfirmAction)pendingConfirmAction();};
document.getElementById("confirmAddMemoryBtn").onclick=confirmAddMemory;
document.getElementById("migrateTargetMode").onchange=onMigrateTargetModeChange;
document.getElementById("confirmMigrateBtn").onclick=confirmMigrate;
document.querySelectorAll("#modeTabs .tab").forEach(function(t){t.onclick=function(){switchMode(this.dataset.mode);};});
document.querySelectorAll(".nav-item").forEach(function(x){x.onclick=function(){switchPanel(this.getAttribute("data-panel"));};});
document.getElementById("historyList").onclick=function(e){var item=e.target.closest(".history-item");if(item)showHistoryDetail(parseInt(item.getAttribute("data-index")));};
document.addEventListener("click",function(e){
if(e.target.classList.contains("edit-diary-btn"))editDiary(parseInt(e.target.getAttribute("data-idx")));
if(e.target.classList.contains("delete-diary-btn"))deleteDiary(parseInt(e.target.getAttribute("data-idx")));
if(e.target.classList.contains("save-diary-btn"))saveDiaryEdit(parseInt(e.target.getAttribute("data-idx")));
if(e.target.classList.contains("cancel-diary-btn"))loadDiaries(diariesPage);
if(e.target.classList.contains("edit-summary-btn"))editSummary(parseInt(e.target.getAttribute("data-idx")));
if(e.target.classList.contains("delete-summary-btn"))deleteSummary(parseInt(e.target.getAttribute("data-idx")));
if(e.target.classList.contains("save-summary-btn"))saveSummaryEdit(parseInt(e.target.getAttribute("data-idx")));
if(e.target.classList.contains("cancel-summary-btn"))loadPlotSummaries(summariesPage);
if(e.target.classList.contains("edit-locked-btn"))editLocked(parseInt(e.target.getAttribute("data-id")));
if(e.target.classList.contains("delete-locked-btn"))deleteLocked(parseInt(e.target.getAttribute("data-id")));
if(e.target.classList.contains("save-locked-btn"))saveLockedEdit(parseInt(e.target.getAttribute("data-id")));
if(e.target.classList.contains("cancel-locked-btn"))loadLockedMemories();
if(e.target.classList.contains("page-btn")&&!e.target.disabled){var type=e.target.getAttribute("data-type");var page=parseInt(e.target.getAttribute("data-page"));if(type==="diaries")loadDiaries(page);if(type==="summaries")loadPlotSummaries(page);}
if(e.target.classList.contains("migrate-diary-btn")){
var idx=parseInt(e.target.getAttribute("data-idx"));
for(var i=0;i<allDiariesCache.length;i++){
if(allDiariesCache[i].index===idx){showMigrateModal("diary",allDiariesCache[i]);break;}
}
}
if(e.target.classList.contains("migrate-summary-btn")){
var idx=parseInt(e.target.getAttribute("data-idx"));
for(var i=0;i<allSummariesCache.length;i++){
if(allSummariesCache[i].index===idx){showMigrateModal("plot_summary",allSummariesCache[i]);break;}
}
}
if(e.target.classList.contains("migrate-locked-btn")){
var id=parseInt(e.target.getAttribute("data-id"));
api("/api/locked_memories?"+getQueryParams()).then(function(r){
if(r.locked_memories){
for(var i=0;i<r.locked_memories.length;i++){
if(r.locked_memories[i].id===id){showMigrateModal("locked_memory",r.locked_memories[i]);break;}
}
}
});
}
});
if(authToken)checkAuth();
});
</script>
</body>
</html>'''
