"""
API Pool — 聚合 API 自动切换模块（GUI 版）

启动: python api_pool_server.py
访问: http://localhost:5100

⚠️ EXPERIMENTAL (2026-08-13): 极端情况处理实验版本，观察期。
  1. 幂等冻结：并发请求二次失败不刷新冷却窗口（_set_cooldown）
  2. 并发保护：chat 循环顶部跳过已冻结端点，并发请求立即转向
  3. 并发探活：下一级探活失败后并发探活剩余候选（11s/21s 两阶段）
  4. 终极兜底：priority=99 端点，全池故障/轮转超 530s 时锁定兜底（60s 容错，5min 滑动窗口）
  详见 PROJECT.md「实验版本」章节。
"""

import os
import json
import time
import threading
import sqlite3
import socket
import itertools
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import queue
from datetime import datetime, timedelta
from collections import deque

LATENCY_OK_MAX = 2000     
LATENCY_SLOW_MAX = 5000   
# 假成功检测：上游返回 200 OK 但内容含拒绝/错误信息
FAKE_SUCCESS_PATTERNS = ["无法给到相关内容"]


class LogManager:
    def __init__(self, max_history=300):
        self.history = []
        self.lock = threading.Lock()
        self.max_history = max_history
        self._counter = 0

    def log(self, level, msg):
        ts = time.time()
        time_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
        with self.lock:
            self._counter += 1
            entry = {"id": self._counter, "time": time_str, "level": level, "msg": msg, "timestamp": ts}
            self.history.append(entry)
            if len(self.history) > self.max_history:
                self.history.pop(0)

    def get_logs_since(self, last_id):
        with self.lock:
            return [log for log in self.history if log["id"] > last_id]

    def clear_logs(self):
        with self.lock:
            self.history.clear()

sys_logger = LogManager()
def sys_log(msg, level="INFO"):
    sys_logger.log(level, msg)
    print(f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}")

class TokenTracker:
    def __init__(self, db_path="token_stats.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON token_usage(timestamp)")
            try:
                conn.execute("ALTER TABLE token_usage ADD COLUMN endpoint_name TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE token_usage ADD COLUMN cached_tokens INTEGER DEFAULT 0")
            except Exception:
                pass

    def add_usage(self, endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens=0):
        def _do_insert():
            try:
                with sqlite3.connect(self.db_path, timeout=5) as conn:
                    conn.execute(
                        "INSERT INTO token_usage (endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens) VALUES (?, ?, ?, ?, ?, ?)",
                        (endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens)
                    )
            except Exception as e:
                sys_log(f"记录 token 消耗失败: {e}", "WARN")
        threading.Thread(target=_do_insert, daemon=True).start()

    def get_today_usage_by_endpoint(self, endpoint_name):
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT SUM(total_tokens) FROM token_usage WHERE endpoint_name = ? AND timestamp >= datetime(date('now', 'localtime'), 'utc')", (endpoint_name,))
                return cursor.fetchone()[0] or 0
        except Exception:
            return 0

    def rename_endpoint(self, old_name: str, new_name: str):
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("UPDATE token_usage SET endpoint_name = ? WHERE endpoint_name = ?", (new_name, old_name))
        except Exception as e:
            sys_log(f"重命名端点统计数据失败: {e}", "WARN")

    def get_stats(self, endpoint_filter=None):
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()
            ep_cond = " AND endpoint_name = ?" if endpoint_filter and endpoint_filter != "all" else ""
            params = (endpoint_filter,) if (endpoint_filter and endpoint_filter != "all") else ()
            
            cursor.execute(f"SELECT SUM(total_tokens), SUM(cached_tokens), SUM(prompt_tokens), COUNT(*) FROM token_usage WHERE timestamp >= datetime(date('now', 'localtime'), 'utc'){ep_cond}", params)
            today_row = cursor.fetchone()
            today = today_row[0] or 0
            today_cached = today_row[1] or 0
            today_prompt = today_row[2] or 0
            today_calls = today_row[3] or 0
            today_cache_hit_rate = round(today_cached / today_prompt * 100, 1) if today_prompt > 0 else 0
            
            cursor.execute(f"SELECT SUM(total_tokens) FROM token_usage WHERE timestamp >= datetime(date('now', '-2 days', 'localtime'), 'utc'){ep_cond}", params)
            last_3_days = cursor.fetchone()[0] or 0
            cursor.execute(f"SELECT SUM(total_tokens) FROM token_usage WHERE timestamp >= datetime(date('now', '-6 days', 'localtime'), 'utc'){ep_cond}", params)
            last_7_days = cursor.fetchone()[0] or 0
            cursor.execute(f"SELECT SUM(total_tokens), SUM(cached_tokens), SUM(prompt_tokens), COUNT(*) FROM token_usage WHERE timestamp >= datetime(date('now', '-29 days', 'localtime'), 'utc'){ep_cond}", params)
            month_row = cursor.fetchone()
            last_30_days = month_row[0] or 0
            month_cached = month_row[1] or 0
            month_prompt = month_row[2] or 0
            month_calls = month_row[3] or 0
            month_cache_hit_rate = round(month_cached / month_prompt * 100, 1) if month_prompt > 0 else 0
            
            cursor.execute(f"""
                SELECT date(timestamp, 'localtime') as d, SUM(total_tokens), SUM(prompt_tokens), SUM(cached_tokens), SUM(completion_tokens)
                FROM token_usage
                WHERE timestamp >= datetime(date('now', '-13 days', 'localtime'), 'utc'){ep_cond}
                GROUP BY d
            """, params)
            raw_trend = {r[0]: {"total": r[1] or 0, "prompt": r[2] or 0, "cached": r[3] or 0, "completion": r[4] or 0} for r in cursor.fetchall()}
            trend_14d = []
            now = datetime.now()
            for i in range(13, -1, -1):
                d_str = (now - timedelta(days=i)).strftime('%Y-%m-%d')
                data = raw_trend.get(d_str, {"total": 0, "prompt": 0, "cached": 0, "completion": 0})
                trend_14d.append({"date": d_str, "tokens": data["total"], "prompt": data["prompt"], "cached": data["cached"], "completion": data["completion"]})
                
            cursor.execute(f"""
                SELECT strftime('%H', datetime(timestamp, 'localtime')) as h, SUM(total_tokens), COUNT(*), SUM(prompt_tokens), SUM(cached_tokens)
                FROM token_usage
                WHERE timestamp >= datetime(date('now', 'localtime'), 'utc'){ep_cond}
                GROUP BY h
            """, params)
            raw_hourly = {r[0]: (r[1], r[2], r[3] or 0, r[4] or 0) for r in cursor.fetchall()}
            trend_today_hourly = []
            for i in range(24):
                h_str = f"{i:02d}"
                val = raw_hourly.get(h_str, (0, 0, 0, 0))
                missed = max(0, val[2] - val[3])
                trend_today_hourly.append({"date": f"{h_str}:00", "tokens": val[0] or 0, "calls": val[1] or 0, "missed": missed})
                
            cursor.execute(f"""
                SELECT endpoint_name, model, SUM(total_tokens), COUNT(*), SUM(prompt_tokens), SUM(cached_tokens)
                FROM token_usage
                WHERE timestamp >= datetime(date('now', 'localtime'), 'utc'){ep_cond}
                GROUP BY endpoint_name, model
                ORDER BY SUM(total_tokens) DESC
            """, params)
            today_endpoints = [{"endpoint": r[0] or "未知端点", "model": r[1], "tokens": r[2] or 0, "calls": r[3] or 0, "cache_hit_rate": round((r[5] or 0)/(r[4] or 1)*100, 1) if (r[4] or 0) > 0 else 0} for r in cursor.fetchall()]
            
            cursor.execute(f"""
                SELECT endpoint_name, model, SUM(total_tokens), COUNT(*), SUM(prompt_tokens), SUM(cached_tokens)
                FROM token_usage
                WHERE strftime('%Y-%m', timestamp, 'localtime') = strftime('%Y-%m', 'now', 'localtime'){ep_cond}
                GROUP BY endpoint_name, model
                ORDER BY SUM(total_tokens) DESC
            """, params)
            month_endpoints = [{"endpoint": r[0] or "未知端点", "model": r[1], "tokens": r[2] or 0, "calls": r[3] or 0, "cache_hit_rate": round((r[5] or 0)/(r[4] or 1)*100, 1) if (r[4] or 0) > 0 else 0} for r in cursor.fetchall()]

            cursor.execute("SELECT DISTINCT endpoint_name FROM token_usage WHERE endpoint_name IS NOT NULL")
            all_endpoints_list = [r[0] for r in cursor.fetchall()]

            return {
                "today": today,
                "today_cached": today_cached,
                "today_missed": max(0, today_prompt - today_cached),
                "today_calls": today_calls,
                "today_cache_hit_rate": today_cache_hit_rate,
                "last_3_days": last_3_days,
                "last_7_days": last_7_days,
                "last_30_days": last_30_days,
                "month_cached": month_cached,
                "month_missed": max(0, month_prompt - month_cached),
                "month_calls": month_calls,
                "month_cache_hit_rate": month_cache_hit_rate,
                "trend_14d": trend_14d,
                "trend_today_hourly": trend_today_hourly,
                "today_endpoints": today_endpoints,
                "month_endpoints": month_endpoints,
                "all_endpoints_list": all_endpoints_list
            }

    def export_csv(self):
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Timestamp", "Endpoint", "Model", "Prompt Tokens", "Completion Tokens", "Total Tokens", "Cached Tokens"])
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, timestamp, endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens FROM token_usage ORDER BY id DESC")
            for row in cursor.fetchall():
                writer.writerow(row)
        return output.getvalue()

    def clear_data(self):
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM token_usage")
            conn.commit()

token_tracker = TokenTracker()

class ChatLogger:
    def __init__(self, db_path="chat_logs.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                endpoint_name TEXT,
                model TEXT,
                prompt TEXT,
                completion TEXT,
                total_tokens INTEGER,
                latency_ms INTEGER
            )''')
            conn.commit()
            conn.close()

    def add_log(self, endpoint_name, model, prompt, completion, total_tokens, latency_ms):
        def _write():
            with self._lock:
                try:
                    conn = sqlite3.connect(self.db_path)
                    c = conn.cursor()
                    c.execute(
                        "INSERT INTO chat_logs (endpoint_name, model, prompt, completion, total_tokens, latency_ms) VALUES (?, ?, ?, ?, ?, ?)",
                        (endpoint_name, model, prompt, completion, total_tokens, latency_ms)
                    )
                    conn.commit()
                    conn.close()
                except Exception as e:
                    sys_log(f"记录对话日志失败: {e}", "ERROR")
        threading.Thread(target=_write, daemon=True).start()

    def get_logs(self, limit=50, offset=0):
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute(
                    "SELECT id, datetime(timestamp, 'localtime'), endpoint_name, model, prompt, completion, total_tokens, latency_ms FROM chat_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset)
                )
                rows = c.fetchall()
                
                c.execute("SELECT COUNT(*) FROM chat_logs")
                total = c.fetchone()[0]
                conn.close()
                
                return {
                    "total": total,
                    "logs": [
                        {
                            "id": r[0],
                            "timestamp": r[1],
                            "endpoint_name": r[2],
                            "model": r[3],
                            "prompt": r[4],
                            "completion": r[5],
                            "total_tokens": r[6],
                            "latency_ms": r[7]
                        } for r in rows
                    ]
                }
            except Exception as e:
                return {"total": 0, "logs": [], "error": str(e)}

    def clear_logs(self):
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute("DELETE FROM chat_logs")
                conn.commit()
                conn.close()
            except Exception:
                pass

chat_logger = ChatLogger()

def extract_prompt_text(payload):
    try:
        messages = payload.get("messages", [])
        output = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if isinstance(content, str):
                output.append(f"[{role.upper()}]\n{content}")
            elif isinstance(content, list):
                parts = []
                for part in content:
                    ptype = part.get("type", "")
                    if ptype == "text":
                        parts.append(part.get("text", ""))
                    elif ptype == "image_url":
                        parts.append("[Base64 Image Omitted]")
                output.append(f"[{role.upper()}]\n" + "\n".join(parts))
        return "\n\n".join(output)
    except Exception:
        return str(payload)[:2000]

# ============================================================
#  数据结构
# ============================================================


def _get_resp_socket(resp):
    """Get the underlying socket of an http.client.HTTPResponse for settimeout()."""
    try:
        fp = getattr(resp, "fp", None)
        if fp is None:
            return None
        raw = getattr(fp, "raw", None)
        if raw is None:
            raw = fp
        sock = getattr(raw, "_sock", None)
        if sock is None:
            sock = raw
        return sock
    except Exception:
        return None


@dataclass
class Endpoint:
    id: str = ""
    name: str = "unnamed"
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"
    priority: int = 999
    timeout: int = 60
    max_retries: int = 1
    enabled: bool = True
    cooldown_minutes: int = 5
    daily_limit: int = 0
    rpm_limit: int = 0
    use_proxy: bool = False
    protocol: str = "openai"
    extra_headers: dict = field(default_factory=dict)
    is_vision: bool = True
    in_pool: bool = False  # 是否加入聚合池（默认不加入）
    check_fake_success: bool = False  # 是否检测假成功（200 OK 但内容含拒绝信息）
    tool_call_id_prefix: str = ""
    stream_first_packet_timeout: int = 120
    stream_stall_timeout: int = 60
    stream_max_duration: int = 300  # 流总时长上限（秒），0=禁用；防 keep-alive 型无限挂起（2026-08-13）
    deferrable: bool = True  # 是否可延迟切换（false=上游恢复时立即切回，不保留cache）
    max_context_k: int = 0  # 最大上下文长度（K=1000 tokens），0=不限

    _fail_count: int = field(default=0, repr=False)
    _req_timestamps: deque = field(default_factory=deque, repr=False)
    _rpm_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_error: str = field(default="", repr=False)
    _last_error_ts: float = field(default=0, repr=False)
    _last_success_ts: float = field(default=0, repr=False)
    _total_calls: int = field(default=0, repr=False)
    _total_failures: int = field(default=0, repr=False)
    _cooldown_until: float = field(default=0, repr=False)
    _defer_until: float = field(default=0, repr=False)  # 延迟切换到期时间，在此期间不切回此端点
    
    _today_used: int = field(default=0, repr=False)
    _today_date: str = field(default="", repr=False)
    health_mode: str = field(default="models")
    billing_mode: str = field(default="subscription")

    _transient_count: int = field(default=0, repr=False)
    _transient_window_start: float = field(default=0, repr=False)
    _health: str = field(default="unknown", repr=False) 
    _health_latency_ms: int = field(default=-1, repr=False)
    _health_last_check: float = field(default=0, repr=False)
    _health_error: str = field(default="", repr=False)


class AllEndpointsFailed(Exception):
    def __init__(self, errors: list):
        self.errors = errors
        super().__init__(f"All endpoints failed: {errors}")


# ============================================================
#  API Pool
# ============================================================

class APIPool:
    def __init__(self, endpoints=None):
        self._lock = threading.Lock()
        self._endpoints: list[Endpoint] = []
        self._current_endpoint_id = None  # 用端点ID追踪当前端点，而非位置索引
        self._manual_override_id = None  # 用户手动指定端点的ID，优先级覆盖路由选择
        self._last_reasoning_content = None  # 缓存上一轮返回的 reasoning_content，用于多轮对话补全
        self._last_reasoning_text = None  # 缓存上一轮返回的 reasoning_text（DeepSeek V4 request 字段名），用于多轮对话补全
        self._last_pool_activity: float = 0  # 上次池活跃时间（用于 defer 判断）
        self._fallback_lock_until: float = 0  # 终极兜底锁定截止时间（滑动窗口 300s）
        # 后台探活基础设施：冷却过期端点在后台线程探活，不阻塞请求路径
        self._probe_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="apipool-probe")
        self._probe_inflight = set()  # 正在后台探活的端点 id 集合（防重复入队）
        if endpoints:
            for ep in endpoints:
                self.add_endpoint(ep)

    def add_endpoint(self, ep):
        if isinstance(ep, dict):
            ep_dict = {k: v for k, v in ep.items() if k in Endpoint.__dataclass_fields__}
            # 新增时按组别自动设置健康检测模式（未显式指定时生效）
            if "health_mode" not in ep_dict:
                ep_dict["health_mode"] = "models" if ep_dict.get("billing_mode", "subscription") == "pay_per_use" else ("chat" if ep_dict.get("in_pool", False) else "models")
            if "billing_mode" not in ep_dict:
                ep_dict["billing_mode"] = "subscription"
            ep = Endpoint(**ep_dict)
        elif not ep.health_mode:
            ep.health_mode = "models" if ep.billing_mode == "pay_per_use" else ("chat" if ep.in_pool else "models")
        if not ep.id:
            import uuid
            ep.id = str(uuid.uuid4())
        ep._today_date = datetime.now().strftime("%Y-%m-%d")
        ep._today_used = token_tracker.get_today_usage_by_endpoint(ep.name)
        with self._lock:
            self._endpoints.append(ep)
            self._endpoints.sort(key=lambda e: e.priority)
            self._renumber_pool_priorities()

    def remove_endpoint(self, ep_id):
        with self._lock:
            self._endpoints = [e for e in self._endpoints if e.id != ep_id]
            if self._current_endpoint_id == ep_id:
                self._current_endpoint_id = None
            self._renumber_pool_priorities()
            if self._manual_override_id == ep_id:
                self._manual_override_id = None

    def set_enabled(self, ep_id, enabled):
        with self._lock:
            for ep in self._endpoints:
                if ep.id == ep_id:
                    ep.enabled = enabled

    def set_pool(self, ep_id, in_pool):
        with self._lock:
            for ep in self._endpoints:
                if ep.id == ep_id:
                    ep.in_pool = in_pool
                    # 手动移出池再移回 = 显式信任该端点 → 清除延迟切换状态，恢复最高优先级
                    if in_pool:
                        ep._defer_until = 0
                    break
            self._renumber_pool_priorities()

    def update_endpoint(self, ep_id, updates: dict):
        with self._lock:
            for ep in self._endpoints:
                if ep.id == ep_id:
                    new_priority = updates.get("priority")
                    old_priority = ep.priority

                    # 先处理所有字段
                    for k, v in updates.items():
                        if hasattr(ep, k) and not k.startswith("_") and k != "id":
                            setattr(ep, k, v)
                    # cooldown_minutes 最低 1，防止跳过冷却恢复流程
                    if updates.get("cooldown_minutes") is not None and updates["cooldown_minutes"] < 1:
                        ep.cooldown_minutes = 1

                    # 池内端点显式改优先级 → insert-at-position
                    if new_priority is not None and ep.in_pool:
                        ep.priority = new_priority  # 确保目标准确
                        if new_priority < old_priority:
                            # 提升：[new, old) 区间的其他端点 +1
                            for other in self._endpoints:
                                if other is not ep and other.in_pool:
                                    if new_priority <= other.priority < old_priority:
                                        other.priority += 1
                        elif new_priority > old_priority:
                            # 降级：(old, new] 区间的其他端点 -1
                            for other in self._endpoints:
                                if other is not ep and other.in_pool:
                                    if old_priority < other.priority <= new_priority:
                                        other.priority -= 1
                    else:
                        # 非 priority 变更或池外端点，只保证池内连续
                        self._renumber_pool_priorities()

                    self._endpoints.sort(key=lambda e: e.priority)
                    break

    def _renumber_pool_priorities(self):
        """对池内端点分配唯一连续优先级，池外端点不动。"""
        pool_eps = sorted([ep for ep in self._endpoints if ep.in_pool], key=lambda e: e.priority)
        for i, ep in enumerate(pool_eps):
            ep.priority = i + 1

    def switch_to_endpoint(self, ep_id):
        with self._lock:
            for ep in self._endpoints:
                if ep.id == ep_id:
                    self._manual_override_id = ep_id
                    return True
        return False

    def list_endpoints(self):
        self._cleanup_expired_cooldowns()
        now = time.time()
        with self._lock:
            current_ep = None
            current_display_id = self._manual_override_id or self._current_endpoint_id
            if current_display_id:
                current_ep = next((ep for ep in self._endpoints if ep.id == current_display_id), None)
            return [self._ep_to_dict(ep, ep is current_ep, now) for ep in self._endpoints]

    def _ep_to_dict(self, ep, is_current, now):
        return {
            "id": ep.id,
            "name": ep.name,
            "base_url": ep.base_url,
            "api_key": ep.api_key[:8] + "***" if len(ep.api_key) > 8 else "***",
            "api_key_full": ep.api_key,
            "model": ep.model,
            "priority": ep.priority,
            "timeout": ep.timeout,
            "max_retries": ep.max_retries,
            "enabled": ep.enabled,
            "cooldown_minutes": ep.cooldown_minutes,
            "daily_limit": ep.daily_limit,
            "today_used": ep._today_used,
            "rpm_limit": ep.rpm_limit,
            "use_proxy": ep.use_proxy,
            "protocol": ep.protocol,
            "health_mode": ep.health_mode,
            "billing_mode": ep.billing_mode,
            "is_vision": ep.is_vision,
            "in_pool": ep.in_pool,
            "check_fake_success": ep.check_fake_success,
            "tool_call_id_prefix": ep.tool_call_id_prefix,
            "is_rpm_limited": self._is_rpm_limited(ep),
            "fail_count": ep._fail_count,
            "last_error": ep._last_error,
            "last_success": ep._last_success_ts,
            "total_calls": ep._total_calls,
            "total_failures": ep._total_failures,
            "is_current": is_current,
            "in_cooldown": ep._cooldown_until > now,
            "cooldown_remaining": max(0, int(ep._cooldown_until - now)),
            "cooldown_until": ep._cooldown_until,
            "deferrable": ep.deferrable,
            "is_deferred": ep._defer_until > now,
            "defer_remaining": max(0, int(ep._defer_until - now)),
            "max_context_k": ep.max_context_k,
            "health": ep._health,
            "health_latency_ms": ep._health_latency_ms,
            "health_last_check": ep._health_last_check,
            "health_error": ep._health_error,
        }

    def get_active_chain(self):
        self._cleanup_expired_cooldowns()
        now = time.time()
        with self._lock:
            active = self._active_endpoints()
            current_ep = None
            current_display_id = self._manual_override_id or self._current_endpoint_id
            if current_display_id:
                current_ep = next((ep for ep in active if ep.id == current_display_id), None)
            return [
                {
                    "name": ep.name,
                    "model": ep.model,
                    "priority": ep.priority,
                    "is_current": ep is current_ep,
                    "fail_count": ep._fail_count,
                    "in_cooldown": ep._cooldown_until > now,
                    "cooldown_remaining": max(0, int(ep._cooldown_until - now)),
                    "deferrable": ep.deferrable,
                    "is_deferred": ep._defer_until > now,
                    "defer_remaining": max(0, int(ep._defer_until - now)),
            "max_context_k": ep.max_context_k,
                    "daily_limit": ep.daily_limit,
                    "today_used": ep._today_used,
                    "rpm_limit": ep.rpm_limit,
                    "use_proxy": ep.use_proxy,
                    "is_rpm_limited": self._is_rpm_limited(ep),
                    "is_vision": ep.is_vision,
            "in_pool": ep.in_pool,
                    "health": ep._health,
                    "health_latency_ms": ep._health_latency_ms,
                    "health_error": ep._health_error,
                }
                for ep in active
            ]

    def reset(self):
        with self._lock:
            for ep in self._endpoints:
                ep._fail_count = 0
                ep._last_error = ""
                ep._last_error_ts = 0
                ep._cooldown_until = 0
                ep._defer_until = 0
                with ep._rpm_lock:
                    ep._req_timestamps.clear()
            self._current_endpoint_id = None
            self._manual_override_id = None

    def reset_to_priority_mode(self):
        with self._lock:
            active = [ep for ep in self._endpoints if ep.enabled and ep.in_pool
                      and not self._is_in_cooldown(ep)
                      and not self._is_quota_exceeded(ep)
                      and not self._is_rpm_limited(ep)]
            if not active:
                return False
            best = min(active, key=lambda ep: ep.priority)
            self._current_endpoint_id = best.id
            self._manual_override_id = None
            return True

    def _check_one_health(self, ep):
        if ep.health_mode == "none":
            return ep.id, "unknown", -1, "已禁用健康检测"

        # 按所在组别自动决定检测模式：池内用chat（验证真实可用），池外用models（零消耗探测）
        # 按次计费的端点强制使用 models 检测，避免产生费用
        if ep.billing_mode == "pay_per_use":
            effective_mode = "models"
        else:
            effective_mode = "chat" if ep.in_pool else "models"

        if effective_mode == "models":
            t0 = time.time()
            try:
                models = self.fetch_models(ep.base_url, ep.api_key, timeout=10, use_proxy=ep.use_proxy, protocol=ep.protocol)
                latency = int((time.time() - t0) * 1000)
                if models:
                    return ep.id, "ok", latency, ""
                else:
                    return ep.id, "bad", latency, "获取模型列表失败"
            except Exception as e:
                return ep.id, "bad", int((time.time() - t0) * 1000), f"Models接口错误: {e}"[:100]
                
        payload = {"model": ep.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 3}
        
        # Attempt 1
        t0 = time.time()
        reply, err = self._try_endpoint(ep, payload, timeout=10, log_usage=False, force_no_retry=True)
        latency = int((time.time() - t0) * 1000)
        
        if reply is not None and latency <= LATENCY_OK_MAX:
            return ep.id, "ok", latency, ""
            
        # Evaluate if we should retry
        err_str = err[:100] if err else ""
        hard_errors = ["auth error", "400", "401", "403", "404", "429"]
        if any(code in err_str for code in hard_errors):
            return ep.id, "bad", latency, err_str
            
        # Attempt 2 (Retry for cold start or transient glitch)
        t1 = time.time()
        reply2, err2 = self._try_endpoint(ep, payload, timeout=10, log_usage=False, force_no_retry=True)
        latency2 = int((time.time() - t1) * 1000)
        
        if reply2 is not None and latency2 <= LATENCY_OK_MAX:
            return ep.id, "ok", latency2, ""
            
        # If retry also fails or isn't fast enough, return the original attempt's status
        if reply is not None:
            if latency <= LATENCY_SLOW_MAX:
                return ep.id, "slow", latency, ""
            else:
                return ep.id, "bad", latency, f"延迟过高: {latency}ms"
        else:
            return ep.id, "bad", latency, err_str or "未知错误"

    def _has_images(self, messages):
        if not messages: return False
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                for c in content:
                    if c.get("type") == "image_url": return True
        return False

    def _translate_images_sync(self, messages, active_eps):
        vision_eps = [e for e in active_eps if getattr(e, "is_vision", True)]
        if not vision_eps:
            return messages
            
        translation_msgs = []
        for m in messages:
            if isinstance(m.get("content"), list):
                new_content = []
                has_image = False
                for c in m["content"]:
                    if c.get("type") == "image_url":
                        has_image = True
                        new_content.append(c)
                if has_image:
                    translation_msgs.append({"role": "user", "content": new_content})
        
        if not translation_msgs: return messages
        
        sys_prompt = "你是一个专业图像解析器。请将用户提供的图片内容转化为极其详细的文字描述（包括画面细节、OCR文字、代码片段等），只输出文字描述，不要有多余的客套话。"
        translation_msgs.insert(0, {"role": "system", "content": sys_prompt})
        
        description = ""
        for v_ep in vision_eps:
            sys_log(f"启动图片解析 -> 尝试端点 {v_ep.name} ({v_ep.model})", "INFO")
            payload = {"model": v_ep.model, "messages": translation_msgs, "stream": False, "max_tokens": 4096}
            result, error = self._try_endpoint(v_ep, payload, timeout=60, log_usage=True, force_no_retry=True)
            if error:
                sys_log(f"图片解析失败 ({v_ep.name} - {v_ep.model}): {error}", "WARNING")
                continue
                
            description = result if isinstance(result, str) else result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not description.strip():
                reasoning = result.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "")
                if reasoning:
                    description = reasoning
            if description:
                break
                
        if not description:
            sys_log("所有图片解析端点均失败", "ERROR")
            return messages
        
        import copy
        new_msgs = copy.deepcopy(messages)
        for m in new_msgs:
            if isinstance(m.get("content"), list):
                has_image = False
                filtered_content = []
                for c in m["content"]:
                    if c.get("type") != "image_url":
                        filtered_content.append(c)
                    else:
                        has_image = True
                if has_image:
                    filtered_content.append({"type": "text", "text": f"\n\n[图片解析内容]: {description}"})
                m["content"] = filtered_content
        sys_log("图片解析完成", "INFO")
        return new_msgs

    def check_all_health(self):
        with self._lock:
            endpoints = [ep for ep in self._endpoints if ep.enabled and ep.in_pool]
            for ep in endpoints:
                ep._health = "testing"
        if not endpoints:
            return []
        results = []
        with ThreadPoolExecutor(max_workers=min(len(endpoints), 10)) as pool_exec:
            futures = {pool_exec.submit(self._check_one_health, ep): ep for ep in endpoints}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    ep = futures[future]
                    results.append((ep.id, "bad", -1, str(e)))
        now = time.time()
        with self._lock:
            id_map = {ep.id: ep for ep in self._endpoints}
            for ep_id, health, latency, error in results:
                ep = id_map.get(ep_id)
                if ep:
                    ep._health = health
                    ep._health_latency_ms = latency
                    ep._health_last_check = now
                    ep._health_error = error
        sys_log(f"健康检测完成: 检测了 {len(endpoints)} 个端点", "INFO")
        return [{"id": i, "health": h, "latency_ms": l, "error": e} for i, h, l, e in results]

    def _is_in_cooldown(self, ep):
        return ep._cooldown_until > time.time()

    # 终极兜底（priority=99）参数：正常轮转预算 530s，保底容错 60s（530+60=590 < Hermes 600s），
    # 锁定滑动窗口 300s（5 分钟无新请求视为任务结束，解锁后重新评估故障端点）
    _FALLBACK_DEADLINE_SECONDS = 530
    _FALLBACK_TIMEOUT_SECONDS = 60
    _FALLBACK_LOCK_SECONDS = 300

    def _get_fallback_endpoint(self):
        """返回优先级 99 的终极兜底端点（启用、在池、未冷却）。"""
        for ep in self._endpoints:
            if (ep.enabled and ep.in_pool and ep.priority == 99
                    and not self._is_in_cooldown(ep) and not self._is_quota_exceeded(ep)):
                return ep
        return None

    def _is_fallback_locked(self):
        return self._fallback_lock_until > time.time()

    def _is_quota_exceeded(self, ep):
        if ep.daily_limit <= 0: return False
        now_date = datetime.now().strftime("%Y-%m-%d")
        if ep._today_date != now_date:
            ep._today_date = now_date
            ep._today_used = 0
        return ep._today_used >= ep.daily_limit

    def _is_rpm_limited(self, ep):
        if ep.rpm_limit <= 0: return False
        now = time.time()
        with ep._rpm_lock:
            while ep._req_timestamps and ep._req_timestamps[0] < now - 60:
                ep._req_timestamps.popleft()
            return len(ep._req_timestamps) >= ep.rpm_limit

    def _is_deferred(self, ep):
        return ep._defer_until > time.time()

    def _set_cooldown(self, ep):
        # 幂等：已在冷却中不刷新冻结时间（并发请求的失败是同一故障的重复观测，
        # 不延长冷却窗口；fail_count 仍由调用方累加）。窗口过期后的新失败自然触发新冻结。
        if ep._cooldown_until > time.time():
            return
        cd = max(ep.cooldown_minutes, 1)
        ep._cooldown_until = time.time() + cd * 60

    def _clear_cooldown(self, ep):
        ep._cooldown_until = 0

    def clear_error(self, ep_id):
        """手动清除端点错误状态（解冻），不改配置值"""
        with self._lock:
            for ep in self._endpoints:
                if ep.id == ep_id:
                    ep._cooldown_until = 0
                    ep._defer_until = 0
                    ep._fail_count = 0
                    ep._last_error = ""
                    ep._last_error_ts = 0
                    ep._health = "ok"
                    ep._transient_count = 0
                    ep._transient_window_start = 0
                    sys_log(f"端点 '{ep.name}' 手动解冻，错误状态已清除", "INFO")
                    return True
        return False

    def _cleanup_expired_cooldowns(self):
        """冷却过期端点：收集后丢给后台线程探活，不阻塞请求路径（2026-08-13 后台化改造）。

        探活结果在后台更新端点状态；下次请求自然使用恢复的端点（defer 逻辑兼容保留）。
        """
        now = time.time()
        defer_window = 300  # 5分钟活跃窗口
        expired = []
        with self._lock:
            for ep in self._endpoints:
                if ep.in_pool and ep._cooldown_until > 0 and ep._cooldown_until <= now:
                    expired.append(ep)

        for ep in expired:
            with self._lock:
                # 去重：30秒内已探活过的跳过，避免并发入口重复探活
                if now - ep._health_last_check < 30:
                    continue
                # 已在后台探活中的跳过（防重复入队）
                if ep.id in self._probe_inflight:
                    continue
                # 探活前快照当前端点：探活耗时数秒，期间并发请求可能修改 _current_endpoint_id，
                # 用快照保证 defer 判断基于探活开始时的状态（避免 999554"已恢复"竞态）
                probe_current_id = self._manual_override_id or self._current_endpoint_id
                self._probe_inflight.add(ep.id)
            self._probe_executor.submit(self._background_probe, ep, probe_current_id)

        # 滚动处理已延迟的端点：池仍活跃则延长 defer，池空闲/当前不可 defer 则解除
        self._reconcile_deferred(now, defer_window)

    def _background_probe(self, ep, probe_current_id):
        """后台探活单个端点：通过→清冷却+defer判断+更新current指针；失败→继续冷却。"""
        try:
            if self._probe_endpoint(ep):
                now = time.time()
                with self._lock:
                    ep._fail_count = 0
                    ep._last_error = ""
                    ep._last_error_ts = 0
                    ep._cooldown_until = 0
                    ep._health = "ok"
                    ep._transient_count = 0
                    ep._transient_window_start = 0
                # 判断是否延迟切换（保护当前端点 cache）
                current_ep = None
                if probe_current_id:
                    current_ep = next((e for e in self._endpoints if e.id == probe_current_id), None)
                pool_active = self._last_pool_activity > now - 300
                if current_ep and current_ep is not ep and current_ep.deferrable and pool_active:
                    with self._lock:
                        ep._defer_until = now + 300
                    sys_log(f"端点 '{ep.name}' 冷却过期探活通过，但池活跃（当前在 '{current_ep.name}'），延迟切换 5 分钟以保持 cache", "INFO")
                else:
                    with self._lock:
                        ep._defer_until = 0
                    sys_log(f"端点 '{ep.name}' 冷却过期探活通过，已恢复", "INFO")
                # 更新 current 指针（保留手动覆盖）
                with self._lock:
                    if not self._manual_override_id:
                        active = self._active_endpoints()
                        if active:
                            best = min(active, key=lambda e: e.priority)
                            self._current_endpoint_id = best.id
            else:
                with self._lock:
                    self._set_cooldown(ep)
                sys_log(f"端点 '{ep.name}' 冷却过期探活未通过，继续冷却 {ep.cooldown_minutes} 分钟", "WARN")
        except Exception as e:
            sys_log(f"端点 '{ep.name}' 后台探活异常: {e}", "ERROR")
        finally:
            with self._lock:
                self._probe_inflight.discard(ep.id)

    def _reconcile_deferred(self, now=None, defer_window=300):
        """已延迟切换的端点：池仍活跃且当前端点可 defer → 滚动延长；池空闲/当前不可 defer → 解除。

        实现「直到无响应为止」：活跃会话期间 defer 持续滚动，会话空闲 5 分钟后才解除，
        下一次请求自然切回最高优先级端点。
        """
        if now is None:
            now = time.time()
        with self._lock:
            current_ep = None
            current_display_id = self._manual_override_id or self._current_endpoint_id
            if current_display_id:
                current_ep = next((e for e in self._endpoints if e.id == current_display_id), None)
            pool_active = self._last_pool_activity > now - defer_window
            for ep in self._endpoints:
                if not (ep.in_pool and ep._defer_until > 0):
                    continue
                # 注意：不再有 "ep is current_ep → 解除" 分支。
                # 兜底使用的 defer 清除已由 _on_success(clear_defer=True) 处理；
                # 这里若用实时 current 判断会误清"defer 前进入的并发请求"产生的 defer
                # （探活/请求期间 current 被并发修改，如场景9 竞态）。
                if current_ep and not current_ep.deferrable:
                    ep._defer_until = 0  # 当前端点不可 defer（昂贵兜底）→ 立即恢复上游
                    sys_log(f"端点 '{ep.name}' 延迟切换解除（当前端点 '{current_ep.name}' 不可延迟）", "INFO")
                elif pool_active:
                    ep._defer_until = now + defer_window  # 滚动延长，保持 cache
                else:
                    ep._defer_until = 0  # 池已空闲 → 解除延迟，下次切回
                    sys_log(f"端点 '{ep.name}' 延迟切换解除（池空闲）", "INFO")

    def _active_endpoints(self):
        available = [ep for ep in self._endpoints if ep.enabled and ep.in_pool and not self._is_in_cooldown(ep) and not self._is_quota_exceeded(ep) and not self._is_rpm_limited(ep) and not self._is_deferred(ep)]
        if available:
            return available
        # 最后手段：全池无可用 → 返回优先级 99 终极兜底端点（若有）；否则空列表 → AllEndpointsFailed → Hermes fallback。
        # 不再返回冷却中的端点去试错（避免轮转超时 + Hermes 600s 叠加）。
        fb = self._get_fallback_endpoint()
        return [fb] if fb else []

    def _pick_best(self, active):
        for ep in active:
            if not self._is_in_cooldown(ep):
                return ep
        return min(active, key=lambda e: e._cooldown_until) if active else None

    def _rotate(self, failed_ep, error_msg, probe_failed=False):
        failed_ep._fail_count += 1
        failed_ep._total_failures += 1
        failed_ep._last_error = error_msg
        failed_ep._last_error_ts = time.time()
        if probe_failed:
            # 探活失败：只设短冷却（30秒），避免误杀冷启动端点
            failed_ep._cooldown_until = time.time() + 30
            sys_log(f"端点 '{failed_ep.name}' 探活失败，短冷却 30 秒", "WARN")
        else:
            self._set_cooldown(failed_ep)
            sys_log(f"端点 '{failed_ep.name}' 触发冷却机制，下次可用时间在 {failed_ep.cooldown_minutes} 分钟后", "WARN")
        active = self._active_endpoints()
        if active:
            for i, ep in enumerate(active):
                if ep is failed_ep:
                    next_idx = (i + 1) % len(active)
                    self._current_endpoint_id = active[next_idx].id
                    self._manual_override_id = None  # 健康检测自动切换时清除手动覆盖
                    return
            self._current_endpoint_id = active[0].id if active else None

    def _on_success(self, ep, result=None, clear_defer=True):
        now = time.time()
        self._last_pool_activity = now  # 记录池活跃时间（用于 defer 判断）
        # 仅在"请求开始时端点已在 defer（兜底使用）"时清除延迟状态。
        # 竞态保护：defer 设置前已进入的并发请求（请求开始时不在 defer）成功后
        # 不清 defer，避免并发请求破坏延迟切换保护窗口。
        if clear_defer:
            ep._defer_until = 0
        ep._total_calls += 1
        ep._last_success_ts = now
        ep._health = "ok"
        ep._fail_count = 0
        ep._transient_count = 0
        ep._transient_window_start = 0
        ep._last_error = ""
        # 仅在非冷却中清除冷却，防止并发请求穿透冷却保护（429→冷却→并发成功→清冷却→再429）
        if not self._is_in_cooldown(ep):
            self._clear_cooldown(ep)
        self._current_endpoint_id = ep.id
        if self._manual_override_id and self._manual_override_id != ep.id:
            self._manual_override_id = None  # 手动覆盖端点失败后落到其他端点，清除覆盖
        # 缓存 reasoning_content/reasoning_text 用于多轮对话
        if result and isinstance(result, dict):
            try:
                msg = result.get("choices", [{}])[0].get("message", {})
                rc = msg.get("reasoning_content")
                if rc:
                    self._last_reasoning_content = rc
                    self._last_reasoning_text = rc
                rt = msg.get("reasoning_text")
                if rt:
                    self._last_reasoning_text = rt
            except (IndexError, KeyError):
                pass

    def _rewrite_tool_call_ids(self, messages, prefix):
        """确定性重写 tool_call id：原 id → 前缀+md5 后缀，保持 assistant/tool 配对。

        DeepSeek 官方（如 Kcne）校验历史里 tool_call id 必须为其生成的格式（call_00_ET_*），
        跨端点切换后历史混入其他端点生成的 id 会导致 HTTP 400。重写为统一前缀后，
        服务端视为新消息跳过校验。md5 保证确定性（跨轮次稳定，不破坏缓存）且配对一致。
        """
        import hashlib
        mapping = {}
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    oid = tc.get("id", "")
                    if oid and not oid.startswith(prefix):
                        if oid not in mapping:
                            mapping[oid] = prefix + hashlib.md5(oid.encode()).hexdigest()[:16]
                        tc["id"] = mapping[oid]
            elif m.get("role") == "tool" and m.get("tool_call_id"):
                oid = m.get("tool_call_id")
                if oid and oid in mapping:
                    m["tool_call_id"] = mapping[oid]
        return messages

    def _estimate_context_tokens(self, messages):
        """粗略估算 prompt 上下文 token 数（1K=1000 tokens），用于 max_context_k 判断。

        从 messages 提取所有文本内容，按 1 token ≈ 3 字符（中英文混合保守估计）。
        仅用于防护性跳过，不追求精确计数。
        """
        total_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        total_chars += len(c.get("text", ""))
        return total_chars // 3  # 中英文混合平均

    def _probe_endpoint(self, ep):
        """对单个端点做快速探活，成功返回 True，失败返回 False。统一用 chat ping 检测。"""
        if ep.health_mode == "none":
            return True  # 关闭检测视为可用
        # 统一使用 chat ping 探活：models 接口可访问不代表模型可响应
        ep._health_last_check = time.time()
        payload = {"model": ep.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 3}
        reply, err = self._try_endpoint(ep, payload, timeout=10, log_usage=False, force_no_retry=True)
        return reply is not None

    def chat(self, messages, model=None, extra_payload=None, timeout=None, return_endpoint=False):
        self._cleanup_expired_cooldowns()
        _chat_start = time.time()  # 530s 轮转预算计时起点（与 Hermes 600s 超时窗口对齐）
        # 终极兜底锁定：锁定期间所有请求直连 prio99（全局滑动窗口）
        if self._is_fallback_locked():
            fb = self._get_fallback_endpoint()
            if fb is not None:
                sys_log(f"终极兜底锁定中，直连端点 '{fb.name}'", "INFO")
                active = [fb]
            else:
                with self._lock:
                    self._fallback_lock_until = 0  # 兜底不可用，解除锁定
                active = self._active_endpoints()
        else:
            active = self._active_endpoints()
        if not active:
            raise ValueError("没有可用的 API 端点")
        errors = []
        tried = 0
        total = len(active)
        # 按优先级排序：每次从最高优先级端点开始尝试，故障自动降级，恢复后自动回迁
        active.sort(key=lambda e: e.priority)
        # 修复消息顺序：tool 消息前缺少 tool_calls 时自动补 assistant 回复
        needs_fix = False
        tool_call_ids = set()
        for m in messages:
            for tc in (m.get("tool_calls") or []):
                tool_call_ids.add(tc.get("id", ""))
        for m in messages:
            if m.get("role") == "tool" and m.get("tool_call_id", "") not in tool_call_ids:
                needs_fix = True
                break
        if needs_fix:
            fixed = []
            seen_tc_ids = set()
            for m in messages:
                if m.get("role") == "tool":
                    tc_id = m.get("tool_call_id", "")
                    if tc_id and tc_id not in seen_tc_ids:
                        fixed.append({"role": "assistant", "content": None, "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": "resolved", "arguments": "{}"}}]})
                elif m.get("role") == "assistant":
                    for tc in (m.get("tool_calls") or []):
                        seen_tc_ids.add(tc.get("id", ""))
                fixed.append(m)
            messages = fixed

        
        # 补全 reasoning_content：找到最后一个 assistant 消息，如果没有则注入缓存值
        # 手动覆盖：如果用户通过UI指定了端点，优先走该端点
        if self._manual_override_id:
            override_ep = next((ep for ep in active if ep.id == self._manual_override_id), None)
            if override_ep:
                active.remove(override_ep)
                active.insert(0, override_ep)
        idx = 0
        while tried < total:
            ep = active[idx]
            # 并发保护：端点已被其他请求冻结时，跳过它转向下一个可用端点。
            # 仅当池中还存在非冷却端点时才跳过；全部冷却时走 _active_endpoints 的
            # fallback（按解冻时间排序尝试），保留"宁可试冷却端点也不报错"的兜底语义。
            if self._is_in_cooldown(ep) and any(not self._is_in_cooldown(e) for e in active):
                tried += 1
                idx = (idx + 1) % total
                continue
            # 530s 预算：正常轮转超时 → 立即切终极兜底（不再继续轮转，避免 Hermes 600s 超时并发重试）
            if (time.time() - _chat_start) >= self._FALLBACK_DEADLINE_SECONDS:
                fb = self._get_fallback_endpoint()
                if fb is not None and fb is not ep and not self._is_in_cooldown(fb):
                    sys_log(f"轮转超 {self._FALLBACK_DEADLINE_SECONDS}s，切换终极兜底 '{fb.name}'", "WARN")
                    with self._lock:
                        self._fallback_lock_until = time.time() + self._FALLBACK_LOCK_SECONDS
                    for i, e in enumerate(active):
                        if e is fb:
                            idx = i
                            break
                    continue
            # 快照请求开始时的 defer 状态：用于 _on_success 判断是否"兜底使用"。
            # 竞态保护：defer 设置前已进入的请求（快照=0）成功后不清 defer。
            defer_at_request = ep._defer_until
            ep_timeout = timeout or ep.timeout
            ep_model = model or ep.model
            
            # 按端点协议决定消息体：Anthropic 端点不注入 reasoning_content（避免缓存穿透）
            loop_messages = messages
            is_anthropic = (getattr(ep, "protocol", "openai") == "anthropic")
            # tool_call id 前缀重写：端点配置 tool_call_id_prefix 非空时，把消息里所有 tool_call id
            # 重写为该前缀格式（如 DeepSeek 官方 call_00_ET_）。跨端点切换后历史里混入其他端点
            # 生成的 id（不同前缀/格式），Kcne 等 DeepSeek 官方会校验并报 400
            # "reasoning_text must be passed back"；重写为统一格式后服务端视为新消息跳过校验。
            ep_prefix = getattr(ep, "tool_call_id_prefix", "") or ""
            if ep_prefix and not is_anthropic:
                if loop_messages is messages:
                    loop_messages = list(messages)  # 避免原地修改原始消息
                self._rewrite_tool_call_ids(loop_messages, ep_prefix)
            
            payload = {
                "model": ep_model, "messages": loop_messages,
                **(extra_payload or {}),
            }
            
            # [VISION TRANSLATION INTERCEPT]
            if self._has_images(payload["messages"]) and getattr(ep, "is_vision", True) is False:
                has_vision = any(getattr(e, "is_vision", True) for e in active)
                if has_vision:
                    if payload.get("stream"):
                        def vision_wrapper(tgt_ep, pld, t_out, a_eps):
                            import json
                            yield f"data: {{'choices':[{{'delta':{{'content':'[API Pool: 检测到图片，当前目标不支持视觉，正在调用视觉模型进行解析...]\\n\\n'}}}}]}}\n\n".replace("'", '"')
                            translated_msgs = self._translate_images_sync(pld["messages"], a_eps)
                            yield f"data: {{'choices':[{{'delta':{{'content':'[图片解析完成，交由目标模型继续处理...]\\n\\n'}}}}]}}\n\n".replace("'", '"')
                            pld["messages"] = translated_msgs
                            gen, err = self._try_endpoint(tgt_ep, pld, t_out)
                            if err:
                                yield f"data: {{'choices':[{{'delta':{{'content':'\\n\\n[API Pool Error: 请求最终目标失败: {err}]'}}}}]}}\n\n".replace("'", '"')
                            else:
                                # 实际请求成功后标记端点成功
                                # clear_defer：仅当请求开始时端点已在 defer（兜底使用）才清除
                                with self._lock:
                                    self._on_success(tgt_ep, clear_defer=defer_at_request > 0)
                                yield from gen
                        return vision_wrapper(ep, payload, ep_timeout, active)
                    else:
                        payload["messages"] = self._translate_images_sync(payload["messages"], active)
            
            if tried == 0:
                sys_log(f"收到 API 请求，尝试请求端点 '{ep.name}' (模型: {ep_model})", "INFO")
            else:
                sys_log(f"重试请求，尝试端点 '{ep.name}' (模型: {ep_model})", "INFO")

            # 上下文长度检查：超过限制时跳过该端点（不冻结、不记失败），轮转到下一个
            if ep.max_context_k > 0:
                estimated = self._estimate_context_tokens(loop_messages)
                if estimated > ep.max_context_k * 1000:
                    sys_log(f"端点 '{ep.name}' 上下文约 {estimated}t 超过限制 {ep.max_context_k}K，跳过", "INFO")
                    tried += 1
                    idx = (idx + 1) % total
                    continue

            result, error = self._try_endpoint(ep, payload, ep_timeout, force_no_retry=(getattr(ep, "priority", 0) == 99))
            if result is not None:
                with self._lock:
                    self._on_success(ep, result, clear_defer=defer_at_request > 0)
                    # 终极兜底成功 → 滑动刷新锁定窗口（5 分钟无新请求视为任务结束）
                    if getattr(ep, "priority", 0) == 99:
                        self._fallback_lock_until = time.time() + self._FALLBACK_LOCK_SECONDS
                sys_log(f"端点 '{ep.name}' 请求成功 (延迟: 正常)", "INFO")
                if return_endpoint: return result, ep
                return result
            errors.append(f"[{ep.name}] {error}")
            sys_log(f"端点 '{ep.name}' 请求失败: {error}", "ERROR")
            # _try_endpoint 内部已按 max_retries 重试完毕，直接冻结+轮转
            with self._lock:
                self._rotate(ep, error)
                active = self._active_endpoints()
                active.sort(key=lambda e: e.priority)
                total = len(active)
                if total == 0:
                    break
                tried = 0  # 轮转后重置尝试计数，用刷新后的 total 重新计算
                # 按优先级逐个探活下一个候选端点
                next_ep = None
                for i, e in enumerate(active):
                    if e is not ep and not self._is_in_cooldown(e):
                        next_ep = e
                        idx = i
                        break
                if next_ep is None:
                    for i, e in enumerate(active):
                        if e is ep:
                            idx = (i + 1) % len(active)
                            break
                else:
                    # 对候选端点做探活
                    sys_log(f"对候选端点 '{next_ep.name}' 进行探活...", "INFO")
                    if self._probe_endpoint(next_ep):
                        sys_log(f"候选端点 '{next_ep.name}' 探活通过，准备重试请求", "INFO")
                        continue  # 探活通过，回到循环顶部用 next_ep 发起实际请求
                    else:
                        sys_log(f"候选端点 '{next_ep.name}' 探活失败，跳过", "WARN")
                        self._rotate(next_ep, "探活失败", probe_failed=True)
                        # 下一级探活也失败 → 并发探活剩余所有候选端点。
                        # 用 _check_one_health（含 Attempt 2 重试，最坏 20s）做参考性探测；
                        # 两阶段等待：11s 内返回结果的端点直接按优先级使用，最多等 21s。
                        remaining = [e for e in active if e is not ep and e is not next_ep and not self._is_in_cooldown(e)]
                        if remaining:
                            sys_log(f"并发探活剩余 {len(remaining)} 个候选端点...", "INFO")
                            probe_results = {}
                            chosen = None
                            _deadline11 = time.time() + 11
                            _deadline21 = time.time() + 21
                            with ThreadPoolExecutor(max_workers=min(len(remaining), 10)) as _pool:
                                _futures = {_pool.submit(self._check_one_health, e): e for e in remaining}
                                _pending = set(_futures)
                                # 阶段1：11s 内谁先返回结果，按优先级取第一个通过的立即使用
                                while _pending and time.time() < _deadline11:
                                    _done, _pending = wait(_pending, timeout=max(0.1, _deadline11 - time.time()))
                                    for _fut in _done:
                                        _e = _futures[_fut]
                                        try:
                                            _eid, _health, _lat, _err = _fut.result()
                                            probe_results[_e] = _health
                                        except Exception:
                                            probe_results[_e] = "bad"
                                    for _e in sorted(remaining, key=lambda x: x.priority):
                                        if probe_results.get(_e) in ("ok", "slow") and _e is not ep:
                                            chosen = _e
                                            break
                                    if chosen is not None:
                                        break
                                # 阶段2：11s 内没有可用端点，继续等剩余结果，最多到 21s
                                if chosen is None:
                                    while _pending and time.time() < _deadline21:
                                        _done, _pending = wait(_pending, timeout=max(0.1, _deadline21 - time.time()))
                                        for _fut in _done:
                                            _e = _futures[_fut]
                                            try:
                                                _eid, _health, _lat, _err = _fut.result()
                                                probe_results[_e] = _health
                                            except Exception:
                                                probe_results[_e] = "bad"
                                    for _e in sorted(remaining, key=lambda x: x.priority):
                                        if probe_results.get(_e) in ("ok", "slow") and _e is not ep:
                                            chosen = _e
                                            break
                            if chosen is not None:
                                sys_log(f"候选端点 '{chosen.name}' 并发探活通过，准备重试请求", "INFO")
                                for i, e in enumerate(active):
                                    if e is chosen:
                                        idx = i
                                        break
                                # 其余探活失败的端点标记短冷却，避免后续轮转再撞上
                                for e in remaining:
                                    if e is not chosen and probe_results.get(e) not in ("ok", "slow") and not self._is_in_cooldown(e):
                                        self._rotate(e, "并发探活失败", probe_failed=True)
                                continue
                            else:
                                # 全部探活失败：全部短冷却，回到循环顶部走 fallback 兜底
                                sys_log(f"剩余候选端点全部探活失败", "WARN")
                                for e in remaining:
                                    if not self._is_in_cooldown(e):
                                        self._rotate(e, "并发探活失败", probe_failed=True)
                                active = self._active_endpoints()
                                active.sort(key=lambda e: e.priority)
                                total = len(active)
                                if total == 0:
                                    break
                                next_ep2 = None
                                for i, e in enumerate(active):
                                    if e is not ep and e is not next_ep and not self._is_in_cooldown(e):
                                        next_ep2 = e
                                        idx = i
                                        break
                                if next_ep2 is None:
                                    # 没有更多候选了，尝试任何非当前端点
                                    for i, e in enumerate(active):
                                        if e is ep:
                                            idx = (i + 1) % len(active)
                                            break
                tried += 1
        raise AllEndpointsFailed(errors)

    def _try_endpoint(self, ep, payload, timeout, log_usage=True, force_no_retry=False):
        req_t0 = time.time()
        prompt_text_to_log = extract_prompt_text(payload) if log_usage and not ep.name.startswith("test_") else ""
        
        # 协议层处理：Anthropic 端点做完整格式转换以保证 Kcne 缓存 key 一致性
        is_anthropic = (getattr(ep, "protocol", "openai") == "anthropic")
        if is_anthropic:
            url = ep.base_url.rstrip("/") + "/messages"
            anthropic_payload = {
                "model": payload.get("model", ep.model),
                "max_tokens": payload.get("max_tokens", 4096),
            }
            if "temperature" in payload: anthropic_payload["temperature"] = payload["temperature"]
            if "top_p" in payload: anthropic_payload["top_p"] = payload["top_p"]
            if "stream" in payload: anthropic_payload["stream"] = payload["stream"]
            sys_prompt = ""
            messages = []
            for m in payload.get("messages", []):
                if m.get("role") == "system":
                    sys_prompt += m.get("content", "") + "\n"
                else:
                    role = m.get("role")
                    content = m.get("content")
                    if isinstance(content, list):
                        new_content = []
                        for c in content:
                            if c.get("type") == "image_url":
                                url_val = c.get("image_url", {}).get("url", "")
                                if url_val.startswith("data:image/"):
                                    try:
                                        media_type = url_val.split(";")[0].replace("data:", "")
                                        b64_data = url_val.split(",")[1]
                                        new_content.append({
                                            "type": "image",
                                            "source": {"type": "base64", "media_type": media_type, "data": b64_data}
                                        })
                                    except Exception:
                                        pass
                                else:
                                    new_content.append({"type": "text", "text": f"[Image URL: {url_val}]"})
                            else:
                                new_content.append(c)
                        messages.append({"role": role, "content": new_content})
                    else:
                        messages.append(m)
            if sys_prompt:
                anthropic_payload["system"] = sys_prompt.strip()
            anthropic_payload["messages"] = messages
            data = json.dumps(anthropic_payload).encode("utf-8")
        else:
            url = ep.base_url.rstrip("/") + "/chat/completions"
            data = json.dumps(payload).encode("utf-8")
            
        is_stream = payload.get("stream", False)
        
        retries = 0 if force_no_retry else ep.max_retries
        for attempt in range(retries + 1):
            if ep.rpm_limit > 0:
                with ep._rpm_lock:
                    ep._req_timestamps.append(time.time())
                    
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            safe_api_key = ep.api_key.encode('ascii', 'ignore').decode('ascii').strip()
            if is_anthropic:
                req.add_header("x-api-key", safe_api_key)
                req.add_header("anthropic-version", "2023-06-01")
            req.add_header("Authorization", f"Bearer {safe_api_key}")
            req.add_header("User-Agent", "OpenAI/Python 2.33.0")
                
            for k, v in ep.extra_headers.items():
                req.add_header(k, v)
                
            try:
                if getattr(ep, "use_proxy", True) is False:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    resp = opener.open(req, timeout=timeout)
                else:
                    resp = urllib.request.urlopen(req, timeout=timeout)
                
                if is_stream:
                    # Stream first-packet pre-read: timeout retries, not freeze
                    first_line = b""
                    _first_pkt_timeout = getattr(ep, "stream_first_packet_timeout", 0)
                    if _first_pkt_timeout > 0:
                        _sock1 = _get_resp_socket(resp)
                        if _sock1 is not None:
                            try:
                                _sock1.settimeout(_first_pkt_timeout)
                                first_line = resp.readline()
                            except socket.timeout:
                                sys_log(f"endpoint {chr(39)}{ep.name}{chr(39)} stream first packet timeout ({_first_pkt_timeout}s) attempt {attempt+1}", "WARN")
                                try:
                                    resp.close()
                                except Exception:
                                    pass
                                if attempt < retries:
                                    time.sleep(1.5 * (attempt + 1))
                                    continue
                                return None, f"stream first packet timeout ({_first_pkt_timeout}s)"
                            except Exception as e:
                                sys_log(f"端点 '{ep.name}' 首包预读 socket 不可用({e})，依赖请求超时/总时长兜底", "WARN")
                    def stream_generator():
                        stream_id = f"chatcmpl-{int(time.time()*1000)}"
                        final_prompt_tokens = 0
                        final_completion_tokens = 0
                        final_total_tokens = 0
                        final_cached_tokens = 0
                        has_usage = False
                        final_completion_text = ""
                        stall_timeout = getattr(ep, "stream_stall_timeout", 0)
                        _sock2 = _get_resp_socket(resp)
                        if _sock2 is not None and stall_timeout > 0:
                            try:
                                _sock2.settimeout(stall_timeout)
                            except Exception:
                                pass

                        def _timeout_abort(reason):
                            """停滞/超时长统一终止：日志 + 冷却 + error chunk（2026-08-13 修复，不再静默吞）"""
                            sys_log(f"端点 '{ep.name}' {reason}", "ERROR")
                            with self._lock:
                                ep._fail_count += 1
                                ep._last_error = reason
                                ep._last_error_ts = time.time()
                                self._set_cooldown(ep)
                            yield b'data: {"choices":[{"delta":{"content":"\\n\\n[API Pool Error: ' + reason.encode("utf-8", "ignore") + b']"},"finish_reason":"stop"}]}\n\n'
                            yield b"data: [DONE]\n\n"

                        # 流总时长上限：循环内绝对时间检查。
                        # 说明（2026-08-13）：真实停滞场景是 SSE keep-alive（带换行注释行）→
                        # readline 每行返回、循环体能执行 → 此检查生效；曾试过 watchdog 线程
                        # 方案，但 http.client 的 readline 是 C 层循环（持 GIL），watchdog 被
                        # 饿死不可靠，已废弃。完全无数据场景由 stall 超时（socket）兜底。
                        stream_deadline = time.time() + ep.stream_max_duration if ep.stream_max_duration > 0 else None
                        try:
                            lines = itertools.chain([first_line], resp) if first_line else resp
                            for line in lines:
                                if stream_deadline is not None and time.time() > stream_deadline:
                                    yield from _timeout_abort(f"流式总时长超限({ep.stream_max_duration}s)")
                                    return
                                if is_anthropic:
                                    if not line.strip() or not line.startswith(b"data: "):
                                        continue
                                    if line.startswith(b"data: [DONE]"):
                                        continue
                                    try:
                                        chunk = json.loads(line[6:].decode("utf-8"))
                                        ctype = chunk.get("type")
                                        if ctype == "content_block_delta":
                                            text = chunk.get("delta", {}).get("text", "")
                                            final_completion_text += text
                                            if text:
                                                o_chunk = {
                                                    "id": stream_id,
                                                    "object": "chat.completion.chunk",
                                                    "created": int(time.time()),
                                                    "model": ep.model,
                                                    "choices": [{"index": 0, "delta": {"content": text}}]
                                                }
                                                yield b"data: " + json.dumps(o_chunk).encode("utf-8") + b"\n\n"
                                        elif ctype == "message_stop":
                                            usage_chunk = {
                                                "id": stream_id,
                                                "object": "chat.completion.chunk",
                                                "created": int(time.time()),
                                                "model": ep.model,
                                                "choices": [],
                                                "usage": {
                                                    "prompt_tokens": final_prompt_tokens,
                                                    "completion_tokens": final_completion_tokens,
                                                    "total_tokens": final_total_tokens
                                                }
                                            }
                                            yield b"data: " + json.dumps(usage_chunk).encode("utf-8") + b"\n\n"
                                            yield b"data: [DONE]\n\n"
                                        elif ctype == "message_delta" and "usage" in chunk:
                                            u = chunk["usage"]
                                            final_completion_tokens += u.get("output_tokens", 0)
                                            final_total_tokens += u.get("output_tokens", 0)
                                            has_usage = True
                                        elif ctype == "message_start" and "message" in chunk and "usage" in chunk["message"]:
                                            u = chunk["message"]["usage"]
                                            prompt_t = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                                            final_prompt_tokens += prompt_t
                                            final_total_tokens += prompt_t
                                            final_cached_tokens += u.get("cache_read_input_tokens", 0)
                                            has_usage = True
                                    except Exception:
                                        pass
                                else:
                                    yield line
                                    if line.strip() and line.startswith(b"data: ") and not line.startswith(b"data: [DONE]"):
                                        try:
                                            chunk = json.loads(line[6:].decode("utf-8"))
                                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                                delta = chunk["choices"][0].get("delta", {})
                                                if "content" in delta:
                                                    final_completion_text += delta.get("content", "")
                                            if "usage" in chunk and chunk["usage"]:
                                                u = chunk["usage"]
                                                final_prompt_tokens = u.get("prompt_tokens", 0)
                                                final_completion_tokens = u.get("completion_tokens", 0)
                                                final_total_tokens = u.get("total_tokens", 0)
                                                if "prompt_tokens_details" in u and isinstance(u["prompt_tokens_details"], dict):
                                                    final_cached_tokens = u["prompt_tokens_details"].get("cached_tokens", 0)
                                                has_usage = True
                                        except Exception:
                                            pass
                        except socket.timeout:
                            yield from _timeout_abort(f"流式响应停滞({stall_timeout}s)")
                            return
                        except Exception:
                            pass
                        finally:
                            if has_usage and log_usage and not ep.name.startswith("test_"):
                                token_tracker.add_usage(ep.name, ep.model, final_prompt_tokens, final_completion_tokens, final_total_tokens, final_cached_tokens)
                                chat_logger.add_log(ep.name, ep.model, prompt_text_to_log, final_completion_text, final_total_tokens, int((time.time() - req_t0) * 1000))
                                ep._today_used += final_total_tokens
                            resp.close()
                    return stream_generator(), ""
                else:
                    body = json.loads(resp.read().decode("utf-8"))
                    if is_anthropic:
                        reply = ""
                        for c in body.get("content", []):
                            if c.get("type") == "text": reply += c.get("text", "")
                        u = body.get("usage", {})
                        prompt_t = 0; cached = 0; tot = 0
                        if u:
                            prompt_t = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                            tot = prompt_t + u.get("output_tokens", 0)
                            cached = u.get("cache_read_input_tokens", 0)
                            if log_usage and not ep.name.startswith("test_"):
                                token_tracker.add_usage(ep.name, ep.model, prompt_t, u.get("output_tokens", 0), tot, cached)
                                chat_logger.add_log(ep.name, ep.model, prompt_text_to_log, reply.strip(), tot, int((time.time() - req_t0) * 1000))
                                ep._today_used += tot
                        o_body = {
                            "id": f"chatcmpl-{int(time.time())}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": ep.model,
                            "choices": [{
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": reply.strip()
                                },
                                "finish_reason": "stop"
                            }],
                            "usage": {
                                "prompt_tokens": prompt_t,
                                "completion_tokens": u.get("output_tokens", 0) if u else 0,
                                "total_tokens": tot,
                                "prompt_tokens_details": {"cached_tokens": cached} if cached else {}
                            }
                        }
                        # 假成功检测（仅端点启用时）
                        if ep.check_fake_success:
                            _reply_text = reply.strip()
                            if _reply_text and any(p in _reply_text for p in FAKE_SUCCESS_PATTERNS):
                                sys_log(f"端点 '{ep.name}' 假成功（内容匹配拒绝模式）", "WARNING")
                                return None, "fake-success: 内容匹配拒绝模式"
                        return o_body, ""
                    else:
                        u = body.get("usage", {})
                        content = body["choices"][0]["message"].get("content", "")
                        reasoning = body["choices"][0]["message"].get("reasoning_content", "")
                        if u:
                            tot = u.get("total_tokens", 0)
                            cached = 0
                            if "prompt_tokens_details" in u and isinstance(u["prompt_tokens_details"], dict):
                                cached = u["prompt_tokens_details"].get("cached_tokens", 0)
                            if log_usage and not ep.name.startswith("test_"):
                                token_tracker.add_usage(ep.name, ep.model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), tot, cached)
                                log_text = content.strip() or reasoning.strip() or ""
                                chat_logger.add_log(ep.name, ep.model, prompt_text_to_log, log_text, tot, int((time.time() - req_t0) * 1000))
                                ep._today_used += tot
                        # 假成功检测（仅端点启用时）
                        if ep.check_fake_success:
                            _content_text = (content or reasoning or "").strip()
                            if _content_text and any(p in _content_text for p in FAKE_SUCCESS_PATTERNS):
                                sys_log(f"端点 '{ep.name}' 假成功（内容匹配拒绝模式）", "WARNING")
                                return None, "fake-success: 内容匹配拒绝模式"
                        return body, ""
                    
                    
            except urllib.error.HTTPError as e:
                err_body = ""
                try: err_body = e.read().decode("utf-8", errors="ignore")[:200]
                except Exception: pass
                msg = f"HTTP {e.code}: {err_body}"
                if (e.code == 400 and not force_no_retry
                        and any(k in payload for k in ("temperature", "top_p"))
                        and ("temperature" in err_body or "top_p" in err_body)):
                    cleaned = {k: v for k, v in payload.items() if k not in ("temperature", "top_p")}
                    sys_log(f"\u7aef\u70b9 '{ep.name}' \u4e0d\u652f\u6301 temperature/top_p\uff0c\u5df2\u81ea\u52a8\u79fb\u9664\u540e\u91cd\u8bd5", "WARNING")
                    return self._try_endpoint(ep, cleaned, timeout, log_usage=log_usage, force_no_retry=True)
                if e.code == 429: return None, msg + " (429 rate-limited)"
                if e.code in (401, 403): return None, msg + " (auth error)"
                if e.code >= 500:
                    if attempt < retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    return None, msg
                return None, msg
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                msg = f"连接/超时错误: {e}"
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return None, msg
            except Exception as e:
                return None, f"未知错误: {e}"
        return None, "重试次数用尽"

    def fetch_models(self, base_url, api_key, timeout=10, use_proxy=True, protocol="openai"):
        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, method="GET")
        safe_api_key = api_key.encode('ascii', 'ignore').decode('ascii').strip()
        req.add_header("Authorization", f"Bearer {safe_api_key}")
        req.add_header("User-Agent", "OpenAI/Python 2.33.0")
            
        try:
            if not use_proxy:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urllib.request.urlopen(req, timeout=timeout)
                
            with resp:
                data = json.loads(resp.read().decode("utf-8"))
                raw = data.get("data", [])
                models = []
                for m in raw:
                    mid = m.get("id", "")
                    if not mid: continue
                    info = {"id": mid}
                    if "pricing" in m: info["pricing"] = m["pricing"]
                    if "description" in m: info["description"] = m["description"][:120]
                    info["modality"] = "unknown"
                    info["modality_source"] = "none"
                    models.append(info)
                models.sort(key=lambda x: x["id"])
                return models
        except Exception as e:
            raise e

    def test_vision(self, base_url, api_key, model, timeout=15, use_proxy=True, protocol="openai"):
        ep = Endpoint(name="test_vision", base_url=base_url, api_key=api_key, model=model, max_retries=0, use_proxy=use_proxy, protocol=protocol)
        tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "describe this image in 3 words"}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_png}"}}]}],
            "max_tokens": 10,
        }
        t0 = time.time()
        reply, err = self._try_endpoint(ep, payload, timeout)
        latency = int((time.time() - t0) * 1000)
        
        if reply is not None:
            if isinstance(reply, dict):
                reply_text = reply.get("choices", [{}])[0].get("message", {}).get("content", "").lower()
            else:
                reply_text = str(reply).lower()
            unsupported_keywords = ["cannot see", "can't see", "not able to see", "unable to see", "text-based", "language model", "无法查看", "无法读取", "无法看到", "不具备", "不支持", "抱歉", "sorry", "没有上传", "没上传"]
            if any(k in reply_text for k in unsupported_keywords):
                return {"ok": True, "supports_vision": False, "latency_ms": latency, "reply": reply, "error": f"模型疑似无法读图: {reply_text[:50]}..."}
            return {"ok": True, "supports_vision": True, "latency_ms": latency, "reply": reply, "error": ""}
        else:
            unsupported = "image" in err.lower() or "vision" in err.lower() or "content" in err.lower() or "400" in err
            return {"ok": not unsupported, "supports_vision": not unsupported, "latency_ms": latency, "reply": "", "error": err}

    def test_model_latency(self, base_url, api_key, model, timeout=15, use_proxy=True, protocol="openai"):
        ep = Endpoint(name="test_latency", base_url=base_url, api_key=api_key, model=model, max_retries=0, use_proxy=use_proxy, protocol=protocol)
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
        t0 = time.time()
        reply, err = self._try_endpoint(ep, payload, timeout)
        latency = int((time.time() - t0) * 1000)
        
        if reply is not None:
            status = "ok" if latency <= LATENCY_OK_MAX else ("slow" if latency <= LATENCY_SLOW_MAX else "bad")
            return {"ok": True, "status": status, "latency_ms": latency, "reply": reply, "error": ""}
        else:
            return {"ok": False, "status": "bad", "latency_ms": latency, "reply": "", "error": err}

CONFIG_FILE = "api_config.json"

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f).get("api_endpoints", [])
    except Exception:
        return []

def save_config(endpoints_data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"api_endpoints": endpoints_data}, f, ensure_ascii=False, indent=2)

def ensure_config():
    if not os.path.exists(CONFIG_FILE): save_config([])

ensure_config()

pool = APIPool()
for ep_data in load_config():
    if "in_pool" not in ep_data: ep_data["in_pool"] = True
    pool.add_endpoint(ep_data)


def api_handler(method, path, body):
    parsed = urlparse(path)
    cp = parsed.path

    # ================= 代理接口 =================
    if method == "POST" and cp in ("/v1/chat/completions", "/chat/completions"):
        messages = body.get("messages", [])
        is_stream = body.get("stream", False)
        
        extra_payload = {k: v for k, v in body.items() if k not in ("messages", "model")}
        extra_payload.pop("extra_body", None)
        extra_payload.pop("response_format", None)
        
        try:
            result = pool.chat(messages, extra_payload=extra_payload)
            if is_stream: return 200, result, True 
            
            # result is the full upstream response body (preserves reasoning_content etc.)
            response = dict(result)  # shallow copy
            # 保留真实模型名称，让 Hermes 能识别 DeepSeek 并应用对应 provider 逻辑
            # 原来的 api-pool-aggregated 会导致 Hermes 用通用 OpenAI 客户端，丢失 reasoning_content 处理
            # response["model"] = "api-pool-aggregated"
            return 200, response, False
            
        except AllEndpointsFailed as e:
            return 500, {"error": {"message": f"所有端点均已失效: {e.errors}", "type": "server_error"}}, False
        except Exception as e:
            return 500, {"error": {"message": str(e), "type": "server_error"}}, False

    if method == "GET" and cp == "/api/logs":
        qs = dict(q.split("=") for q in parsed.query.split("&") if "=" in q) if parsed.query else {}
        last_id = int(qs.get("since", 0))
        return 200, sys_logger.get_logs_since(last_id), False

    if method == "DELETE" and cp == "/api/logs":
        sys_logger.clear_logs()
        return 200, {"ok": True}, False

    if method == "GET" and cp == "/api/chat-logs":
        qs = dict(q.split("=") for q in parsed.query.split("&") if "=" in q) if parsed.query else {}
        limit = int(qs.get("limit", 50))
        offset = int(qs.get("offset", 0))
        return 200, chat_logger.get_logs(limit=limit, offset=offset), False

    if method == "DELETE" and cp == "/api/chat-logs":
        chat_logger.clear_logs()
        return 200, {"ok": True}, False

    if method == "GET" and cp == "/api/token-stats":
        qs = dict(q.split("=") for q in parsed.query.split("&") if "=" in q) if parsed.query else {}
        ep = qs.get("endpoint", "all")
        # url decode
        ep = urllib.parse.unquote(ep)
        return 200, token_tracker.get_stats(endpoint_filter=ep), False

    if method == "DELETE" and cp == "/api/token-stats":
        token_tracker.clear_data()
        return 200, {"ok": True}, False

    if method == "GET" and cp == "/api/endpoints": return 200, pool.list_endpoints(), False
    if method == "GET" and cp == "/api/chain": return 200, pool.get_active_chain(), False
    # ================= 聚合池管理 =================
    if method == "GET" and cp == "/api/pool":
        return 200, [ep for ep in pool.list_endpoints() if ep.get("in_pool")], False
    if method == "POST" and cp.startswith("/api/pool/"):
        ep_id = unquote(cp.split("/")[-1])
        pool.set_pool(ep_id, True); _sync_to_config()
        return 200, {"ok": True}, False
    if method == "DELETE" and cp.startswith("/api/pool/"):
        ep_id = unquote(cp.split("/")[-1])
        pool.set_pool(ep_id, False); _sync_to_config()
        return 200, {"ok": True}, False
    if method == "POST" and cp.startswith("/api/switch-endpoint/"):
        ep_id = unquote(cp.split("/")[-1])
        ok = pool.switch_to_endpoint(ep_id)
        return 200, {"ok": ok}, False
    if method == "POST" and cp == "/api/endpoints":
        pool.add_endpoint(body); _sync_to_config(); return 201, {"ok": True}, False
    if method == "POST" and cp == "/api/endpoints/batch":
        items = body.get("endpoints", []); base = body.get("base", {}); added = 0; start_priority = base.get("start_priority", 1)
        for i, item in enumerate(items):
            ep = {
                "name": item.get("name", base.get("name", f"ep_{i}")), "base_url": item.get("base_url", base.get("base_url", "")),
                "api_key": item.get("api_key", base.get("api_key", "")), "model": item.get("model", ""),
                "priority": item.get("priority", start_priority + i), "timeout": item.get("timeout", base.get("timeout", 60)),
                "max_retries": item.get("max_retries", base.get("max_retries", 1)), "cooldown_minutes": item.get("cooldown_minutes", base.get("cooldown_minutes", 5)),
                "daily_limit": item.get("daily_limit", base.get("daily_limit", 0)), "rpm_limit": item.get("rpm_limit", base.get("rpm_limit", 0)),
                "use_proxy": item.get("use_proxy", base.get("use_proxy", False)),
                "protocol": item.get("protocol", base.get("protocol", "openai")),
                "health_mode": item.get("health_mode", base.get("health_mode", "chat")),
                  "billing_mode": item.get("billing_mode", base.get("billing_mode", "subscription")),
                  "is_vision": item.get("is_vision", base.get("is_vision", True)),
                  "in_pool": item.get("in_pool", base.get("in_pool", False)),
                  "enabled": item.get("enabled", True),
            }
            if ep["model"]: pool.add_endpoint(ep); added += 1
        _sync_to_config(); return 201, {"ok": True, "added": added}, False
    if method == "PUT" and cp.startswith("/api/endpoints/") and not cp.endswith("/toggle"):
        ep_id = unquote(cp.split("/")[-1])
        new_name = body.get("name")
        old_ep = next((e for e in pool.list_endpoints() if e["id"] == ep_id), None)
        if old_ep and new_name and new_name != old_ep["name"]:
            token_tracker.rename_endpoint(old_ep["name"], new_name)
        pool.update_endpoint(ep_id, body); _sync_to_config(); return 200, {"ok": True}, False
    if method == "DELETE" and cp.startswith("/api/endpoints/"):
        ep_id = unquote(cp.split("/")[-1]); pool.remove_endpoint(ep_id); _sync_to_config(); return 200, {"ok": True}, False
    if method == "POST" and cp.endswith("/toggle"):
        ep_id = unquote(cp.split("/")[3])
        for ep in pool.list_endpoints():
            if ep["id"] == ep_id: pool.set_enabled(ep_id, not ep["enabled"]); break
        _sync_to_config(); return 200, {"ok": True}, False
    if method == "POST" and cp.endswith("/clear-error"):
        ep_id = unquote(cp.split("/")[3])
        pool.clear_error(ep_id)
        return 200, {"ok": True}, False
    if method == "POST" and cp == "/api/health-check": return 200, {"ok": True, "results": pool.check_all_health()}, False
    if method == "POST" and cp == "/api/fetch-models":
        base_url = body.get("base_url", ""); api_key = body.get("api_key", "")
        if not base_url or not api_key: return 400, {"error": "需要 base_url 和 api_key"}, False
        try:
            models = pool.fetch_models(base_url, api_key, use_proxy=body.get("use_proxy", True), protocol=body.get("protocol", "openai"))
            return 200, {"ok": True, "models": models, "count": len(models)}, False
        except urllib.error.HTTPError as e:
            err_body = ""
            try: err_body = e.read().decode("utf-8", errors="ignore")[:200]
            except Exception: pass
            return 200, {"ok": False, "error": f"HTTP {e.code}: {err_body}"}, False
        except Exception as e: return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/test-model": return 200, pool.test_model_latency(body.get("base_url", ""), body.get("api_key", ""), body.get("model", ""), timeout=body.get("timeout", 60), use_proxy=body.get("use_proxy", True), protocol=body.get("protocol", "openai")), False
    if method == "POST" and cp == "/api/test-vision": return 200, pool.test_vision(body.get("base_url", ""), body.get("api_key", ""), body.get("model", ""), timeout=body.get("timeout", 60), use_proxy=body.get("use_proxy", True), protocol=body.get("protocol", "openai")), False
    if method == "POST" and cp == "/api/test":
        ep_id = body.get("id", ""); test_msg = body.get("message", "你好"); target_ep = None
        for ep in pool.list_endpoints():
            if ep["id"] == ep_id: target_ep = ep; break
        if not target_ep: return 404, {"error": "端点不存在"}, False
        test_pool = APIPool()
        test_pool.add_endpoint({"name": target_ep["name"], "base_url": target_ep["base_url"], "api_key": target_ep["api_key_full"], "model": target_ep["model"], "priority": 1, "timeout": target_ep["timeout"], "max_retries": target_ep["max_retries"], "enabled": True, "in_pool": True, "use_proxy": target_ep.get("use_proxy", True), "protocol": target_ep.get("protocol", "openai"), "is_vision": target_ep.get("is_vision", True)})
        
        img = body.get("image")
        if img:
            test_msg = [{"type": "text", "text": test_msg}, {"type": "image_url", "image_url": {"url": img}}]
            
        try:
            res_dict, served_ep = test_pool.chat([{"role": "user", "content": test_msg}], return_endpoint=True)
            res_str = res_dict.get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(res_dict, dict) else res_dict
            return 200, {"ok": True, "result": res_str, "served_by": f"{served_ep.name} ({served_ep.model})"}, False
        except Exception as e: return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/test-pool":
        test_msg = body.get("message", "你好")
        img = body.get("image")
        if img:
            test_msg = [{"type": "text", "text": test_msg}, {"type": "image_url", "image_url": {"url": img}}]
        try:
            res_dict, served_ep = pool.chat([{"role": "user", "content": test_msg}], return_endpoint=True)
            res_str = res_dict.get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(res_dict, dict) else res_dict
            return 200, {"ok": True, "result": res_str, "served_by": f"{served_ep.name} ({served_ep.model})"}, False
        except AllEndpointsFailed as e: return 200, {"ok": False, "errors": e.errors}, False
        except Exception as e: return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/reset": pool.reset(); return 200, {"ok": True}, False
    if method == "POST" and cp == "/api/reset-priority": pool.reset_to_priority_mode(); return 200, {"ok": True}, False

    return 404, {"error": "Not found"}, False

def _sync_to_config():
    save_config([{"id": ep.get("id"), "name": ep["name"], "base_url": ep["base_url"], "api_key": ep.get("api_key_full", ep.get("api_key", "")), "model": ep["model"], "priority": ep["priority"], "timeout": ep["timeout"], "max_retries": ep["max_retries"], "enabled": ep["enabled"], "cooldown_minutes": ep["cooldown_minutes"], "daily_limit": ep.get("daily_limit", 0), "rpm_limit": ep.get("rpm_limit", 0), "use_proxy": ep.get("use_proxy", True), "protocol": ep.get("protocol", "openai"), "health_mode": ep.get("health_mode", "chat"), "billing_mode": ep.get("billing_mode", "subscription"), "is_vision": ep.get("is_vision", True),
            "in_pool": ep.get("in_pool", False), "check_fake_success": ep.get("check_fake_success", False), "tool_call_id_prefix": ep.get("tool_call_id_prefix", ""), "deferrable": ep.get("deferrable", True), "max_context_k": ep.get("max_context_k", 0)} for ep in pool.list_endpoints()])


GUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
GUI_FILE = os.path.join(GUI_DIR, "index.html")
_gui_html_cache = None
_gui_html_mtime = 0


def _load_gui_html():
    """读取前端页面，mtime 变化时自动重载（前端热更新，无需重启服务）。"""
    global _gui_html_cache, _gui_html_mtime
    try:
        mtime = os.path.getmtime(GUI_FILE)
        if mtime != _gui_html_mtime:
            with open(GUI_FILE, "r", encoding="utf-8") as f:
                _gui_html_cache = f.read()
            _gui_html_mtime = mtime
        return _gui_html_cache
    except Exception:
        return None



class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, code, data):
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass

    def _send_html(self, html):
        try:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                return json.loads(self.rfile.read(length))
            return {}
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = _load_gui_html()
            if html is None:
                self._send_html("<!DOCTYPE html><html><body style=\"background:#111;color:#eee;font-family:sans-serif;padding:40px\"><h3>⚠️ API Pool UI 加载失败</h3><p>static/index.html 不存在或不可读，请检查服务目录。</p></body></html>")
            else:
                self._send_html(html)
        elif self.path.startswith("/api/export-stats"):
            csv_data = token_tracker.export_csv()
            try:
                body = csv_data.encode("utf-8-sig")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=token_stats.csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except ConnectionError:
                pass
        elif self.path.startswith("/api/"):
            res = api_handler("GET", self.path, {})
            if len(res) == 3 and res[2] is True:
                code, stream_gen = res[0], res[1]
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    for chunk in stream_gen:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except ConnectionError:
                    pass
            else:
                self._send_json(res[0], res[1])
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        body = self._read_body()
        res = api_handler("POST", self.path, body)
        
        if len(res) == 3 and res[2] is True:
            code, stream_gen = res[0], res[1]
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                
                for chunk in stream_gen:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except ConnectionError:
                pass
        else:
            self._send_json(res[0], res[1])

    def do_PUT(self):
        body = self._read_body()
        res = api_handler("PUT", self.path, body)
        self._send_json(res[0], res[1])

    def do_DELETE(self):
        res = api_handler("DELETE", self.path, {})
        self._send_json(res[0], res[1])


def main():
    import sys
    if sys.stdout.encoding.lower() != 'utf-8':
        try: sys.stdout.reconfigure(encoding='utf-8')
        except: pass
    port = 5100
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"\n  ⚡ API Pool 管理面板已启动")
    print(f"  🌐 管理面板访问: http://localhost:{port}")
    print(f"  🔗 客户端 Base URL: http://localhost:{port}/v1")
    print(f"  📋 已加载 {len(pool._endpoints)} 个端点")
    print(f"  🩺 健康检测: 失败时按优先级逐个探活候选端点")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()
