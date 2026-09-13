"""
API Pool — 聚合 API 自动切换模块（GUI 版）

启动: API_POOL_PORT=5200 API_POOL_INSTANCE="API Pool 2.0" python api_pool_server.py
访问: http://localhost:5200

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
import signal
import threading
import sqlite3
import socket
import hashlib
import base64
import struct
import zlib
import itertools
import re
import urllib.request
import urllib.error
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import queue
from datetime import datetime, timedelta
from collections import deque
import copy as _copy
import types as _types

# 敏感字过滤私有配置文件（仅 API Pool 进程读取，Hermes 不可见）
CONTENT_FILTER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "content_filter.json")

LATENCY_OK_MAX = 2000     
LATENCY_SLOW_MAX = 5000   
# 假成功检测：上游返回 200 OK 但内容含拒绝/错误信息
FAKE_SUCCESS_PATTERNS = ["无法给到相关内容"]

# 流事务中止原因中「答案被截断」的那一类：已向下游输出内容后再中止，回答一定不完整。
# 这些原因要保留可见的 error finish，让 Hermes 走续写路径（`_timeout_abort`）。
_TRUNCATED_STREAM_REASONS = ("流式总时长超限", "流式无有效业务增量停滞", "流式无新数据停滞", "上游流式错误")

# ── 出站 User-Agent 透传（2026-09-05）──
# 客户端原始 UA 经线程局部存储送到出站请求构造点。ThreadingHTTPServer 每请求独占
# 线程，流式生成器与图片翻译都在同一线程内消费，因此该作用域覆盖整个请求生命周期
# （含流式内部重试与 temperature/top_p 清洗重试）。
# 只有代理路径写入真实客户端头；探活/管理页测试/拉模型接口无客户端上下文，改用客户端基线
# （最近一次真实入站头），使其与真实流量携带同一客户端特征。端点自定义头
# （default_headers/extra_headers）在此之后 add_header 覆盖，优先级为 端点自定义 > 客户端 > 默认库标识。
# 池自身发起的出站（探活/健康检测/测试/拉模型/管理接口触发的测试请求）没有客户端上下文：
# 身份必须来自**确定来源**（端点 client_profile → 池级 probe_client_profile → 最近一次真实客户端
# 指纹）；三者都没有时**跳过本次出站**，绝不编造身份（2026-09-12：编造 UA 曾被上游判
# unauthorized client（401），并连带把端点冷却、打掉主组路由）。
POOL_IDENTITY_SKIP_REASON = "无可用客户端身份，已跳过本次出站"
_POOL_INITIATED_CTX = threading.local()   # 池自身发起的请求标记（伤害隔离：失败只写观测态）
# DeepSeek 严格校验 400 的同端点重试退避（毫秒基准，实际 sleep 再乘 1.0–1.4 抖动）：
# 该类 400 是上游实例级随机故障，隔开一点再打命中率更高；测试可置 0 免等待。
_STRICT400_BACKOFF_MS = (300, 800)


class PoolIdentityUnavailable(RuntimeError):
    """池自身出站解析不到可用客户端身份 → 调用方必须跳过本次出站（不改任何端点状态）。"""
_client_ctx = threading.local()
# 客户端基线：进程内最近一次真实入站头快照，供无客户端上下文的出站路径复用。
# ponytail: 单份全局，多客户端混用时代表最近一个客户端；需要区分时改 per-endpoint 基线。
_client_baseline: dict = {}
_client_baseline_lock = threading.Lock()

# ── 客户端伪装 Client Profile ──
# 让选中端点的出站请求在头层面与真实客户端一致：profile.headers 为唯一来源。
# profile 存配置文件顶层 client_profiles 键，均可编辑（hermes 亦为配置条目，版本号随客户端升级更新）。
# hermes 黄金样本：/opt/hermes/.venv openai SDK 2.24.0 实测出站头集合。
# profile 不允许覆盖的保留头（由 pool 按端点协议管理）
_PROFILE_RESERVED_HEADERS = frozenset({
    "host", "content-length", "authorization", "x-api-key", "anthropic-version", "x-goog-api-key",
})
# 透传时跳过的头：连接级/端到端头 + pool 托管头（host/content-length 由 urllib 重算，
# authorization/x-api-key/anthropic-version 由 pool 按端点协议写入）
_PASSTHROUGH_SKIP_HEADERS = _PROFILE_RESERVED_HEADERS | frozenset({
    "connection", "transfer-encoding", "te", "trailer", "upgrade",
    "proxy-authorization", "proxy-connection", "keep-alive",
})
# 解压链（_DecodedResponse）支持的压缩编码；其余 token（br/zstd）透传会让下游收到无法解码的字节
_SUPPORTED_ACCEPT_ENCODINGS = frozenset({"gzip", "deflate", "identity"})


def set_client_headers(headers):
    """代理路径写入真实入站头，并更新进程级基线供探活/测试/拉模型复用。"""
    hdrs = {k: v for k, v in dict(headers or {}).items() if isinstance(k, str) and isinstance(v, str)}
    _client_ctx.headers = hdrs
    with _client_baseline_lock:
        _client_baseline.clear()
        _client_baseline.update(hdrs)


def clear_client_headers():
    _client_ctx.headers = {}


def _current_client_headers():
    """当前线程的客户端头；无（探活/测试/管理线程）→ 客户端基线；都无 → {}。"""
    hdrs = getattr(_client_ctx, "headers", None)
    if hdrs:
        return hdrs
    with _client_baseline_lock:
        return dict(_client_baseline)



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
    # flush=True: systemd 下 stdout 是块缓冲（8KB），不加 flush 日志会积攒 6-12 分钟
    # 才落盘 journal，排障时严重误导（2026-08-15 实测：23:53 的日志 23:52:53 才批量落盘）
    print(f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)


class ContentFilterError(Exception):
    """敏感字过滤器不可用。请求应被拒绝，禁止降级放行原始请求。"""


class ContentFilter:
    """入口敏感字过滤器。

    - 词典：私有 content_filter.json，key=替换词映射；值取空串表示删除。
    - 匹配：正则多分支，单次扫描，长词优先（按词长降序排列）。
    - 替换结果不参与二次匹配（single-pass，A->B 后不再用 B 匹配）。
    - 结构保护：messages 中非字符串 content、图像块等字段不触碰。
    - 快速路径：未启用或词典为空时直接返回原对象（不深拷贝）。

    扫描范围由配置 targets 控制（精准最小化，实测命中位置默认开启）：
      messages.content              messages[].content 字符串              [默认开]
      messages.text_blocks          messages[].content[].text 多模态文本块  [默认开]
      messages.reasoning            reasoning_content / reasoning_text      [默认关]
      messages.name                 messages[].name                        [默认关]
      messages.tool_call_arguments  tool_calls[].function.arguments         [默认关]
      tools.descriptions            tools[].function..*.description         [默认关]
      all_strings                   递归扫描 payload 全部字符串值(不碰 key)  [默认关]
    """

    # 实测命中位置：敏感词由 Hermes untrusted 包装提示语产生，落在 messages content
    DEFAULT_TARGETS = ("messages.content", "messages.text_blocks")
    KNOWN_TARGETS = (
        "messages.content",
        "messages.text_blocks",
        "messages.reasoning",
        "messages.name",
        "messages.tool_call_arguments",
        "tools.descriptions",
        "all_strings",
    )

    def __init__(self, file_path=CONTENT_FILTER_FILE, logger=None):
        self.file_path = file_path
        self._logger = logger or sys_log
        self._lock = threading.Lock()
        self._enabled = False
        self._dictionary_version = ""
        self._pairs = []  # [(pattern, replacement)]
        self._pattern = None
        self._replacements = []
        self._targets = set(self.DEFAULT_TARGETS)
        self._raw = {}
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                self._enabled = False
                self._pairs = []
                self._pattern = None
                self._replacements = []
                self._raw = {}
                return False
            except (OSError, ValueError) as e:
                self._logger(
                    f"敏感字过滤词典加载失败: {type(e).__name__}: {e}",
                    "ERROR",
                )
                self._enabled = False
                self._pairs = []
                self._pattern = None
                self._replacements = []
                self._raw = {}
                return False
            try:
                section = data.get("content_filter") if isinstance(data, dict) else None
                if not isinstance(section, dict):
                    raise TypeError("content_filter 配置段缺失或类型错误")
                enabled = bool(section.get("enabled", False))
                raw_rules = section.get("rules")
                raw_dict = section.get("dictionary", {})
                if raw_rules is not None and not isinstance(raw_rules, list):
                    raise TypeError("rules 必须是数组")
                if not isinstance(raw_dict, dict):
                    raise TypeError("dictionary 必须是对象")
                version = str(section.get("dictionary_version", ""))
                raw_targets = section.get("targets")
                if raw_targets is None:
                    targets = set(self.DEFAULT_TARGETS)
                elif isinstance(raw_targets, list):
                    targets = {str(t) for t in raw_targets if isinstance(t, str)}
                    unknown = targets - set(self.KNOWN_TARGETS)
                    if unknown:
                        self._logger(
                            f"敏感字过滤配置含未知 targets，已忽略: {sorted(unknown)}",
                            "WARN",
                        )
                        targets -= unknown
                else:
                    raise TypeError("targets 必须是数组")
            except (ValueError, TypeError) as e:
                self._logger(f"敏感字过滤配置结构无效: {e}", "ERROR")
                self._enabled = False
                self._pairs = []
                self._pattern = None
                self._replacements = []
                self._raw = {}
                return False

            pairs = []
            if raw_rules is not None:
                for rule in raw_rules:
                    if not isinstance(rule, dict):
                        continue
                    kind = rule.get("type", "literal")
                    pattern = rule.get("pattern", "")
                    replacement = rule.get("replacement", "")
                    if not isinstance(pattern, str) or not pattern:
                        continue
                    if not isinstance(replacement, str):
                        replacement = str(replacement)
                    if kind == "literal":
                        pattern = re.escape(pattern)
                    elif kind != "regex":
                        raise TypeError(f"不支持的规则类型: {kind}")
                    try:
                        pairs.append((pattern, replacement))
                    except re.error as e:
                        raise ValueError(f"规则正则无效: {e}") from e
            else:
                # 兼容旧 dictionary 配置
                for word, replacement in raw_dict.items():
                    if not isinstance(word, str) or not word:
                        continue
                    if replacement is None:
                        replacement = ""
                    if not isinstance(replacement, str):
                        replacement = str(replacement)
                    pairs.append((re.escape(word), replacement))
            # 长规则优先，避免短规则先截断长规则
            pairs.sort(key=lambda pr: len(pr[0]), reverse=True)
            self._pairs = [(re.compile(pattern), replacement) for pattern, replacement in pairs]
            # 交替正则只在加载词典时编译一次；请求处理阶段直接复用。
            self._pattern = (
                re.compile("|".join(f"({pattern})" for pattern, _ in pairs))
                if pairs
                else None
            )
            self._replacements = [replacement for _, replacement in pairs]
            self._enabled = enabled
            self._dictionary_version = version
            self._targets = targets
            self._raw = data
            return True

    def _reload(self):
        self.load()

    def _match_and_replace(self, text):
        """对单个字符串执行单次扫描替换，替换结果不参与二次匹配。"""
        if not text:
            return text, 0
        pattern = self._pattern
        if pattern is None:
            return text, 0
        replacements = self._replacements
        matched_count = 0

        def _repl(m):
            nonlocal matched_count
            matched_count += 1
            for index, group in enumerate(m.groups()):
                if group is not None:
                    return replacements[index]
            return m.group(0)

        new_text = pattern.sub(_repl, text)
        return new_text, matched_count

    def filter_payload(self, payload, return_stats=False):
        """对请求 payload 执行清洗，返回 (清洗后 payload, stats)。

        - 未启用或词典为空：返回原对象（不深拷贝），stats.matched=0。
        - 失败时抛出 ContentFilterError，调用方应拒绝请求。
        """
        stats = {
            "enabled": self._enabled,
            "matched": 0,
            "duration_ms": 0.0,
            "copy_ms": 0.0,
            "scan_ms": 0.0,
            "dictionary_version": self._dictionary_version,
        }
        if not self._enabled or not self._pairs:
            if return_stats:
                return payload, stats
            return payload

        t0 = time.perf_counter()
        try:
            # 预扫：无命中直接复用原请求对象（与未启用路径一致），避免每次请求全量深拷贝
            if not self._has_match(payload):
                scan_done = time.perf_counter()
                stats["scan_ms"] = round((scan_done - t0) * 1000, 3)
                stats["duration_ms"] = stats["scan_ms"]
                if return_stats:
                    return payload, stats
                return payload
            # 深拷贝 payload，确保后续端点/日志共用清洗结果且不污染原请求
            cleaned = _copy.deepcopy(payload)
            copy_done = time.perf_counter()
            total = self._apply(cleaned)
            scan_done = time.perf_counter()
        except Exception as e:
            raise ContentFilterError(
                f"敏感字过滤执行失败: {type(e).__name__}: {e}"
            ) from e
        stats["matched"] = total
        stats["copy_ms"] = round((copy_done - t0) * 1000, 3)
        stats["scan_ms"] = round((scan_done - copy_done) * 1000, 3)
        stats["duration_ms"] = round((scan_done - t0) * 1000, 3)
        if return_stats:
            return cleaned, stats
        return cleaned

    def _apply(self, payload):
        """就地修改 payload 结构，返回命中次数。范围由 self._targets 控制。"""
        if not isinstance(payload, dict):
            return 0
        t = self._targets
        # 全量递归扫描：从根开始扫所有字符串值（不碰 key），一次覆盖全部位置
        if "all_strings" in t:
            return self._walk_strings(payload)
        total = 0
        messages = payload.get("messages")
        if isinstance(messages, list) and (
            "messages.content" in t
            or "messages.text_blocks" in t
            or "messages.reasoning" in t
            or "messages.name" in t
            or "messages.tool_call_arguments" in t
        ):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if "messages.content" in t and isinstance(content, str):
                    new_text, n = self._match_and_replace(content)
                    if n:
                        msg["content"] = new_text
                        total += n
                elif "messages.text_blocks" in t and isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            tv = part.get("text")
                            if isinstance(tv, str):
                                new_text, n = self._match_and_replace(tv)
                                if n:
                                    part["text"] = new_text
                                    total += n
                if "messages.reasoning" in t:
                    for rfield in ("reasoning_content", "reasoning_text"):
                        rv = msg.get(rfield)
                        if isinstance(rv, str):
                            new_rv, n = self._match_and_replace(rv)
                            if n:
                                msg[rfield] = new_rv
                                total += n
                if "messages.name" in t:
                    mname = msg.get("name")
                    if isinstance(mname, str):
                        new_mname, n = self._match_and_replace(mname)
                        if n:
                            msg["name"] = new_mname
                            total += n
                if "messages.tool_call_arguments" in t:
                    tool_calls = msg.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if not isinstance(tc, dict):
                                continue
                            fn = tc.get("function")
                            if not isinstance(fn, dict):
                                continue
                            args = fn.get("arguments")
                            if isinstance(args, str):
                                new_args, n = self._replace_json_arguments(args)
                                if n:
                                    fn["arguments"] = new_args
                                    total += n
                            elif isinstance(args, dict):
                                total += self._walk_strings(args)
        if "tools.descriptions" in t:
            tools = payload.get("tools")
            if isinstance(tools, list):
                for tool in tools:
                    if not isinstance(tool, dict):
                        continue
                    fn = tool.get("function")
                    if not isinstance(fn, dict):
                        continue
                    desc = fn.get("description")
                    if isinstance(desc, str):
                        new_text, n = self._match_and_replace(desc)
                        if n:
                            fn["description"] = new_text
                            total += n
                    params = fn.get("parameters")
                    if isinstance(params, dict):
                        total += self._walk_descriptions(params)
        return total

    def _has_match(self, payload):
        """与 _apply 同路径的只读预扫：任一目标字符串命中即返回 True（不复制、不修改）。

        遍历范围必须与 _apply/_walk_* 保持一致，否则预扫假阴性会导致漏过滤。
        """
        if not self._enabled or not self._pairs:
            return False
        pattern = self._pattern
        if pattern is None or not isinstance(payload, dict):
            return False
        t = self._targets
        if "all_strings" in t:
            return self._scan_strings(payload)
        messages = payload.get("messages")
        if isinstance(messages, list) and (
            "messages.content" in t
            or "messages.text_blocks" in t
            or "messages.reasoning" in t
            or "messages.name" in t
            or "messages.tool_call_arguments" in t
        ):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if "messages.content" in t and isinstance(content, str):
                    if pattern.search(content):
                        return True
                elif "messages.text_blocks" in t and isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            tv = part.get("text")
                            if isinstance(tv, str) and pattern.search(tv):
                                return True
                if "messages.reasoning" in t:
                    for rfield in ("reasoning_content", "reasoning_text"):
                        rv = msg.get(rfield)
                        if isinstance(rv, str) and pattern.search(rv):
                            return True
                if "messages.name" in t:
                    mname = msg.get("name")
                    if isinstance(mname, str) and pattern.search(mname):
                        return True
                if "messages.tool_call_arguments" in t:
                    tool_calls = msg.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if not isinstance(tc, dict):
                                continue
                            fn = tc.get("function")
                            if not isinstance(fn, dict):
                                continue
                            args = fn.get("arguments")
                            if (
                                isinstance(args, str) and self._scan_arguments(args)
                            ) or (
                                isinstance(args, dict) and self._scan_strings(args)
                            ):
                                return True
        if "tools.descriptions" in t:
            tools = payload.get("tools")
            if isinstance(tools, list):
                for tool in tools:
                    if not isinstance(tool, dict):
                        continue
                    fn = tool.get("function")
                    if not isinstance(fn, dict):
                        continue
                    desc = fn.get("description")
                    if isinstance(desc, str) and pattern.search(desc):
                        return True
                    params = fn.get("parameters")
                    if isinstance(params, dict) and self._scan_descriptions(params):
                        return True
        return False

    def _scan_strings(self, node):
        """_walk_strings 的只读镜像：任一字符串值命中即返回 True。"""
        if isinstance(node, dict):
            for value in node.values():
                if (
                    isinstance(value, str) and self._pattern.search(value)
                ) or (
                    isinstance(value, (dict, list)) and self._scan_strings(value)
                ):
                    return True
        elif isinstance(node, list):
            for item in node:
                if (
                    isinstance(item, str) and self._pattern.search(item)
                ) or (
                    isinstance(item, (dict, list)) and self._scan_strings(item)
                ):
                    return True
        return False

    def _scan_descriptions(self, node):
        """_walk_descriptions 的只读镜像：description 字符串命中即返回 True。"""
        if isinstance(node, dict):
            desc = node.get("description")
            if isinstance(desc, str) and self._pattern.search(desc):
                return True
            for value in node.values():
                if isinstance(value, (dict, list)) and self._scan_descriptions(value):
                    return True
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)) and self._scan_descriptions(item):
                    return True
        return False

    def _scan_arguments(self, args_str):
        """_replace_json_arguments 的只读镜像：JSON 解析成功走对象扫描，失败走原串搜索。"""
        try:
            obj = json.loads(args_str)
        except (ValueError, TypeError):
            return bool(self._pattern.search(args_str))
        if isinstance(obj, (dict, list)):
            return self._scan_strings(obj)
        return False

    def _walk_descriptions(self, node):
        """遍历 parameters 中的 description 字符串字段。"""
        count = 0
        if isinstance(node, dict):
            desc = node.get("description")
            if isinstance(desc, str):
                new_text, n = self._match_and_replace(desc)
                if n:
                    node["description"] = new_text
                    count += n
            for value in node.values():
                if isinstance(value, (dict, list)):
                    count += self._walk_descriptions(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    count += self._walk_descriptions(item)
        return count

    def _walk_strings(self, node):
        """递归替换 dict/list 中所有字符串值（不碰 key），用于工具参数对象。"""
        count = 0
        if isinstance(node, dict):
            for key in list(node.keys()):
                value = node[key]
                if isinstance(value, str):
                    new_value, n = self._match_and_replace(value)
                    if n:
                        node[key] = new_value
                        count += n
                elif isinstance(value, (dict, list)):
                    count += self._walk_strings(value)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                if isinstance(item, str):
                    new_item, n = self._match_and_replace(item)
                    if n:
                        node[i] = new_item
                        count += n
                elif isinstance(item, (dict, list)):
                    count += self._walk_strings(item)
        return count

    def _replace_json_arguments(self, args_str):
        """工具调用参数：优先解析 JSON 后递归替换值，解析失败则直接字符串替换。"""
        try:
            obj = json.loads(args_str)
        except (ValueError, TypeError):
            return self._match_and_replace(args_str)
        if isinstance(obj, (dict, list)):
            n = self._walk_strings(obj)
            if n:
                return json.dumps(obj, ensure_ascii=False), n
        return args_str, 0

    def status(self):
        with self._lock:
            return {
                "enabled": self._enabled,
                "dictionary_version": self._dictionary_version,
                "word_count": len(self._pairs),
                "targets": sorted(self._targets),
            }

    def reload(self):
        """重新加载词典（运行期可调用，不影响进行中的请求）。"""
        return self.load()


content_filter = ContentFilter()

class TokenTracker:
    def __init__(self, db_path="token_stats.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self, timeout=5):
        """建立连接并应用 WAL 配套 pragma（synchronous 是 per-connection 属性，每次连接都要设置）。"""
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        # WAL 持久化在 DB 头，幂等；DB 恢复/拷贝后启动时自动回 WAL
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
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
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO token_usage (endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens) VALUES (?, ?, ?, ?, ?, ?)",
                        (endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens)
                    )
            except Exception as e:
                sys_log(f"记录 token 消耗失败: {e}", "WARN")
        threading.Thread(target=_do_insert, daemon=True).start()

    def rename_endpoint(self, old_name: str, new_name: str):
        try:
            with self._connect() as conn:
                conn.execute("UPDATE token_usage SET endpoint_name = ? WHERE endpoint_name = ?", (new_name, old_name))
        except Exception as e:
            sys_log(f"重命名端点统计数据失败: {e}", "WARN")

    def get_stats(self, endpoint_filter=None):
        with self._connect() as conn:
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
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, timestamp, endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens FROM token_usage ORDER BY id DESC")
            for row in cursor.fetchall():
                writer.writerow(row)
        return output.getvalue()

    def clear_data(self):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM token_usage")
            conn.commit()

token_tracker = TokenTracker()

class ChatLogger:
    RETENTION_DAYS = 30  # 对话日志滚动保留天数（2026-08-15 新增：超过该天数的记录定时删除）
    BATCH_SIZE = 50  # 批量写攒批上限（条）

    def __init__(self, db_path="chat_logs.db"):
        self.db_path = db_path
        self._lock = threading.Lock()  # 仅串行化写路径（SQLite 单写者）；读路径无锁，WAL 下多读并发
        self._log_queue = queue.Queue(maxsize=512)
        self._init_db()
        # 后台守护线程：每小时滚动清理一次过期日志（daemon 线程，失败不影响主服务）
        threading.Thread(target=self._retention_loop, daemon=True).start()
        # 后台守护线程：批量写对话日志（攒批单事务提交，减少锁竞争与 fsync）
        threading.Thread(target=self._batch_writer, daemon=True).start()

    def _connect(self, timeout=5):
        """建立连接并应用 WAL 配套 pragma（synchronous 是 per-connection 属性，每次连接都要设置）。"""
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _retention_loop(self):
        while True:
            try:
                self.prune_old_logs()
            except Exception as e:
                sys_log(f"滚动清理对话日志失败: {e}", "ERROR")
            time.sleep(3600)

    def prune_old_logs(self):
        """删除超过 RETENTION_DAYS 天的对话日志（按 UTC 时间戳比较），返回删除行数。"""
        with self._lock:
            try:
                conn = self._connect()
                c = conn.cursor()
                c.execute(
                    "DELETE FROM chat_logs WHERE timestamp < datetime('now', ?)",
                    (f"-{self.RETENTION_DAYS} days",)
                )
                deleted = c.rowcount
                conn.commit()
                conn.close()
                if deleted > 0:
                    sys_log(f"滚动清理对话日志: 删除 {deleted} 条超过 {self.RETENTION_DAYS} 天的记录")
                return deleted
            except Exception as e:
                sys_log(f"滚动清理对话日志失败: {e}", "ERROR")
                return 0


    def _init_db(self):
        with self._lock:
            conn = self._connect()
            # WAL 持久化在 DB 头，幂等；DB 恢复/拷贝后启动时自动回 WAL
            conn.execute("PRAGMA journal_mode=WAL")
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                endpoint_name TEXT,
                model TEXT,
                prompt TEXT,
                completion TEXT,
                total_tokens INTEGER,
                latency_ms INTEGER,
                pool_group TEXT,
                prompt_tokens INTEGER,
                cached_tokens INTEGER,
                reasoning_tokens INTEGER
            )''')
            # 存量库迁移：老表缺列时补齐（pool_group 2026-08-30 分组调用区分；prompt/cached tokens 同日命中统计）
            c.execute("PRAGMA table_info(chat_logs)")
            _cols = {row[1] for row in c.fetchall()}
            for _col, _ddl in (
                ("pool_group", "TEXT"),
                ("prompt_tokens", "INTEGER"),
                ("cached_tokens", "INTEGER"),
                ("reasoning_tokens", "INTEGER"),
            ):
                if _col not in _cols:
                    c.execute(f"ALTER TABLE chat_logs ADD COLUMN {_col} {_ddl}")
            conn.commit()
            conn.close()

    def add_log(self, endpoint_name, model, prompt, completion, total_tokens, latency_ms, pool_group=None, prompt_tokens=None, cached_tokens=None, reasoning_tokens=None):
        row = (endpoint_name, model, prompt, completion, total_tokens, latency_ms, pool_group, prompt_tokens, cached_tokens, reasoning_tokens)
        try:
            self._log_queue.put_nowait(row)
        except queue.Full:
            # 队列满（极端突发）：降级为独立线程直写，不丢日志
            threading.Thread(target=self._write_batch, args=([row],), daemon=True).start()

    def _batch_writer(self):
        while True:
            row = self._log_queue.get()
            batch = [row]
            try:
                while len(batch) < self.BATCH_SIZE:
                    batch.append(self._log_queue.get_nowait())
            except queue.Empty:
                pass
            self._write_batch(batch)

    def _write_batch(self, batch):
        with self._lock:
            try:
                conn = self._connect()
                c = conn.cursor()
                c.executemany(
                    "INSERT INTO chat_logs (endpoint_name, model, prompt, completion, total_tokens, latency_ms, pool_group, prompt_tokens, cached_tokens, reasoning_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()
                conn.close()
            except Exception as e:
                sys_log(f"记录对话日志失败: {e}", "ERROR")

    def get_logs(self, limit=50, offset=0, detail=True):
        # 读路径无锁：WAL 下多读者并发安全，不被日志写入/滚动清理阻塞
        try:
            conn = self._connect()
            c = conn.cursor()
            # detail=False：SQL 层不取 prompt/completion（避免从磁盘读 32MB 正文再丢弃）
            if detail:
                c.execute(
                    "SELECT id, datetime(timestamp, 'localtime'), endpoint_name, model, prompt, completion, total_tokens, latency_ms, pool_group, prompt_tokens, cached_tokens, reasoning_tokens FROM chat_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset)
                )
            else:
                c.execute(
                    "SELECT id, datetime(timestamp, 'localtime'), endpoint_name, model, total_tokens, latency_ms, pool_group, prompt_tokens, cached_tokens, reasoning_tokens FROM chat_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset)
                )
                rows = [r[:4] + (None, None) + r[4:] for r in c.fetchall()]
                c.execute("SELECT COUNT(*) FROM chat_logs")
                total = c.fetchone()[0]
                conn.close()
                return {
                    "total": total,
                    "logs": [
                        {
                            "id": r[0], "timestamp": r[1], "endpoint_name": r[2], "model": r[3],
                            "prompt": None, "completion": None,
                            "total_tokens": r[6], "latency_ms": r[7], "pool_group": r[8],
                            "prompt_tokens": r[9], "cached_tokens": r[10], "reasoning_tokens": r[11]
                        } for r in rows
                    ]
                }
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
                        # 列表页不需要正文：detail=False 时不返回 prompt/completion（33MB→KB 级）
                        "prompt": r[4] if detail else None,
                        "completion": r[5] if detail else None,
                        "total_tokens": r[6],
                        "latency_ms": r[7],
                        "pool_group": r[8],
                        "prompt_tokens": r[9],
                        "cached_tokens": r[10],
                        "reasoning_tokens": r[11]
                    } for r in rows
                ]
            }
        except Exception as e:
            return {"total": 0, "logs": [], "error": str(e)}

    def get_log_by_id(self, log_id):
        """单条详情（含正文）：列表页 detail=false 不拉正文，点击行时按 id 取全文。"""
        # 读路径无锁：WAL 下多读者并发安全
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute(
                "SELECT id, datetime(timestamp, 'localtime'), endpoint_name, model, prompt, completion, total_tokens, latency_ms, pool_group, prompt_tokens, cached_tokens, reasoning_tokens FROM chat_logs WHERE id = ?",
                (int(log_id),)
            )
            r = c.fetchone()
            conn.close()
            if r is None:
                return None
            return {
                "id": r[0],
                "timestamp": r[1],
                "endpoint_name": r[2],
                "model": r[3],
                "prompt": r[4],
                "completion": r[5],
                "total_tokens": r[6],
                "latency_ms": r[7],
                "pool_group": r[8],
                "prompt_tokens": r[9],
                "cached_tokens": r[10],
                "reasoning_tokens": r[11]
            }
        except Exception as e:
            return None

    def clear_logs(self):
        with self._lock:
            try:
                conn = self._connect()
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

def _responses_instructions_text(instructions):
    if isinstance(instructions, str):
        return instructions
    if isinstance(instructions, list):
        texts = []
        for item in instructions:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and item.get("type") in ("input_text", "text", "output_text"):
                texts.append(item.get("text", ""))
        return "\n".join(t for t in texts if t)
    return str(instructions or "")


def _responses_content_to_chat(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append({"type": "text", "text": c})
                continue
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype in ("input_text", "output_text", "text"):
                parts.append({"type": "text", "text": c.get("text", "")})
            elif ctype in ("input_image", "image_url"):
                raw_url = c.get("image_url")
                if isinstance(raw_url, dict):
                    url = raw_url.get("url", "")
                    detail = c.get("detail") or raw_url.get("detail")
                else:
                    url = raw_url or ""
                    detail = c.get("detail")
                image = {"url": url}
                if detail:
                    image["detail"] = detail
                parts.append({"type": "image_url", "image_url": image})
        if not parts:
            return ""
        if len(parts) == 1 and parts[0]["type"] == "text":
            return parts[0]["text"]
        return parts
    return str(content)


def _responses_content_text(content):
    if isinstance(content, str):
        return content
    texts = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, str):
                texts.append(c)
            elif isinstance(c, dict):
                ctype = c.get("type")
                if ctype in ("input_text", "output_text", "text"):
                    texts.append(c.get("text", ""))
    return "\n".join(t for t in texts if t)


def _responses_input_to_messages(input_data, instructions):
    messages = []
    if instructions:
        instr_text = _responses_instructions_text(instructions)
        if instr_text:
            messages.append({"role": "system", "content": instr_text})

    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
        return messages
    if not isinstance(input_data, list):
        raise ValueError("input must be a string or an array of input items")

    user_parts = []

    def flush_user_parts():
        nonlocal user_parts
        if user_parts:
            messages.append({"role": "user", "content": user_parts})
            user_parts = []

    for item in input_data:
        if isinstance(item, str):
            user_parts.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("input_text", "text"):
            user_parts.append({"type": "text", "text": item.get("text", "")})
        elif item_type in ("input_image", "image_url"):
            raw_url = item.get("image_url")
            if isinstance(raw_url, dict):
                url = raw_url.get("url", "")
                detail = item.get("detail") or raw_url.get("detail")
            else:
                url = raw_url or ""
                detail = item.get("detail")
            image = {"url": url}
            if detail:
                image["detail"] = detail
            user_parts.append({"type": "image_url", "image_url": image})
        elif item_type == "message":
            role = item.get("role", "user")
            content = _responses_content_to_chat(item.get("content"))
            if role in ("system", "developer"):
                text = content if isinstance(content, str) else _responses_content_text(content)
                if text:
                    messages.append({"role": "system", "content": text})
            else:
                safe_role = role if role in ("user", "assistant", "tool") else "user"
                messages.append({"role": safe_role, "content": content or ""})
        elif item_type == "function_call":
            flush_user_parts()
            fn = item.get("function") or {}
            tool_call = {
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", "") or fn.get("name", ""),
                    "arguments": item.get("arguments", "") or fn.get("arguments", "")
                }
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("tool_calls", []).append(tool_call)
            else:
                messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
        elif item_type == "function_call_output":
            flush_user_parts()
            call_id = item.get("call_id", "")
            output = item.get("output", "")
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": str(output)
            })
        elif item_type is None and item.get("role"):
            # OpenAI Responses easy-input message: {"role": ..., "content": ...}
            # without a "type" field. The official SDK accepts this shorthand;
            # Hermes' codex_responses transport sends user turns in this shape.
            role = item.get("role", "user")
            content = _responses_content_to_chat(item.get("content"))
            if role in ("system", "developer"):
                text = content if isinstance(content, str) else _responses_content_text(content)
                if text:
                    messages.append({"role": "system", "content": text})
            else:
                safe_role = role if role in ("user", "assistant", "tool") else "user"
                messages.append({"role": safe_role, "content": content or ""})

    if user_parts:
        messages.append({"role": "user", "content": user_parts})
    if not messages:
        raise ValueError("input is empty or contains no supported items")
    return messages


def _responses_tools_to_chat(tools):
    if not isinstance(tools, list):
        return []
    chat_tools = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        chat_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}}
            }
        })
    return chat_tools


def _responses_tool_choice_to_chat(tool_choice):
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = tool_choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def _responses_text_format_to_chat(text_param):
    if not isinstance(text_param, dict):
        return None
    fmt = text_param.get("format")
    if isinstance(fmt, str):
        return {"type": fmt} if fmt in ("text", "json_object") else None
    if not isinstance(fmt, dict):
        return None
    fmt_type = fmt.get("type")
    if fmt_type == "json_schema":
        json_schema = fmt.get("json_schema")
        if not isinstance(json_schema, dict):
            json_schema = fmt
        return {
            "type": "json_schema",
            "json_schema": {
                "name": json_schema.get("name") or "response",
                "schema": json_schema.get("schema") or {},
                "strict": bool(json_schema.get("strict", False))
            }
        }
    if fmt_type in ("text", "json_object"):
        return {"type": fmt_type}
    return None


def _chat_response_format_to_responses_text(response_format):
    if not isinstance(response_format, dict):
        return None
    fmt_type = response_format.get("type")
    if fmt_type == "json_schema":
        json_schema = response_format.get("json_schema") or {}
        return {
            "format": {
                "type": "json_schema",
                "name": json_schema.get("name") or "response",
                "schema": json_schema.get("schema") or {},
                "strict": bool(json_schema.get("strict", False))
            }
        }
    if fmt_type in ("text", "json_object"):
        return {"format": {"type": fmt_type}}
    return None


def _responses_to_chat_request(body):
    input_data = body.get("input")
    if input_data is None:
        raise ValueError("input is required")
    messages = _responses_input_to_messages(input_data, body.get("instructions"))

    extra = {}
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty",
                "seed", "user", "metadata", "logprobs", "top_logprobs"):
        if key in body:
            extra[key] = body[key]
    if "max_output_tokens" in body:
        extra["max_tokens"] = body["max_output_tokens"]
    if "max_completion_tokens" in body:
        extra["max_tokens"] = body["max_completion_tokens"]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        extra["reasoning_effort"] = reasoning["effort"]
    response_format = _responses_text_format_to_chat(body.get("text"))
    if response_format:
        extra["response_format"] = response_format

    tools = _responses_tools_to_chat(body.get("tools"))
    if tools:
        extra["tools"] = tools
        if "parallel_tool_calls" in body:
            extra["parallel_tool_calls"] = bool(body["parallel_tool_calls"])
        tool_choice = _responses_tool_choice_to_chat(body.get("tool_choice"))
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
    return messages, extra


def _responses_default_usage():
    return {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0
    }


def _responses_usage_from_chat_usage(usage):
    if not usage:
        return _responses_default_usage()
    reasoning_tokens = 0
    if isinstance(usage.get("completion_tokens_details"), dict):
        reasoning_tokens = usage["completion_tokens_details"].get("reasoning_tokens", 0) or 0
    cached_tokens = 0
    if isinstance(usage.get("prompt_tokens_details"), dict):
        cached_tokens = usage["prompt_tokens_details"].get("cached_tokens", 0) or 0
    return {
        "input_tokens": usage.get("prompt_tokens", 0) or 0,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": usage.get("completion_tokens", 0) or 0,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": usage.get("total_tokens", 0) or 0
    }


def _chat_content_to_responses_content(content, text_type="input_text"):
    # Responses 校验：assistant 正文只能是 output_text/refusal，user/system 才是
    # input_text（ps.air-outer 严格校验，assistant 带 input_text 直接 400）。
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    parts = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type")
        if ctype == "text":
            parts.append({"type": text_type, "text": c.get("text", "")})
        elif ctype == "image_url":
            raw_url = c.get("image_url") or {}
            if isinstance(raw_url, dict):
                url = raw_url.get("url", "")
                detail = c.get("detail") or raw_url.get("detail")
            else:
                url = raw_url or ""
                detail = c.get("detail")
            image = {"url": url}
            if detail:
                image["detail"] = detail
            parts.append({"type": "input_image", "image_url": image})
    return parts


def _chat_messages_to_responses_input(messages):
    instructions = []
    items = []
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            content = m.get("content", "")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            if content:
                instructions.append(content)
            continue
        if role == "assistant":
            content = m.get("content") or ""
            if content:
                items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": _chat_content_to_responses_content(content, "output_text")
                })
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "") or ""
                })
        elif role == "tool":
            output = m.get("content", "")
            if isinstance(output, list):
                output = "".join(p.get("text", "") for p in output if isinstance(p, dict) and p.get("type") == "text")
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id", ""),
                "output": output or ""
            })
        else:
            items.append({
                "type": "message",
                "role": "user",
                "content": _chat_content_to_responses_content(m.get("content", ""))
            })
    return items, "\n".join(i for i in instructions if i).strip()


def _responses_tools_from_chat(tools):
    if not isinstance(tools, list):
        return []
    responses_tools = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        responses_tools.append({
            "type": "function",
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}}
        })
    return responses_tools


def _responses_body_from_chat(payload):
    items, instructions = _chat_messages_to_responses_input(payload.get("messages", []))
    body = {
        "model": payload.get("model", ""),
        "input": items or "",
    }
    if instructions:
        body["instructions"] = instructions
    if "max_tokens" in payload:
        body["max_output_tokens"] = payload["max_tokens"]
    for key in ("stream", "temperature", "top_p", "user", "metadata",
                "parallel_tool_calls", "store", "reasoning", "previous_response_id", "response_format"):
        if key in payload:
            body[key] = payload[key]
    if "response_format" in payload:
        text = _chat_response_format_to_responses_text(payload["response_format"])
        if text:
            body["text"] = text
        body.pop("response_format", None)
    # Hermes 顶层 reasoning_effort → Responses 的 reasoning.effort：chat/completions
    # 下「tools + reasoning_effort」被上游拒（建议改用 /v1/responses），转成 Responses
    # 体时必须带上推理强度，否则客户端设置被静默丢弃。minimal 上游不收，钳到 low。
    effort = str(payload.get("reasoning_effort") or "").strip().lower()
    if effort and "reasoning" not in body:
        # summary=auto：Responses 只在显式请求摘要时返回推理文本（否则只有 encrypted_content）。
        body["reasoning"] = {"effort": "low" if effort == "minimal" else effort, "summary": "auto"}
    tools = _responses_tools_from_chat(payload.get("tools"))
    if "_responses_response_format" in payload:
        text = _chat_response_format_to_responses_text(payload["_responses_response_format"])
        if text:
            body["text"] = text
    if tools:
        body["tools"] = tools
        tool_choice = payload.get("tool_choice")
        if tool_choice is not None:
            if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
                body["tool_choice"] = {
                    "type": "function",
                    "name": tool_choice.get("function", {}).get("name", "")
                }
            else:
                body["tool_choice"] = tool_choice
    return body


def _responses_output_to_chat_message(output):
    text = ""
    tool_calls = []
    reasoning = ""
    for item in output or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text += part.get("text", "")
        elif item.get("type") == "reasoning":
            # Responses 的推理摘要 → chat 的 reasoning_content（需请求体带 reasoning.summary）
            for part in item.get("summary") or []:
                if isinstance(part, dict) and part.get("type") == "summary_text":
                    reasoning += part.get("text", "")
        elif item.get("type") == "function_call":
            tool_calls.append({
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "") or ""
                }
            })
    return text, tool_calls, reasoning


def _responses_usage_to_chat_usage(usage):
    if not usage:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0}
        }
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("input_tokens", 0) or 0,
        "completion_tokens": usage.get("output_tokens", 0) or 0,
        "total_tokens": usage.get("total_tokens", 0) or 0,
        "prompt_tokens_details": {"cached_tokens": input_details.get("cached_tokens", 0) or 0},
        "completion_tokens_details": {"reasoning_tokens": output_details.get("reasoning_tokens", 0) or 0}
    }


RESPONSES_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "responses_store.db")
RESPONSES_STORE_LOCK = threading.Lock()
RESPONSES_STORE_MAX = 200


def _responses_store_init():
    with sqlite3.connect(RESPONSES_DB, timeout=5) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS responses_store (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()


def _responses_store_put(response_id, data):
    with RESPONSES_STORE_LOCK:
        with sqlite3.connect(RESPONSES_DB, timeout=5) as conn:
            conn.execute(
                """
                INSERT INTO responses_store (id, data, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET data = excluded.data, created_at = excluded.created_at
                """,
                (response_id, json.dumps(data, ensure_ascii=False), time.time())
            )
            conn.execute(
                """
                DELETE FROM responses_store
                WHERE id NOT IN (
                    SELECT id FROM responses_store ORDER BY created_at DESC LIMIT ?
                )
                """,
                (RESPONSES_STORE_MAX,)
            )
            conn.commit()


def _responses_store_get(response_id):
    with RESPONSES_STORE_LOCK:
        with sqlite3.connect(RESPONSES_DB, timeout=5) as conn:
            cursor = conn.execute(
                "SELECT data FROM responses_store WHERE id = ?",
                (response_id,)
            )
            row = cursor.fetchone()
            return json.loads(row[0]) if row else None


def _responses_store_delete(response_id):
    with RESPONSES_STORE_LOCK:
        with sqlite3.connect(RESPONSES_DB, timeout=5) as conn:
            cursor = conn.execute(
                "DELETE FROM responses_store WHERE id = ?",
                (response_id,)
            )
            conn.commit()
            return cursor.rowcount > 0


_responses_store_init()


def _responses_function_call_item(tool_call, idx):
    fn = tool_call.get("function") or {}
    call_id = tool_call.get("id") or f"call_{int(time.time() * 1000)}_{idx}"
    return {
        "id": f"fc_{int(time.time() * 1000)}_{idx}",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": fn.get("name", ""),
        "arguments": fn.get("arguments", "") or "",
        "output": None
    }


def _responses_response_object(body, text, usage=None, model="api-pool-aggregated",
                               status="completed", response_id=None, item_id=None,
                               message=None, output=None):
    now = int(time.time())
    rid = response_id or f"resp_{int(time.time() * 1000)}"
    mid = item_id or f"msg_{int(time.time() * 1000)}"
    text_value = text if isinstance(text, str) else str(text or "")
    item = {
        "id": mid,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text_value, "annotations": []}]
    }
    if output is not None:
        output_items = output
    else:
        msg = message or {}
        tool_calls = msg.get("tool_calls") or []
        output_items = []
        if text_value or not tool_calls:
            output_items.append(item)
        for idx, tool_call in enumerate(tool_calls):
            output_items.append(_responses_function_call_item(tool_call, idx))
    text_param = body.get("text")
    if isinstance(text_param, dict):
        text_format = text_param.get("format")
        if not isinstance(text_format, dict):
            text_format = {"type": "text"}
    else:
        text_format = {"type": "text"}
    reasoning_param = body.get("reasoning")
    reasoning_effort = reasoning_param.get("effort") if isinstance(reasoning_param, dict) else None
    return {
        "id": rid,
        "object": "response",
        "created_at": now,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "model": model,
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "previous_response_id": body.get("previous_response_id"),
        "reasoning": {"effort": reasoning_effort, "summary": None},
        "store": bool(body.get("store", False)),
        "temperature": body.get("temperature", 0.7),
        "text": {"format": text_format},
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": body.get("tools", []),
        "top_p": body.get("top_p", 1),
        "truncation": body.get("truncation", "disabled"),
        "usage": usage or _responses_default_usage(),
        "user": body.get("user"),
        "metadata": body.get("metadata") or {},
        "expires_at": None,
        "output": output_items
    }


def _responses_sse(event_type, data):
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


def _parse_chat_completion_chunk(chunk):
    if not chunk:
        return None
    raw = chunk.decode("utf-8", errors="ignore")
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            return None
        try:
            return json.loads(payload)
        except Exception:
            pass
    return None


def _responses_stream_generator(upstream_gen, body, meta=None, model="api-pool-aggregated",
                                response_id=None, item_id=None, on_complete=None):
    is_responses = bool((meta or {}).get("protocol") == "responses")
    response_id = response_id or f"resp_{int(time.time() * 1000)}"
    item_id = item_id or f"msg_{int(time.time() * 1000)}"
    output_index = 0
    content_index = 0
    final_text = ""
    final_usage = None
    tool_states = {}
    responses_tool_states = {}
    next_output_index = 1
    seq = 0

    def emit(event_type, data):
        nonlocal seq
        seq += 1
        payload = {"sequence_number": seq, "response_id": response_id}
        payload.update(data)
        return _responses_sse(event_type, payload)

    base_response = _responses_response_object(body, "", status="in_progress",
                                               response_id=response_id, item_id=item_id,
                                               model=model)
    base_item = base_response["output"][0]
    base_part = base_item["content"][0]
    output_items = [base_item]

    yield emit("response.created", {"type": "response.created", "response": base_response})
    yield emit("response.in_progress", {"type": "response.in_progress", "response": base_response})
    yield emit("response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": output_index,
        "item": base_item
    })
    yield emit("response.content_part.added", {
        "type": "response.content_part.added",
        "item_id": item_id,
        "output_index": output_index,
        "content_index": content_index,
        "part": base_part
    })

    try:
        for chunk in upstream_gen:
            parsed = _parse_chat_completion_chunk(chunk)
            if not parsed:
                # A native Responses stream may carry an event name on a separate SSE line;
                # preserve it when parsing the JSON data payload.
                try:
                    lines = chunk.decode("utf-8", errors="ignore").splitlines()
                    event_line = next((line[7:] for line in lines if line.startswith("event: ")), "")
                    data_line = next((line[6:] for line in lines if line.startswith("data: ")), "")
                    parsed = json.loads(data_line) if data_line and data_line != "[DONE]" else None
                    if parsed is not None and event_line and not parsed.get("type"):
                        parsed["type"] = event_line.strip()
                except Exception:
                    parsed = None
            if not parsed:
                continue
            if parsed.get("type") == "response.output_text.delta":
                parsed = {"choices": [{"delta": {"content": parsed.get("delta", "")}}]}
            elif parsed.get("type") == "response.output_item.added":
                item = parsed.get("item") or {}
                if item.get("type") == "function_call":
                    idx = parsed.get("output_index", len(tool_states))
                    tool_states[idx] = {
                        "item_id": item.get("id", f"fc_{int(time.time() * 1000)}_{idx}"),
                        "call_id": item.get("call_id", ""),
                        "name": item.get("name", ""), "arguments": item.get("arguments", ""),
                        "output_index": idx, "item": item,
                    }
                continue
            elif parsed.get("type") == "response.function_call_arguments.delta":
                idx = parsed.get("output_index", len(tool_states))
                state = tool_states.setdefault(idx, {"item_id": parsed.get("item_id", ""), "call_id": "", "name": "", "arguments": "", "output_index": idx, "item": {}})
                state["arguments"] += parsed.get("delta", "")
                continue
            elif parsed.get("type") == "response.function_call_arguments.done":
                idx = parsed.get("output_index", len(tool_states))
                state = tool_states.setdefault(idx, {"item_id": parsed.get("item_id", ""), "call_id": "", "name": parsed.get("name", ""), "arguments": "", "output_index": idx, "item": {}})
                state["arguments"] = parsed.get("arguments", state["arguments"])
                continue
            elif parsed.get("type") == "response.completed":
                parsed = {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": (parsed.get("response") or {}).get("usage")}
            choices = parsed.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                text_delta = delta.get("content")
                if text_delta:
                    final_text += text_delta
                    yield emit("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "delta": text_delta
                    })
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", len(tool_states))
                    state = tool_states.get(idx)
                    if state is None:
                        call_id = tc.get("id") or f"call_{int(time.time() * 1000)}_{idx}"
                        fc_id = f"fc_{int(time.time() * 1000)}_{idx}"
                        name = (tc.get("function") or {}).get("name", "")
                        state = {
                            "item_id": fc_id,
                            "call_id": call_id,
                            "name": name,
                            "arguments": "",
                            "output_index": next_output_index,
                            "item": {
                                "id": fc_id,
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": call_id,
                                "name": name,
                                "arguments": "",
                                "output": None
                            }
                        }
                        tool_states[idx] = state
                        output_items.append(state["item"])
                        next_output_index += 1
                        yield emit("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": state["output_index"],
                            "item": state["item"]
                        })
                    if tc.get("id"):
                        state["call_id"] = tc["id"]
                        state["item"]["call_id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        state["name"] = fn["name"]
                        state["item"]["name"] = fn["name"]
                    arg_delta = fn.get("arguments") or ""
                    if arg_delta:
                        state["arguments"] += arg_delta
                        state["item"]["arguments"] = state["arguments"]
                        yield emit("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "item_id": state["item_id"],
                            "output_index": state["output_index"],
                            "delta": arg_delta
                        })
            if parsed.get("usage"):
                final_usage = parsed["usage"]

        meta_message = (meta or {}).get("message") or {}
        for idx, state in sorted(responses_tool_states.items(), key=lambda item: item[0]):
            tool_states[idx] = {"item_id": state["item_id"], "call_id": state["call_id"], "name": state["name"], "arguments": state["arguments"], "output_index": idx, "item": state}
            yield emit("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "item_id": state["item_id"], "output_index": idx, "name": state["name"], "arguments": state["arguments"]})
        for idx, tc in enumerate((meta_message.get("tool_calls") or [])):
            fn = tc.get("function") or {}
            arguments = fn.get("arguments", "") or ""
            call_id = tc.get("id") or f"call_{int(time.time() * 1000)}_{idx}"
            existing = [s for s in tool_states.values()]
            if idx in tool_states:
                match = tool_states[idx]
            elif call_id:
                match = next((s for s in existing if s["call_id"] == call_id), None)
            else:
                match = next(
                    (s for s in existing if s["name"] == fn.get("name") and not s["call_id"]),
                    None
                )
            if match is not None:
                if arguments and not match["arguments"]:
                    match["arguments"] = arguments
                    match["item"]["arguments"] = arguments
                    yield emit("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": match["item_id"],
                        "output_index": match["output_index"],
                        "delta": arguments
                    })
                continue
            fc_id = f"fc_{int(time.time() * 1000)}_{idx}"
            state = {
                "item_id": fc_id,
                "call_id": call_id,
                "name": fn.get("name", ""),
                "arguments": arguments,
                "output_index": next_output_index,
                "item": {
                    "id": fc_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": fn.get("name", ""),
                    "arguments": arguments,
                    "output": None
                }
            }
            tool_states[idx] = state
            output_items.append(state["item"])
            next_output_index += 1
            yield emit("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": state["output_index"],
                "item": state["item"]
            })
            if arguments:
                yield emit("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": state["item_id"],
                    "output_index": state["output_index"],
                    "delta": arguments
                })

        for idx, state in sorted(tool_states.items(), key=lambda item: item[1]["output_index"]):
            yield emit("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done", "item_id": state["item_id"],
                "output_index": state["output_index"], "name": state["name"], "arguments": state["arguments"]
            })

        yield emit("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "text": final_text
        })
        done_part = {"type": "output_text", "text": final_text, "annotations": []}
        yield emit("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "part": done_part
        })
        done_item = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [done_part]
        }
        yield emit("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": done_item
        })
        completed_output = [done_item]
        for idx in sorted(tool_states, key=lambda i: tool_states[i]["output_index"]):
            state = tool_states[idx]
            yield emit("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": state["item_id"],
                "output_index": state["output_index"],
                "name": state["name"],
                "arguments": state["arguments"]
            })
            done_call = {
                "id": state["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": state["arguments"],
                "output": None
            }
            completed_output.append(done_call)
            yield emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": state["output_index"],
                "item": done_call
            })
        if final_usage is None and (meta or {}).get("usage"):
            final_usage = meta["usage"]
        completed_response = _responses_response_object(
            body, final_text,
            usage=_responses_usage_from_chat_usage(final_usage),
            status="completed",
            response_id=response_id,
            item_id=item_id,
            model=model,
            output=completed_output
        )
        yield emit("response.completed", {
            "type": "response.completed",
            "response": completed_response
        })
        yield b"data: [DONE]\n\n"
        if on_complete:
            on_complete(completed_response)
    except Exception as e:
        error_obj = {"message": str(e), "type": "server_error", "param": None, "code": None}
        failed_response = _responses_response_object(
            body, final_text,
            usage=_responses_usage_from_chat_usage(final_usage),
            status="failed",
            response_id=response_id,
            item_id=item_id,
            model=model,
            output=output_items
        )
        failed_response["error"] = error_obj
        yield emit("response.failed", {
            "type": "response.failed",
            "response": failed_response
        })
        yield b"data: [DONE]\n\n"


# ============================================================
#  数据结构
# ============================================================


def _get_resp_socket(resp):
    """Get the underlying socket of an http.client.HTTPResponse for settimeout().
    Handles SSL-wrapped sockets by unwrapping _sock layers."""
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
        # Unwrap SSL socket layers (SSLSocket._sock → plain socket)
        while hasattr(sock, '_sock') and sock._sock is not None:
            sock = sock._sock
        return sock
    except Exception:
        return None


class _DecodedResponse:
    """Content-Encoding 感知解压（仅 gzip/deflate）的 HTTPResponse 包装。

    客户端伪装（2026-09-10）配套：出站声明 Accept-Encoding: gzip, deflate 后，
    上游可能真实压缩响应，urllib 不会自动解压，必须在此处理。

    铁律：
    1. 无 Content-Encoding / identity / 未知编码 → 完全透传，零开销
    2. 解压失败 → 异常向上传播（调用方按上游失败处理），绝不吐损坏字节
    3. deflate 双模式：wbits=47（gzip/zlib 自动识别）首块失败回退 wbits=-15（裸 deflate RFC1951）
    """

    def __init__(self, resp):
        self._resp = resp
        try:
            enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
        except Exception:
            enc = ""
        self._encoding = enc if enc in ("gzip", "deflate") else ""
        self._decoder = None
        self._buf = b""
        self._eof = False
        self._first_raw = None  # 首块原始字节，用于裸 deflate 回退重放

    def _decode(self, raw):
        if self._decoder is None:
            self._decoder = zlib.decompressobj(47)
        if self._first_raw is None:
            self._first_raw = raw
        try:
            out = self._decoder.decompress(raw)
            self._first_raw = None  # 成功消费，不再需要重放基线
            return out
        except zlib.error:
            if self._first_raw is raw:
                # 首块即失败：可能是裸 deflate（RFC1951 无 zlib 头），用 -15 重放首块
                try:
                    self._decoder = zlib.decompressobj(-15)
                    return self._decoder.decompress(raw)
                except zlib.error:
                    pass
            # 首块成功后的中途损坏，或回退也失败：按铁律 2 抛（上游失败）
            raise

    def _flush(self):
        if self._decoder is None:
            return b""
        return self._decoder.flush()

    def _fill(self):
        """从底层拉一块数据解压进缓冲。返回 False 表示 EOF 且缓冲已空。"""
        raw = self._resp.read(65536)
        if not raw:
            self._eof = True
            out = self._flush()
        else:
            out = self._decode(raw)
        if out:
            self._buf += out
        return not self._eof or bool(self._buf)

    def read(self, size=-1):
        if not self._encoding:
            return self._resp.read() if size is None or size < 0 else self._resp.read(size)
        if size is None or size < 0:
            while not self._eof:
                self._fill()
            out, self._buf = self._buf, b""
            return out
        while len(self._buf) < size and not self._eof:
            self._fill()
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def readline(self, limit=-1):
        if not self._encoding:
            return self._resp.readline() if limit is None or limit < 0 else self._resp.readline(limit)
        while b"\n" not in self._buf and not self._eof:
            self._fill()
        if b"\n" in self._buf:
            idx = self._buf.index(b"\n") + 1
            out, self._buf = self._buf[:idx], self._buf[idx:]
        else:
            out, self._buf = self._buf, b""
        return out

    def __iter__(self):
        if not self._encoding:
            return iter(self._resp)

        def _gen():
            while True:
                line = self.readline()
                if not line:
                    return
                yield line

        return _gen()

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, item):
        return getattr(self._resp, item)


def _anthropic_tools_from_chat(tools):
    """将 OpenAI 格式的 tools 转换为 Anthropic 格式。"""
    if not isinstance(tools, list):
        return []
    anthropic_tools = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        anthropic_tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}
        })
    return anthropic_tools


def _anthropic_tool_choice_from_chat(tool_choice):
    """将 OpenAI 格式的 tool_choice 转换为 Anthropic 格式。"""
    if isinstance(tool_choice, str):
        if tool_choice in ("required", "any"):
            return {"type": "any"}
        return {"type": tool_choice if tool_choice in ("auto", "none") else "auto"}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        fn = tool_choice.get("function") or {}
        name = fn.get("name") or tool_choice.get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


# ── Gemini 原生方言桥接（2026-09-12）──────────────────────────────────────────
# SoleAPI 一类网关对 Gemini 系模型只开 /v1beta/models/{model}:generateContent：同一模型
# 走 /v1/chat/completions、/v1/responses、/v1/messages 一律 404「没有能承接该入口协议
# 的货源」。端点级 protocol="gemini" → 入站保持 OpenAI chat 形态，出站转 Gemini 原生
# 方言，响应再转回 OpenAI 形态。
#
# thoughtSignature 必须回传（实测 gemini-3.8-flash，thinkingBudget=0 亦然）：functionCall
# part 不带签名续跑必 400「Function call is missing a thought_signature」。签名只出现在
# 上游响应里、OpenAI 形态没有对应字段，故按 pool 生成的 tool_call id 记在进程内表里。
# ponytail: 进程内 TTL 表，进程重启即失忆；失忆（或历史来自其他协议的端点）时该轮工具
# 调用降级为文本 parts，需要跨重启保真再改存 sqlite。
_GEMINI_SIG_TTL = 3600
_GEMINI_SIG_MAX = 4000
_gemini_tool_sigs: dict = {}          # {tool_call_id: (name, thoughtSignature, ts)}
_gemini_tool_sigs_lock = threading.Lock()
_GEMINI_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
}


def _gemini_remember_tool_call(name, signature):
    """登记一次上游 functionCall，返回下行 OpenAI 形态用的 tool_call id。"""
    import uuid
    call_id = "call_" + uuid.uuid4().hex[:20]
    if signature:
        with _gemini_tool_sigs_lock:
            now = time.time()
            for k in [k for k, v in _gemini_tool_sigs.items() if now - v[2] > _GEMINI_SIG_TTL]:
                _gemini_tool_sigs.pop(k, None)
            while len(_gemini_tool_sigs) >= _GEMINI_SIG_MAX:
                _gemini_tool_sigs.pop(next(iter(_gemini_tool_sigs)), None)
            _gemini_tool_sigs[call_id] = (name or "", signature, now)
    return call_id


def _gemini_lookup_tool_call(call_id):
    with _gemini_tool_sigs_lock:
        entry = _gemini_tool_sigs.get(str(call_id or ""))
    return (entry[0], entry[1]) if entry else ("", "")


def _gemini_url(base_url, model, stream=False):
    """base_url 三种写法都接受：…/v1、…/v1beta、裸域名。"""
    base = str(base_url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3] + "/v1beta"
    elif not base.endswith("/v1beta"):
        base += "/v1beta"
    method = "streamGenerateContent" if stream else "generateContent"
    return f"{base}/models/{model}:{method}" + ("?alt=sse" if stream else "")


def _gemini_text_from_content(content):
    """OpenAI content（str / block 列表）里的纯文本，用于 system 与工具结果。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("text"):
                texts.append(b["text"])
            elif b.get("type") == "image_url":
                url = (b.get("image_url") or {}).get("url", "") if isinstance(b.get("image_url"), dict) else ""
                if url:
                    texts.append(f"[Image URL: {url}]")
        return "\n".join(texts)
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _gemini_parts_from_content(content):
    """OpenAI content blocks → Gemini parts（图片 data URL → inlineData）。"""
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts = []
    if not isinstance(content, list):
        return parts
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "image_url":
            url = (b.get("image_url") or {}).get("url", "") if isinstance(b.get("image_url"), dict) else ""
            if url.startswith("data:image/"):
                try:
                    header, b64 = url.split(",", 1)
                    mime = header.split(";")[0].replace("data:", "") or "image/png"
                    parts.append({"inlineData": {"mimeType": mime, "data": b64}})
                except ValueError:
                    pass
            elif url:
                # 远端 URL 不代抓，降级为占位文本（与 Anthropic 桥同策略）
                parts.append({"text": f"[Image URL: {url}]"})
        elif b.get("text"):
            parts.append({"text": b["text"]})
    return parts


def _gemini_tools_from_chat(tools):
    """OpenAI tools → Gemini functionDeclarations（名称/描述/参数 schema 原样搬）。"""
    decls = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") in (None, "function") else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        decl = {"name": fn["name"]}
        if fn.get("description"):
            decl["description"] = fn["description"]
        params = fn.get("parameters")
        decl["parameters"] = params if isinstance(params, dict) else {"type": "object", "properties": {}}
        decls.append(decl)
    return decls


def _gemini_tool_config_from_chat(tool_choice):
    """OpenAI tool_choice → Gemini toolConfig（auto/none/required/指定函数）。"""
    mode = None
    allowed = []
    if isinstance(tool_choice, str):
        mode = {"auto": "AUTO", "none": "NONE", "required": "ANY", "any": "ANY"}.get(tool_choice.lower())
    elif isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        name = fn.get("name") or tool_choice.get("name")
        if tool_choice.get("type") == "function" and name:
            mode, allowed = "ANY", [name]
        elif tool_choice.get("type") == "auto":
            mode = "AUTO"
    if not mode:
        return None
    cfg = {"mode": mode}
    if allowed:
        cfg["allowedFunctionNames"] = allowed
    return {"functionCallingConfig": cfg}


def _gemini_payload_from_chat(payload, ep=None):
    """OpenAI chat payload → Gemini generateContent 请求体（模型名走 URL 路径）。"""
    synthetic = {}          # 无签名工具调用 → 对应结果也用文本形态回填
    call_names = {}         # tool_call_id → 函数名（functionResponse 按名回填）
    system_texts = []
    contents = []
    missing_sig_logged = False

    def push(role, parts):
        if not parts:
            return
        last = contents[-1] if contents else None
        # 含 functionResponse 的工具轮不能混入普通文本：上游对 "工具结果 + 同轮文本" 直接 400
        # （Requests ending with a model turn are not supported）→ 另起一轮，保持工具轮纯净。
        if (last is not None and last["role"] == role
                and not (any("functionResponse" in p for p in last["parts"])
                         and not any("functionResponse" in p for p in parts))):
            last["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("system", "developer"):
            text = _gemini_text_from_content(m.get("content"))
            if text.strip():
                system_texts.append(text)
            continue
        if role == "tool":
            call_id = m.get("tool_call_id") or ""
            name = call_names.get(call_id) or "tool"
            raw = _gemini_text_from_content(m.get("content"))
            if call_id in synthetic:
                push("user", [{"text": f"[工具 {name} 返回] {raw}"}])
            else:
                obj = None
                if raw.strip().startswith(("{", "[")):
                    try:
                        obj = json.loads(raw)
                    except (TypeError, ValueError):
                        obj = None
                if not isinstance(obj, dict):
                    obj = {"result": raw}
                push("user", [{"functionResponse": {"name": name, "response": obj}}])
            continue
        if role != "assistant":
            push("user", _gemini_parts_from_content(m.get("content")))
            continue
        parts = _gemini_parts_from_content(m.get("content"))
        calls = [tc for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]
        signed = []
        for tc in calls:
            fn = tc.get("function") or {}
            call_id = tc.get("id") or ""
            if call_id:
                call_names[call_id] = fn.get("name") or ""
            signed.append(_gemini_lookup_tool_call(call_id)[1])
        if calls and all(signed):
            for tc, sig in zip(calls, signed):
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except (TypeError, ValueError):
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                parts.append({
                    "functionCall": {"name": fn.get("name") or "", "args": args},
                    "thoughtSignature": sig,
                })
        elif calls:
            # 历史里的工具调用不是本进程生成的（跨协议端点/重启）→ 无签名，原样回传必被
            # 上游 400，退化成文本让对话继续。
            if not missing_sig_logged:
                missing_sig_logged = True
                if ep is not None:
                    sys_log(f"端点 '{ep.name}' 历史工具调用无 thoughtSignature（跨协议或重启后的历史），本轮退化为文本形态", "WARN")
            for tc in calls:
                fn = tc.get("function") or {}
                call_id = tc.get("id") or ""
                if call_id:
                    synthetic[call_id] = True
                parts.append({"text": f"[调用工具 {fn.get('name') or ''}，参数 {fn.get('arguments') or '{}'}]"})
        push("model", parts)

    if not contents:
        push("user", [{"text": "ping"}])
    body = {"contents": contents}
    if system_texts:
        body["systemInstruction"] = {"parts": [{"text": "\n".join(system_texts)}]}
    gen = {}
    if payload.get("temperature") is not None:
        gen["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        gen["topP"] = payload["top_p"]
    max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if max_tokens:
        gen["maxOutputTokens"] = max_tokens
    stop = payload.get("stop")
    if stop:
        gen["stopSequences"] = stop if isinstance(stop, list) else [stop]
    rf = payload.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        gen["responseMimeType"] = "application/json"
    if gen:
        body["generationConfig"] = gen
    decls = _gemini_tools_from_chat(payload.get("tools"))
    if decls:
        body["tools"] = [{"functionDeclarations": decls}]
        cfg = _gemini_tool_config_from_chat(payload.get("tool_choice"))
        if cfg:
            body["toolConfig"] = cfg
    return body


def _gemini_finish_reason(raw, has_tool_calls=False):
    if has_tool_calls:
        return "tool_calls"
    return _GEMINI_FINISH_REASONS.get(str(raw or "").upper(), "stop")


def _gemini_usage_to_chat_usage(meta):
    """usageMetadata → OpenAI usage（thoughts 计入 completion，另记 reasoning_tokens）。"""
    meta = meta if isinstance(meta, dict) else {}
    prompt_t = meta.get("promptTokenCount", 0) or 0
    cand_t = meta.get("candidatesTokenCount", 0) or 0
    thought_t = meta.get("thoughtsTokenCount", 0) or 0
    cached_t = meta.get("cachedContentTokenCount", 0) or 0
    completion_t = cand_t + thought_t
    total_t = meta.get("totalTokenCount") or (prompt_t + completion_t)
    return {
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "total_tokens": total_t,
        "prompt_tokens_details": {"cached_tokens": cached_t},
        "completion_tokens_details": {"reasoning_tokens": thought_t},
    }


def _gemini_parts_to_chat(candidate):
    """一个 candidate 的 parts → (text, reasoning, tool_calls)；工具调用顺带登记签名。"""
    text = ""
    reasoning = ""
    tool_calls = []
    if not isinstance(candidate, dict):
        return text, reasoning, tool_calls
    for p in ((candidate.get("content") or {}).get("parts") or []):
        if not isinstance(p, dict):
            continue
        if p.get("functionCall"):
            fc = p["functionCall"] or {}
            name = fc.get("name") or ""
            call_id = _gemini_remember_tool_call(name, p.get("thoughtSignature") or "")
            args = fc.get("args")
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False),
                },
            })
        elif p.get("text"):
            if p.get("thought"):
                reasoning += p["text"]
            else:
                text += p["text"]
    return text, reasoning, tool_calls


def _gemini_chunk_parts(chunk):
    """Gemini 流式 chunk → 首 candidate 的 parts（形态不符返回空）。"""
    if not isinstance(chunk, dict):
        return []
    candidates = chunk.get("candidates")
    cand = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    content = cand.get("content") if isinstance(cand.get("content"), dict) else {}
    parts = content.get("parts")
    return [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []


@dataclass
class Endpoint:
    id: str = ""
    name: str = "unnamed"
    site_name: str = ""
    site_id: str = ""  # 缓存账户 ID：base_url+API Key 相同则共享，不同 Key 硬隔离
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"
    priority: int = 999
    priority_by_group: dict = field(default_factory=dict)  # per-group 优先级（2026-08-29 分组隔离；main 组值同时镜像到 priority 字段保持旧格式兼容）
    timeout: int = 60
    max_retries: int = 1
    enabled: bool = True
    cooldown_minutes: int = 5
    use_proxy: bool = False
    protocol: str = "openai"
    extra_headers: dict = field(default_factory=dict)
    default_headers: dict = field(default_factory=dict)
    client_profile: str = ""  # 客户端伪装 profile 名（空=不伪装）；头合并优先级：profile < default_headers < extra_headers
    is_vision: bool = False
    in_pool: bool = False  # 是否加入聚合池（默认不加入）
    check_fake_success: bool = False  # 是否检测假成功（200 OK 但内容含拒绝信息）
    tool_call_id_prefix: str = ""
    reasoning_policy: str = "auto"  # auto=DeepSeek保留+GLM保留+其余剥离 | keep=强制保留回传 | strip=强制剥离
    # DeepSeek 严格校验 400 的同端点重试上限（0=不重试只轮转 / 1=原样重试 1 次 / 2=原样 1 次 +
    # 「原始 tool_call id」变体 1 次；变体仅在本次确实应用过 tool_call_id_prefix 重写时才发）。
    # 依据：该类 400 为上游实例级随机（zdsub2api 2026-09-12 外部实证：同 body 背靠背重放失败率 15–25%）。
    strict400_retries: int = 2
    preserved_thinking: bool = False  # GLM 保留式思考：注入 thinking.clear_thinking=False
    stream_first_packet_timeout: int = 120
    stream_stall_timeout: int = 60
    stream_max_duration: int = 0  # 流总时长上限（秒），0=禁用；正常持续输出默认不截断
    deferrable: bool = True  # 是否保护本端点缓存（true=本端点工作时延迟切走）
    max_context_k: int = 0  # 模型上下文长度（K=1000 tokens），0=不限；请求超限自动 fallback 下一端点
    pool_groups: list = field(default_factory=list)  # 未入池端点默认无池组；入池时再绑定组

    _fail_count: int = field(default=0, repr=False)
    _last_error: str = field(default="", repr=False)
    _last_error_ts: float = field(default=0, repr=False)
    _last_success_ts: float = field(default=0, repr=False)
    _total_calls: int = field(default=0, repr=False)
    _total_failures: int = field(default=0, repr=False)
    _cooldown_until: float = field(default=0, repr=False)
    _cooldown_reason: str = field(default="", repr=False)
    _manual_unlock_required: bool = field(default=False, repr=False)
    _defer_until: float = field(default=0, repr=False)  # 延迟回迁到期时间，在此期间不主动回迁到此端点
    
    health_mode: str = field(default="models")
    billing_mode: str = field(default="subscription")

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
        self._lock = threading.RLock()
        self._endpoints: list[Endpoint] = []
        self._restored_endpoint_id: str | None = None  # 兼容旧状态字段；恢复后转为持续手动覆盖
        self._last_pool_activity: float = 0  # 上次池活跃时间（用于 defer 判断）
        # ── 分组池路由状态（2026-08-29 spec：四个路由全局态 per-group 化）──
        # 每组独立的粘性指针/手动覆盖/持久化指针/兜底锁。旧扁平状态在 _migrate_legacy_state 兼容。
        self._current_endpoint_by_group: dict[str, str | None] = {}
        self._manual_override_by_group: dict[str, str | None] = {}
        # 每组路由决策版本：请求开始后若发生手动切换、恢复回迁或其他轮转，
        # 迟到的旧请求只更新自身健康状态，不得覆盖更新的路由决策。
        self._route_epoch_by_group: dict[str, int] = {}
        self._persisted_endpoint_by_group: dict[str, str | None] = {}
        self._fallback_lock_until_by_group: dict[str, float] = {}
        # 端点在途归属：ep.id → {组名: 在途请求数}。main 可抢占子组共享端点，
        # 因此必须按组计数，避免并发请求覆盖 owner 或提前释放仍在途的占用。
        self._inflight_owner: dict[str, dict[str, int]] = {}
        # 子组→main 组级延迟回切锁（2026-08-31 A0）：子组 fallback 落 main 后锁定
        # _GROUP_FALLBACK_RETURN_SECONDS 秒；期间该子组请求直走 main 不重复解析死组。
        # 锁定窗口为滑动空闲：锁定期内又有该子组请求 → 请求仍走 main，并把窗口
        # 顺延到最后一次请求后 N 秒（无请求 N 秒后才回组重试）。期满后第一个请求
        # 回组试探，成功即回组粘性，失败重新锁定（与 prio99 兜底锁语义对齐）。
        self._group_fallback_lock_until: dict[str, float] = {}
        # 「待回切」状态（2026-09-12）：该组曾 fallback，回切锁已期满但尚无该组请求
        # 回组试探（回切是懒的，等下一个请求；期间 UI 显示 ⏸待回切 而非隐藏状态）。
        # 期满后该组首个请求即清除（回切已发生）。不落盘：重启后锁已归零、下一个请求
        # 本就回组，持久化只会显示一个不存在的待回切状态。
        self._group_fallback_pending: dict[str, bool] = {}
        # 每组最近一次成功写入 usage 的站点账户 ID；用于识别手动切换、恢复回切
        # 和故障轮转形成的缓存账户边界。仅保存不敏感的 site_id。
        self._cache_stats_site_id_by_group: dict[str, str] = {}
        # ── 组实体定义（2026-08-30 组管理）：name → {"type": "mixed"|"dedicated", "model": selector} ──
        # mixed 组 model = Hermes 侧配置的选择器名（如 api-pool-bg）；dedicated 组 model = 绑定的
        # 真实上游模型名（兼作选择器）。main 恒为 mixed，选择器固定 api-pool（历史别名）。
        # 系统内置组：main（主组）+ 图片解析池（VISION_GROUP），恒在
        self._group_defs: dict[str, dict] = {
            self.MAIN_GROUP: {"type": "mixed", "model": "api-pool"},
            self.VISION_GROUP: {"type": "mixed", "model": self.VISION_SELECTOR},
        }
        self._client_profiles: dict = load_client_profiles()
        # 池自身出站的默认身份 profile（探活/测试/拉模型；2026-09-12）
        self._probe_client_profile: str = load_probe_client_profile()
        self._identity_skip_log_ts: dict = {}   # 无身份跳过日志去重 {(ep_id, reason): ts}
        # 图片翻译短 TTL 缓存：图片集合签名 → (时间戳, 描述)，消除多轮重发历史含图的重译（2026-09-10 视觉池组）
        self._vision_cache: dict[str, tuple[float, str]] = {}
        # 后台探活基础设施：冷却过期端点在后台线程探活，不阻塞请求路径
        self._probe_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="apipool-probe")
        self._probe_inflight = set()  # 正在探活的端点 id 集合（后台/批量探活共享，防重复请求）
        self._health_check_lock = threading.Lock()  # 防止多个全量探活批次重叠
        self._health_probe_max_workers = 2  # chat/models 探针会消耗上游并发与额度
        if endpoints:
            for ep in endpoints:
                # 批量加载：逐次 add 不做组内重排（增量重排以加载顺序为 tiebreak，
                # 会覆盖 config 已保存的组内优先级——2026-09-07 修复），循环后统一一次。
                self.add_endpoint(ep, renumber=False)
            self._renumber_pool_priorities()

    def add_endpoint(self, ep, renumber=True):
        if isinstance(ep, dict):
            raw_ep = ep
            ep_dict = {k: v for k, v in raw_ep.items() if k in Endpoint.__dataclass_fields__}
            ep_dict["max_retries"] = self._normalize_max_retries(ep_dict.get("max_retries", 1))
            if raw_ep.get("user_agent") and "default_headers" not in ep_dict:
                ep_dict["default_headers"] = {"User-Agent": raw_ep["user_agent"]}
            # pool_groups 归一化：未入池默认无组；旧的已入池配置缺字段时兼容 main。
            raw_groups = ep_dict.get("pool_groups")
            if not isinstance(raw_groups, list):
                ep_dict["pool_groups"] = ["main"] if ep_dict.get("in_pool", False) else []
            else:
                seen = []
                for g in raw_groups:
                    gs = str(g).strip()
                    if gs and gs not in seen:
                        seen.append(gs)
                ep_dict["pool_groups"] = seen
            # 新增时按组别自动设置健康检测模式（未显式指定时生效）
            if "health_mode" not in ep_dict:
                ep_dict["health_mode"] = "models" if ep_dict.get("billing_mode", "subscription") == "pay_per_use" else ("chat" if ep_dict.get("in_pool", False) else "models")
            if "billing_mode" not in ep_dict:
                ep_dict["billing_mode"] = "subscription"
            if not str(ep_dict.get("site_id", "") or "").strip():
                ep_dict["site_id"] = self._resolve_site_id(ep_dict.get("base_url", ""), ep_dict.get("api_key", ""))
            ep = Endpoint(**ep_dict)
            ep._manual_unlock_required = bool(raw_ep.get("manual_unlock_required", False))
        else:
            ep.max_retries = self._normalize_max_retries(ep.max_retries)
            if not ep.health_mode:
                ep.health_mode = "models" if ep.billing_mode == "pay_per_use" else ("chat" if ep.in_pool else "models")
        if not ep.id:
            import uuid
            ep.id = str(uuid.uuid4())
        if not ep.site_id:
            ep.site_id = self._resolve_site_id(ep.base_url, ep.api_key)
        with self._lock:
            self._endpoints.append(ep)
            self._endpoints.sort(key=lambda e: e.priority)
            if renumber:
                self._renumber_pool_priorities()

    def remove_endpoint(self, ep_id):
        with self._lock:
            self._endpoints = [e for e in self._endpoints if e.id != ep_id]
            for grp in list(self._current_endpoint_by_group):
                if self._current_endpoint_by_group.get(grp) == ep_id:
                    self._set_current(grp, None)
            self._renumber_pool_priorities()
            for grp in list(self._manual_override_by_group):
                if self._manual_override_by_group.get(grp) == ep_id:
                    self._set_manual(grp, None)
            self._inflight_owner.pop(ep_id, None)

    @staticmethod
    def _site_identity(base_url, api_key):
        """站点账户身份：规范化 URL + 完整 Key，仅用于进程内相等比较。"""
        return (str(base_url or "").strip().rstrip("/").lower(), str(api_key or ""))

    def _resolve_site_id(self, base_url, api_key, exclude_ep_id=None):
        """相同 base_url+Key 复用 site_id，否则生成新 ID。"""
        identity = self._site_identity(base_url, api_key)
        for existing in self._endpoints:
            if existing.id != exclude_ep_id and self._site_identity(existing.base_url, existing.api_key) == identity:
                if existing.site_id:
                    return existing.site_id
        import uuid
        return str(uuid.uuid4())

    def set_enabled(self, ep_id, enabled):
        with self._lock:
            for ep in self._endpoints:
                if ep.id == ep_id:
                    ep.enabled = enabled

    def set_pool(self, ep_id, in_pool, groups=None):
        with self._lock:
            for ep in self._endpoints:
                if ep.id == ep_id:
                    ep.in_pool = in_pool
                    # 入池同时指定组（2026-08-29 分组池）：?groups=main,bg 形式
                    if in_pool and groups is not None:
                        sanitized = self._sanitize_groups(groups)
                        ep.pool_groups = sanitized or [self.MAIN_GROUP]
                    elif in_pool and not ep.pool_groups:
                        ep.pool_groups = [self.MAIN_GROUP]
                    # 出池清组绑定，避免残留（全量出池路径；单组移除走 remove_from_group）
                    if not in_pool:
                        ep.pool_groups = []
                        self._clear_pointers_for(ep_id)
                    # 手动移出池再移回 = 显式信任该端点 → 清除延迟回迁状态，恢复最高优先级
                    if in_pool:
                        ep._defer_until = 0
                    break
            self._renumber_pool_priorities()

    def remove_from_group(self, ep_id, group):
        """组感知移除（2026-08-29）：仅从指定组移出端点，其他组不动；
        移除的是最后一个组时整体出池并清指针。"""
        with self._lock:
            for ep in self._endpoints:
                if ep.id != ep_id or not ep.in_pool:
                    continue
                remaining = [g for g in self._ep_groups(ep) if g != group]
                if remaining:
                    ep.pool_groups = remaining
                    # 该组粘性指针若指向本端点 → 清除（其他组指针不动）
                    if self._current_endpoint_by_group.get(group) == ep_id:
                        self._set_current(group, None)
                    if self._manual_override_by_group.get(group) == ep_id:
                        self._set_manual(group, None)
                    sys_log(f"端点 '{ep.name}' 已从组 '{group}' 移除（仍属其他组）", "INFO")
                else:
                    # 最后一组 → 整体出池
                    ep.in_pool = False
                    ep.pool_groups = []
                    self._clear_pointers_for(ep_id)
                    sys_log(f"端点 '{ep.name}' 从组 '{group}' 移除后无剩余组，已整体移出聚合池", "INFO")
                break
            self._renumber_pool_priorities()

    def _clear_pointers_for(self, ep_id):
        """端点整体出池/删除时清理所有组的指针与在途登记。"""
        for grp in list(self._current_endpoint_by_group):
            if self._current_endpoint_by_group.get(grp) == ep_id:
                self._set_current(grp, None)
        for grp in list(self._manual_override_by_group):
            if self._manual_override_by_group.get(grp) == ep_id:
                self._set_manual(grp, None)
        for grp in list(self._persisted_endpoint_by_group):
            if self._persisted_endpoint_by_group.get(grp) == ep_id:
                self._set_persisted(grp, None)
        self._inflight_owner.pop(ep_id, None)

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
                    if updates.get("base_url") is not None or updates.get("api_key") is not None:
                        ep.site_id = self._resolve_site_id(ep.base_url, ep.api_key, exclude_ep_id=ep.id)
                    # pool_groups 归一化（分组池 2026-08-29）：去重/去空/空列表回退 main
                    if updates.get("pool_groups") is not None:
                        sanitized = self._sanitize_groups(ep.pool_groups)
                        ep.pool_groups = sanitized or [self.MAIN_GROUP]
                    if updates.get("max_retries") is not None:
                        ep.max_retries = self._normalize_max_retries(updates["max_retries"])
                    if updates.get("strict400_retries") is not None:
                        ep.strict400_retries = self._normalize_strict400_retries(updates["strict400_retries"])
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

    # ── 客户端伪装 profile 管理（2026-09-10）──

    def list_client_profiles(self):
        """列表：配置文件里的全部 profile（均可编辑/删除）。"""
        out = []
        for name, entry in (self._client_profiles or {}).items():
            if not isinstance(entry, dict):
                continue
            out.append({"name": name, "builtin": False, "headers": entry.get("headers", {})})
        return out

    def save_client_profile(self, name, headers):
        """新建/覆盖 profile。保留头过滤。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("profile 名称不能为空")
        clean, dropped = _sanitize_profile_headers(headers)
        if dropped:
            sys_log(f"客户端伪装：profile '{name}' 提交的保留头 {dropped} 已过滤", "WARN")
        with self._lock:
            self._client_profiles[name] = {"headers": clean}
        sys_log(f"客户端伪装：profile '{name}' 已保存（{len(clean)} 头）", "INFO")
        return name

    def delete_client_profile(self, name):
        """删除 profile；被引用端点在解析时自动降级为透传（WARN）。"""
        name = (name or "").strip()
        with self._lock:
            if not self._client_profiles or name not in self._client_profiles:
                raise ValueError(f"profile '{name}' 不存在")
            self._client_profiles.pop(name, None)
        sys_log(f"客户端伪装：profile '{name}' 已删除", "INFO")
        return name

    def fetch_endpoint_models(self, ep_id):
        """使用指定端点自身的连接配置读取上游模型目录。"""
        with self._lock:
            ep = next((item for item in self._endpoints if item.id == ep_id), None)
            if ep is None:
                raise KeyError("端点不存在")
            connection = {
                "base_url": ep.base_url,
                "api_key": ep.api_key,
                "timeout": min(max(int(ep.timeout or 10), 1), 30),
                "use_proxy": ep.use_proxy,
                "protocol": ep.protocol,
                "default_headers": dict(ep.default_headers or {}),
                "extra_headers": dict(ep.extra_headers or {}),
                "client_profile": ep.client_profile,
            }
        return self.fetch_models(**connection)

    def replace_group_model(self, group, source_ep_id, model):
        """原子地把组内一个端点替换为同站点的另一模型端点。

        优先复用已有的 site_name+model+protocol 端点；没有时从源端点复制全部
        配置字段，仅生成新 id/name 并改 model。运行态字段不会被复制。
        """
        import uuid

        group = str(group or "").strip()
        model = str(model or "").strip()
        if not group or not model:
            raise ValueError("需要 group、endpoint_id 和 model")

        with self._lock:
            if group not in self._all_group_names():
                raise KeyError("分组不存在")

            source = next((ep for ep in self._endpoints if ep.id == source_ep_id), None)
            if source is None:
                raise KeyError("源端点不存在")
            if not source.in_pool or group not in self._ep_groups(source):
                raise ValueError(f"源端点不属于组 '{group}'")
            if not str(source.site_name or "").strip():
                raise ValueError("源端点缺少 site_name，请先完成站点标识迁移")
            if source.model == model:
                raise ValueError("目标模型与当前模型相同")

            source_priority = self._ep_priority(source, group)
            now = time.time()

            def candidate_rank(item):
                ep, config_index = item
                exact_connection = (
                    ep.base_url == source.base_url
                    and ep.api_key == source.api_key
                    and (ep.default_headers or {}) == (source.default_headers or {})
                    and (ep.extra_headers or {}) == (source.extra_headers or {})
                    and ep.use_proxy == source.use_proxy
                )
                noncooldown = ep._cooldown_until <= now and not ep._manual_unlock_required
                return (
                    0 if exact_connection else 1,
                    0 if ep.enabled else 1,
                    0 if noncooldown else 1,
                    self._ep_priority(ep, group),
                    config_index,
                )

            matches = [
                (ep, index) for index, ep in enumerate(self._endpoints)
                if ep.id != source.id
                and ep.site_name == source.site_name
                and ep.model == model
                and ep.protocol == source.protocol
            ]
            created = not matches
            if matches:
                replacement = min(matches, key=candidate_rank)[0]
            else:
                config = {
                    field_name: _copy.deepcopy(getattr(source, field_name))
                    for field_name in Endpoint.__dataclass_fields__
                    if not field_name.startswith("_")
                }
                config["id"] = str(uuid.uuid4())
                config["model"] = model
                config["in_pool"] = True
                config["pool_groups"] = [group]
                base_name = f"{source.site_name}-{model}".strip("-") or model
                used_names = {ep.name for ep in self._endpoints}
                name = base_name
                suffix = 2
                while name in used_names:
                    name = f"{base_name}-{suffix}"
                    suffix += 1
                config["name"] = name
                replacement = Endpoint(**config)
                self._endpoints.append(replacement)

            replacement_groups = list(self._ep_groups(replacement)) if replacement.in_pool else []
            if group not in replacement_groups:
                replacement_groups.append(group)
            replacement.pool_groups = replacement_groups
            replacement.in_pool = True

            remaining = [g for g in self._ep_groups(source) if g != group]
            if remaining:
                source.pool_groups = remaining
            else:
                source.in_pool = False
                source.pool_groups = [self.MAIN_GROUP]

            # 先重新编号，再恢复源端点在当前组的位置，避免影响同组其他成员次序。
            self._renumber_pool_priorities()
            self.set_group_priority(replacement.id, group, source_priority)
            pointer_moved = False
            for getter, setter in (
                (self._get_current, self._set_current),
                (self._get_manual, self._set_manual),
                (self._get_persisted, self._set_persisted),
            ):
                if getter(group) == source.id:
                    setter(group, replacement.id)
                    pointer_moved = True
            self._endpoints.sort(key=lambda ep: ep.priority)
            return replacement, created, pointer_moved

    def _renumber_pool_priorities(self):
        """对每组内端点分配该组独立的连续优先级（2026-08-29 分组隔离）。

        每组独立 1..N：跨组端点在不同组可有不同排序；main 组值镜像到全局
        priority 字段（兼容旧 config 格式与 prio99 兜底语义）。
        """
        for grp in self._all_group_names():
            group_eps = sorted(
                (ep for ep in self._endpoints if ep.in_pool and grp in self._ep_groups(ep)),
                key=lambda e: self._ep_priority(e, grp),
            )
            for i, ep in enumerate(group_eps):
                self._set_ep_priority(ep, grp, i + 1)

    def set_group_priority(self, ep_id, group, new_priority):
        """组内优先级 insert-at-position（2026-08-29 分组隔离）：只影响该组。"""
        with self._lock:
            for ep in self._endpoints:
                if ep.id != ep_id or not ep.in_pool:
                    continue
                members = sorted(
                    (e for e in self._endpoints if e.in_pool and group in self._ep_groups(e)),
                    key=lambda e: self._ep_priority(e, group),
                )
                old = self._ep_priority(ep, group)
                new_priority = max(1, min(int(new_priority), len(members)))
                self._set_ep_priority(ep, group, new_priority)
                for other in members:
                    if other is ep:
                        continue
                    op = self._ep_priority(other, group)
                    if new_priority < old and new_priority <= op < old:
                        self._set_ep_priority(other, group, op + 1)
                    elif old < op <= new_priority:
                        self._set_ep_priority(other, group, op - 1)
                break
            self._renumber_pool_priorities()
            self._endpoints.sort(key=lambda e: e.priority)

    # ══════════════ 分组池路由基础设施（2026-08-29 spec）══════════════

    MAIN_GROUP = "main"
    # 图片解析池（2026-09-12）：与 main 同级——全池唯一、系统固定存在，无「用途」配置项；
    # 名称/类型/选择器锁定且不可删除。图片转译候选恒取本组成员。
    VISION_GROUP = "vision"
    VISION_SELECTOR = "api-pool-vision"  # 该组对外选择器（Hermes 侧 model 名），固定
    # 组级上下文长度（2026-09-12，纯显式声明）：组声明的对外窗口（tokens，0=不声明），
        # 取值只认组实体 context_tokens，不做成员推导；
    # 2026-09-13 起单位由 K 改为 tokens 精确值（适配各家模型窗口差异，如 200000 / 202752 / 1048576），
    # 旧配置的 context_k（K=1000 tokens）读入时自动换算。
    # 经 /v1/models 的 context_length 暴露给 Hermes（其解析链第 4/5 级）。
    # 边界来自 Hermes `_coerce_reasonable_int`（1024–10,000,000 tokens）：越界值会被它忽略。
    GROUP_CONTEXT_MIN_TOKENS = 2000
    GROUP_CONTEXT_MAX_TOKENS = 10000000
    # 池自身发起的探活/自检请求的输出上限（2026-09-13）：Responses API 要求 max_output_tokens ≥ 16，
    # 原先的 3 / 5 / 10 会让 protocol=responses 端点的探活必然 400 并被误标 bad
    # （探活实际只输出 1-2 token，成本无实质变化）。
    PROBE_MAX_TOKENS = 16

    @classmethod
    def _endpoint_log_label(cls, ep, group=None, model=None):
        """池内端点日志统一标识：[池名]端点名[: 模型名]。"""
        label = f"[{group or cls.MAIN_GROUP}]{ep.name}"
        return f"{label}: {model}" if model else label

    def _sanitize_groups(self, raw):
        """候选组名列表归一化：去重保序、剔除空串、非法类型忽略。"""
        if not isinstance(raw, list):
            return [self.MAIN_GROUP]
        seen = []
        for g in raw:
            gs = str(g).strip()
            if gs and gs not in seen:
                seen.append(gs)
        return seen or [self.MAIN_GROUP]

    def _ep_groups(self, ep):
        """端点归属组（运行时归一化，兼容字段缺失/空列表）。"""
        groups = getattr(ep, "pool_groups", None)
        return groups if groups else ([self.MAIN_GROUP] if ep.in_pool else [])

    # ── 组实体管理（2026-08-30 组管理功能）──
    _GROUP_NAME_RE = None  # 延迟初始化（模块顶部 import re）

    def _valid_group_name(self, name):
        """组名校验：非空、≤32 字符、字母数字/连字符/点号/下划线/中文。"""
        if not isinstance(name, str):
            return False
        n = name.strip()
        return bool(n) and len(n) <= 32 and bool(re.fullmatch(r"[\w\u4e00-\u9fff.-]+", n))

    def _valid_group_type(self, gtype):
        return gtype in ("mixed", "dedicated")

    def _valid_group_context_tokens(self, value):
        """组上下文长度（tokens）归一化：0=未声明；非法/越界 → None（调用方拒绝）。"""
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            return None
        if n == 0:
            return 0
        if self.GROUP_CONTEXT_MIN_TOKENS <= n <= self.GROUP_CONTEXT_MAX_TOKENS:
            return n
        return None

    def _group_context_tokens(self, group):
        """该组显式声明的上下文窗口（tokens）；未声明或存值非法 → None（不对外声明该键）。"""
        return self._valid_group_context_tokens(self._group_defs.get(group, {}).get("context_tokens")) or None

    def _loaded_group_context_tokens(self, d):
        """配置读入：context_tokens 优先；旧 context_k（K=1000 tokens）自动换算（2026-09-13）。"""
        raw = d.get("context_tokens")
        if raw is None:
            try:
                raw = int(d.get("context_k") or 0) * 1000
            except (TypeError, ValueError):
                return 0
        return self._valid_group_context_tokens(raw) or 0

    def _set_group_context_tokens(self, group, tokens):
        """写入/清除组的显式 context_tokens，返回是否有变更。"""
        gd = self._group_defs.get(group)
        if gd is None:
            return False
        if tokens:
            if gd.get("context_tokens") == tokens:
                return False
            gd["context_tokens"] = tokens
        else:
            if gd.pop("context_tokens", None) is None:
                return False
        sys_log(f"组 '{group}' 上下文长度声明为 {'%s tokens' % format(tokens, ',') if tokens else '取消声明'}", "INFO")
        return True

    def _group_selector(self, name):
        """组选择器（Hermes 侧 model 名）：有实体定义取其 model，否则回退组名。"""
        gd = self._group_defs.get(name)
        return gd.get("model") if gd and gd.get("model") else name

    def _dedicated_model(self, group):
        """dedicated 组绑定的真实模型名；mixed 组返回 None。"""
        gd = self._group_defs.get(group)
        if gd and gd.get("type") == "dedicated":
            return gd.get("model") or None
        return None

    def _load_group_defs(self, defs_raw):
        """启动/配置加载：归一化组实体定义（外部传入 list[dict]）。

        系统内置组 main（首位）/ VISION_GROUP（次位）恒在，配置里其余组按配置顺序追加；
        旧配置遗留的 role 键一律忽略（2026-09-12 起图片解析池不再由「用途」指定）。
        """
        self._group_defs = {
            self.MAIN_GROUP: {"type": "mixed", "model": "api-pool"},
            self.VISION_GROUP: {"type": "mixed", "model": self.VISION_SELECTOR},
        }
        if isinstance(defs_raw, list):
            for d in defs_raw:
                if not isinstance(d, dict):
                    continue
                name = str(d.get("name", "")).strip()
                gtype = d.get("type", "mixed")
                model = str(d.get("model", "")).strip()
                if not name or not self._valid_group_type(gtype):
                    continue
                if name == self.MAIN_GROUP:
                    # main 类型/选择器恒内置；组级上下文长度声明随配置恢复
                    ck = self._loaded_group_context_tokens(d)
                    if ck:
                        self._group_defs[self.MAIN_GROUP]["context_tokens"] = ck
                    continue
                if name == self.VISION_GROUP:
                    # 内置图片解析池：类型恒 mixed，选择器沿用配置（缺省 VISION_SELECTOR）
                    entry = {"type": "mixed", "model": model or self.VISION_SELECTOR}
                    ck = self._loaded_group_context_tokens(d)
                    if ck:
                        entry["context_tokens"] = ck
                    self._group_defs[name] = entry
                    continue
                if str(d.get("role", "") or "").strip():
                    sys_log(f"忽略组 '{name}' 的 role 键（图片解析池固定为 '{self.VISION_GROUP}'）", "WARNING")
                entry = {"type": gtype, "model": model or name}
                ck = self._loaded_group_context_tokens(d)
                if ck:
                    entry["context_tokens"] = ck
                self._group_defs[name] = entry
        return self._group_defs

    def _derive_group_defs(self):
        """旧配置（无 pool_group_defs）派生组实体：selector=组名，type=mixed。
        仅内存态，不落盘；首次组编辑后 _sync_to_config 才写入 pool_group_defs。"""
        for grp in self._all_group_names():
            if grp != self.MAIN_GROUP and grp not in self._group_defs:
                self._group_defs[grp] = {"type": "mixed", "model": grp}
        return self._group_defs

    def create_group(self, name, gtype="mixed", model="", context_tokens=0):
        """新建分组。返回 (ok, message)。main / 图片解析池为系统内置组，不可由此创建。"""
        with self._lock:
            name = str(name or "").strip()
            if not self._valid_group_name(name):
                return False, "组名非法（非空、≤32字符、字母数字/连字符/点号/下划线/中文）"
            if name in (self.MAIN_GROUP, self.VISION_GROUP, "api-pool"):
                return False, f"组名 '{name}' 为保留名"
            if name in self._group_defs or name in self._all_group_names():
                return False, f"组 '{name}' 已存在"
            if not self._valid_group_type(gtype):
                return False, "分组类型必须为 mixed 或 dedicated"
            ck = self._valid_group_context_tokens(context_tokens)
            if ck is None:
                return False, ("上下文长度非法（0=不声明，或 "
                               f"{self.GROUP_CONTEXT_MIN_TOKENS:,}–{self.GROUP_CONTEXT_MAX_TOKENS:,} tokens）")
            model = str(model or "").strip()
            if gtype == "dedicated":
                if not model:
                    return False, "专用分组必须绑定模型"
                # selector 与其他组冲突检查（选择器是路由键，必须全池唯一）
                for g, gd in self._group_defs.items():
                    if g != name and gd.get("model") == model:
                        return False, f"选择器 '{model}' 已被组 '{g}' 使用"
            else:
                model = model or name  # mixed 缺省选择器=组名（向后兼容派生态）
                for g, gd in self._group_defs.items():
                    if g != name and gd.get("model") == model:
                        return False, f"选择器 '{model}' 已被组 '{g}' 使用"
            self._group_defs[name] = {"type": gtype, "model": model}
            if ck:
                self._group_defs[name]["context_tokens"] = ck
            sys_log(f"新建分组 '{name}'（{gtype}，选择器 {model}，上下文 {'%s tokens' % format(ck, ',') if ck else '未声明'}）", "INFO")
            return True, name

    def update_group(self, name, updates):
        """编辑分组（名称/类型/绑定模型）。返回 (ok, message)。
        - main：仅允许改选择器 model（名称/类型锁定）
        - 改类型 mixed→dedicated / dedicated→mixed：不校验成员模型（入池规范由人工遵循）
        - 改名：同步端点 pool_groups / 指针态 / defs 键
        """
        with self._lock:
            name = str(name or "").strip()
            if name not in self._group_defs and name not in self._all_group_names():
                return False, f"组 '{name}' 不存在"
            if name not in self._group_defs:
                # 指针态残留组（无实体）：补一个派生实体再编辑
                self._group_defs[name] = {"type": "mixed", "model": name}
            old = dict(self._group_defs[name])
            new_name = str(updates.get("name", name)).strip() or name
            new_type = updates.get("type", old["type"])
            new_model = str(updates.get("model", old.get("model", "")) or "").strip()
            new_ck = self._valid_group_context_tokens(updates.get("context_tokens", old.get("context_tokens", 0)))
            if new_ck is None:
                return False, ("上下文长度非法（0=不声明，或 "
                               f"{self.GROUP_CONTEXT_MIN_TOKENS:,}–{self.GROUP_CONTEXT_MAX_TOKENS:,} tokens）")

            if not self._valid_group_type(new_type):
                return False, "分组类型必须为 mixed 或 dedicated"
            if name == self.MAIN_GROUP:
                if new_name != self.MAIN_GROUP or new_type != "mixed":
                    return False, "main 组名称与类型不可修改（仅可改选择器）"
                if new_model and new_model != "api-pool":
                    # main 选择器改名会让存量 Hermes 配置失配，禁止
                    return False, "main 组选择器固定为 api-pool（历史别名）"
                # 上下文长度不属于锁定项：main 是对外默认窗口，必须可改
                if self._set_group_context_tokens(name, new_ck):
                    return True, name
                return True, "无变更"

            if name == self.VISION_GROUP:
                # 系统内置图片解析池：与 main 同级，名称/类型/选择器全锁
                if new_name != self.VISION_GROUP or new_type != "mixed" or new_model != old.get("model", ""):
                    return False, "图片解析池为系统内置组：名称/类型/选择器均锁定"
                if self._set_group_context_tokens(name, new_ck):
                    return True, name
                return True, "无变更"

            if new_name != name:
                if not self._valid_group_name(new_name):
                    return False, "组名非法（非空、≤32字符、字母数字/连字符/点号/下划线/中文）"
                if new_name in (self.MAIN_GROUP, self.VISION_GROUP, "api-pool"):
                    return False, f"组名 '{new_name}' 为保留名"
                if new_name in self._group_defs or new_name in self._all_group_names():
                    return False, f"组 '{new_name}' 已存在"

            if new_type == "dedicated" and not new_model:
                return False, "专用分组必须绑定模型"

            # 选择器跟随规则（2026-08-30）：改名时，若旧选择器=旧组名（派生态）且未显式
            # 指定新选择器 → 跟随新组名（保持"缺省选择器=组名"语义）；显式设置的选择器不动。
            if new_name != name and new_type == "mixed" \
                    and old.get("model") == name and not updates.get("model"):
                new_model = ""

            # 选择器唯一性（mixed 缺省=新组名）
            eff_model = new_model or (new_name if new_type == "mixed" else "")
            for g, gd in self._group_defs.items():
                if g != name and gd.get("model") == eff_model:
                    return False, f"选择器 '{eff_model}' 已被组 '{g}' 使用"

            # 应用改名：端点 pool_groups / 指针态 / fallback 锁 / defs
            if new_name != name:
                for ep in self._endpoints:
                    if name in self._ep_groups(ep):
                        ep.pool_groups = [new_name if g == name else g for g in self._ep_groups(ep)]
                for state in (self._current_endpoint_by_group, self._manual_override_by_group,
                              self._persisted_endpoint_by_group):
                    if name in state:
                        state[new_name] = state.pop(name)
                if name in self._fallback_lock_until_by_group:
                    self._fallback_lock_until_by_group[new_name] = self._fallback_lock_until_by_group.pop(name)
                if name in self._group_fallback_lock_until:
                    self._group_fallback_lock_until[new_name] = self._group_fallback_lock_until.pop(name)
                if name in self._group_fallback_pending:
                    self._group_fallback_pending[new_name] = self._group_fallback_pending.pop(name)
                del self._group_defs[name]

            self._group_defs[new_name] = {"type": new_type, "model": eff_model}
            if new_ck:
                self._group_defs[new_name]["context_tokens"] = new_ck
            sys_log(f"更新分组 '{name}'→'{new_name}'（{new_type}，选择器 {eff_model}，上下文 {'%s tokens' % format(new_ck, ',') if new_ck else '未声明'}）", "INFO")
            return True, new_name

    def delete_group(self, name):
        """删除分组：成员移出该组（最后一组→整体出池），清理指针与计数。
        main / 图片解析池为系统内置组，不可删。返回 (ok, message)。"""
        with self._lock:
            name = str(name or "").strip()
            if name in (self.MAIN_GROUP, self.VISION_GROUP):
                return False, f"'{name}' 为系统内置组，不可删除"
            if name not in self._group_defs and name not in self._all_group_names():
                return False, f"组 '{name}' 不存在"
            # 逐成员移出（复用组感知移除语义）
            for ep in list(self._endpoints):
                if ep.in_pool and name in self._ep_groups(ep):
                    self.remove_from_group(ep.id, name)
            self._group_defs.pop(name, None)
            for state in (self._current_endpoint_by_group, self._manual_override_by_group,
                          self._persisted_endpoint_by_group, self._fallback_lock_until_by_group):
                state.pop(name, None)
            self._group_fallback_lock_until.pop(name, None)
            self._group_fallback_pending.pop(name, None)
            sys_log(f"删除分组 '{name}'（成员已移出）", "INFO")
            return True, "deleted"

    # ── per-group 优先级访问器（2026-08-29 分组隔离）──
    def _ep_priority(self, ep, group):
        """端点在指定组的优先级：优先 priority_by_group，缺失时回退全局 priority。"""
        pbg = getattr(ep, "priority_by_group", None) or {}
        return pbg.get(group, ep.priority)

    def _set_ep_priority(self, ep, group, value):
        if not hasattr(ep, "priority_by_group") or ep.priority_by_group is None:
            ep.priority_by_group = {}
        ep.priority_by_group[group] = value
        # main 组值镜像到全局 priority 字段（旧格式兼容：config、探活日志、prio99 兜底语义）
        if group == self.MAIN_GROUP:
            ep.priority = value

    def _resolve_request_group(self, model):
        """请求 model 字段 → 路由组名（薄封装，唯一解析点）。

        - None/空 → main
        - "api-pool"（历史别名）→ main
        - 优先匹配组选择器（_group_defs 的 model 字段：mixed=Hermes 配置名，dedicated=真实模型名）
        - 精确匹配已知组名（端点声明 ∪ 指针态出现过的组；bg 组全挂时仍可解析到 bg，
          从而正确触发 bg→main fallback 而不是静默落 main）→ 该组
        - 其他 → main（存量流量零感知）
        """
        if not model:
            return self.MAIN_GROUP
        name = str(model).strip()
        if not name or name == "api-pool":
            return self.MAIN_GROUP
        # selector 精确匹配（组实体定义优先于组名）
        for grp, gd in self._group_defs.items():
            if gd.get("model") and gd["model"] == name:
                return grp
        if name in self._all_group_names():
            return name
        return self.MAIN_GROUP

    def _migrate_legacy_state(self):
        """启动时把旧扁平路由状态迁移为 main 组状态（一次性）。"""
        legacy_current = getattr(self, "_legacy_current_endpoint_id", None)
        legacy_manual = getattr(self, "_legacy_manual_override_id", None)
        if legacy_current or legacy_manual:
            self._current_endpoint_by_group[self.MAIN_GROUP] = legacy_current
            self._manual_override_by_group[self.MAIN_GROUP] = legacy_manual

    # ── 旧扁平属性兼容层：映射到 main 组（测试与外部脚本零改动迁移）──
    @property
    def _current_endpoint_id(self):
        return self._current_endpoint_by_group.get(self.MAIN_GROUP)

    @_current_endpoint_id.setter
    def _current_endpoint_id(self, ep_id):
        self._set_current(self.MAIN_GROUP, ep_id)

    @property
    def _manual_override_id(self):
        return self._manual_override_by_group.get(self.MAIN_GROUP)

    @_manual_override_id.setter
    def _manual_override_id(self, ep_id):
        self._set_manual(self.MAIN_GROUP, ep_id)

    @property
    def _persisted_endpoint_id(self):
        return self._persisted_endpoint_by_group.get(self.MAIN_GROUP)

    @_persisted_endpoint_id.setter
    def _persisted_endpoint_id(self, ep_id):
        self._set_persisted(self.MAIN_GROUP, ep_id)

    def _get_current(self, group):
        return self._current_endpoint_by_group.get(group)

    def _set_current(self, group, ep_id):
        previous = self._current_endpoint_by_group.get(group)
        if ep_id is None:
            self._current_endpoint_by_group.pop(group, None)
        else:
            self._current_endpoint_by_group[group] = ep_id
        if previous != ep_id:
            self._route_epoch_by_group[group] = self._route_epoch_by_group.get(group, 0) + 1

    def _get_manual(self, group):
        return self._manual_override_by_group.get(group)

    def _set_manual(self, group, ep_id):
        previous = self._manual_override_by_group.get(group)
        if ep_id is None:
            self._manual_override_by_group.pop(group, None)
        else:
            self._manual_override_by_group[group] = ep_id
        if previous != ep_id:
            self._route_epoch_by_group[group] = self._route_epoch_by_group.get(group, 0) + 1

    def _get_route_epoch(self, group):
        return self._route_epoch_by_group.get(group, 0)

    def _get_persisted(self, group):
        return self._persisted_endpoint_by_group.get(group)

    def _set_persisted(self, group, ep_id):
        if ep_id is None:
            self._persisted_endpoint_by_group.pop(group, None)
        else:
            self._persisted_endpoint_by_group[group] = ep_id

    def _is_fallback_locked_group(self, group):
        return self._fallback_lock_until_by_group.get(group, 0) > time.time()

    def _should_reset_cached_stats(self, group, site_id):
        """本组上次成功 usage 属于不同站点账户时，首条统计按冷启动处理。"""
        previous = self._cache_stats_site_id_by_group.get(group)
        return previous is not None and previous != site_id

    def _mark_cache_stats_account(self, group, site_id):
        """usage 成功落库后更新本组统计账户边界。"""
        with self._lock:
            self._cache_stats_site_id_by_group[group] = site_id

    def _is_ep_inflight_elsewhere(self, ep, group):
        """子组仅受 main 在途占用约束；main 不受任何子组约束。"""
        if group == self.MAIN_GROUP or self._group_defs.get(group, {}).get("type") == "dedicated":
            return False
        owners = self._inflight_owner.get(ep.id) or {}
        return owners.get(self.MAIN_GROUP, 0) > 0

    def _acquire_inflight(self, ep_id, group):
        owners = self._inflight_owner.setdefault(ep_id, {})
        owners[group] = owners.get(group, 0) + 1

    def _release_inflight(self, ep_id, group):
        """按组递减在途计数，保留其他组及本组剩余并发请求。"""
        owners = self._inflight_owner.get(ep_id)
        if not owners or group not in owners:
            return
        if owners[group] > 1:
            owners[group] -= 1
        else:
            owners.pop(group, None)
        if not owners:
            self._inflight_owner.pop(ep_id, None)

    def _wrap_stream_release(self, gen, ep_id, group):
        """流式响应外层 generator：正常结束/异常/提前关闭时释放在途占用。

        泄漏兜底：若消费方从不迭代也不 close（罕见），10 分钟无活动的清零
        逻辑（_cleanup_expired_cooldowns 内）会兜底。GeneratorExit 由 close() 触发，
        也是合法释放路径。
        """
        try:
            yield from gen
        finally:
            with self._lock:
                self._release_inflight(ep_id, group)

    def _is_ep_sticky_elsewhere(self, ep, group):
        """子组仅避让 main 粘性/手动端点；main 不受子组指针约束。"""
        if group == self.MAIN_GROUP or self._group_defs.get(group, {}).get("type") == "dedicated":
            return False
        return ep.id in {
            self._get_current(self.MAIN_GROUP),
            self._get_manual(self.MAIN_GROUP),
        }

    def _group_sticky_candidates(self, group):
        """组内可用端点：main 无跨组约束；子组避让 main 粘性与在途端点。"""
        base = [ep for ep in self._endpoints if ep.enabled and ep.in_pool
                and group in self._ep_groups(ep)
                and not ep._manual_unlock_required
                and not self._is_in_cooldown(ep)
]
        if not base:
            return [], False
        if group == self.MAIN_GROUP:
            return base, False
        free = [ep for ep in base
                if not self._is_ep_sticky_elsewhere(ep, group)
                and not self._is_ep_inflight_elsewhere(ep, group)]
        if free:
            return free, False
        return [], False

    def _vision_pool_candidates(self):
        """图片解析的候选来源 = 系统内置图片解析池（VISION_GROUP）的可用端点。

        返回 (候选列表, 视觉池组名)：池无可用成员（禁用/未入池/手动锁/冷却）→ ([], 组名)。
        候选语义与普通请求一致（_group_sticky_candidates：enabled + in_pool + 非锁 + 非冷却）。
        """
        vision_group = self.VISION_GROUP
        candidates, _ = self._group_sticky_candidates(vision_group)
        return candidates, vision_group

    def _reset_endpoint_assertion_state(self, ep, clear_defer=True):
        """用户断言（「这个端点现在可用」，例如刚续费、上游已恢复）优先于 API Pool
        上一次观测推导出的不健康状态：把端点视为恢复健康，由随后的真实结果重新分类
        （请求失败会走既有故障路径重新冷却，代价是一次请求；反过来让观测推论压掉
        用户指令，会让操作变成静默空操作——候选集先按状态过滤，指针根本读不到）。
        enabled/in_pool 是配置声明而非健康推论，仍由调用方守卫。

        clear_defer=False 用于组级「立即切回」：defer 是当前工作对象的缓存保护，
        不属于失败态，且一个端点可属多组，清掉会误伤别组正在工作的工作对象。"""
        if clear_defer:
            ep._defer_until = 0
        ep._cooldown_until = 0
        ep._cooldown_reason = ""
        ep._fail_count = 0
        ep._last_error = ""
        ep._last_error_ts = 0
        ep._manual_unlock_required = False
        ep._health_error = ""
        # 未经真实请求验证 → unknown；首次成功由 _on_success 置 ok。
        ep._health = "unknown"

    def switch_to_endpoint(self, ep_id, group=None):
        with self._lock:
            for ep in self._endpoints:
                if ep.id == ep_id:
                    grp = str(group or self.MAIN_GROUP).strip() or self.MAIN_GROUP
                    if not ep.enabled or not ep.in_pool:
                        return False
                    if group is not None and grp not in self._ep_groups(ep):
                        return False
                    self._reset_endpoint_assertion_state(ep)
                    grp = group or self.MAIN_GROUP
                    self._set_manual(grp, ep_id)
                    self._set_current(grp, ep_id)
                    sys_log(f"手动切换端点: '{self._endpoint_log_label(ep, grp)}'（按用户断言重置为待验证：清除冷却/上游配额/余额冻结与失败计数）", "INFO")
                    return True
        return False

    def clear_group_fallback(self, group):
        """手动「立即切回」（UI 点击 ↩main / ⏸待回切 徽标）：用户断言优先于观测推论。

        动作 = ①清该组回切锁（滑动空闲窗口立即期满）+ ②清该组端点的冷却/冻结/失败态，
        于是下一个请求真正落回本组（否则锁一清但端点仍冷全挂，会立刻重新 fallback，
        点击等于空操作）。端点 defer 不动——那是当前工作对象的缓存保护，非失败态。
        未清锁时也执行 ②，覆盖「锁已期满、待回切」阶段的点击。
        返回 {"lock_cleared": bool, "endpoints_reset": int}，由调用方落盘。"""
        with self._lock:
            lock_cleared = self._group_fallback_lock_until.pop(group, None) is not None
            self._group_fallback_pending.pop(group, None)
            members = [ep for ep in self._endpoints
                       if ep.enabled and ep.in_pool and group in self._ep_groups(ep)]
            for ep in members:
                self._reset_endpoint_assertion_state(ep, clear_defer=False)
        if lock_cleared or members:
            sys_log(
                f"手动立即切回组 '{group}'（清回切锁={'是' if lock_cleared else '否'}，"
                f"{len(members)} 个端点按用户断言重置为待验证）",
                "INFO",
            )
        return {"lock_cleared": lock_cleared, "endpoints_reset": len(members)}

    def get_endpoint(self, ep_id):
        """按 ID 返回主池中的真实端点对象，用于定向管理操作。"""
        with self._lock:
            return next((ep for ep in self._endpoints if ep.id == ep_id), None)

    def list_endpoints(self):
        self._cleanup_expired_cooldowns()
        now = time.time()
        with self._lock:
            current_by_group = {
                grp: (self._manual_override_by_group.get(grp) or self._current_endpoint_by_group.get(grp))
                for grp in self._all_group_names()
            }
            current_ids = set(current_by_group.values())
            return [
                self._ep_to_dict(ep, ep.id in current_ids, now, ep.id in current_ids)
                for ep in self._endpoints
            ]

    def _all_group_names(self):
        """全部已知组名：实体定义 ∪ 端点声明 ∪ 指针态，包含零成员新组。"""
        names = list(self._group_defs)
        for ep in self._endpoints:
            for g in self._ep_groups(ep):
                if g not in names:
                    names.append(g)
        for state in (self._current_endpoint_by_group, self._manual_override_by_group,
                      self._persisted_endpoint_by_group):
            for g in state:
                if g not in names:
                    names.append(g)
        if self.MAIN_GROUP not in names:
            names.insert(0, self.MAIN_GROUP)
        return names

    def _groups_pointing_at(self, ep_id):
        """哪些组的粘性/手动指针正指向该端点（UI 展示 + 互斥透明度）。"""
        groups = []
        for state in (self._current_endpoint_by_group, self._manual_override_by_group):
            for grp, eid in state.items():
                if eid == ep_id and grp not in groups:
                    groups.append(grp)
        return groups

    def _ep_to_dict(self, ep, is_current, now, in_flight=False):
        return {
            "id": ep.id,
            "name": ep.name,
            "site_name": ep.site_name,
            "site_id": ep.site_id,
            "base_url": ep.base_url,
            "api_key": ep.api_key[:8] + "***" if len(ep.api_key) > 8 else "***",
            "api_key_full": ep.api_key,
            "model": ep.model,
            "priority": ep.priority,
            "priority_by_group": dict(getattr(ep, "priority_by_group", None) or {}),
            "timeout": ep.timeout,
            "max_retries": ep.max_retries,
            "enabled": ep.enabled,
            "cooldown_minutes": ep.cooldown_minutes,
            "use_proxy": ep.use_proxy,
            "protocol": ep.protocol,
            "extra_headers": ep.extra_headers,
            "default_headers": ep.default_headers,
            "client_profile": ep.client_profile,
            "health_mode": ep.health_mode,
            "billing_mode": ep.billing_mode,
            "is_vision": ep.is_vision,
            "in_pool": ep.in_pool,
            "check_fake_success": ep.check_fake_success,
            "tool_call_id_prefix": ep.tool_call_id_prefix,
            "strict400_retries": getattr(ep, "strict400_retries", 2),
            "reasoning_policy": getattr(ep, "reasoning_policy", "auto"),
            "preserved_thinking": getattr(ep, "preserved_thinking", False),
            "stream_first_packet_timeout": ep.stream_first_packet_timeout,
            "stream_stall_timeout": ep.stream_stall_timeout,
            "stream_max_duration": ep.stream_max_duration,
            "pool_groups": list(self._ep_groups(ep)),
            "current_groups": self._groups_pointing_at(ep.id),
            "fail_count": ep._fail_count,
            "last_error": ep._last_error,
            "last_success": ep._last_success_ts,
            "total_calls": ep._total_calls,
            "total_failures": ep._total_failures,
            "is_current": is_current,
            "in_cooldown": ep._cooldown_until > now,
            "cooldown_remaining": max(0, int(ep._cooldown_until - now)),
            "cooldown_until": ep._cooldown_until,
            "cooldown_reason": ep._cooldown_reason,
            "manual_unlock_required": ep._manual_unlock_required,
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
            # The chain is a status view, so keep healthy deferred endpoints
            # visible even though routing excludes them until defer expires.
            active = sorted(
                (ep for ep in self._endpoints if ep.enabled and ep.in_pool),
                key=lambda ep: (self._ep_priority(ep, self.MAIN_GROUP), ep.priority),
            )
            current_by_group = {
                grp: (self._manual_override_by_group.get(grp) or self._current_endpoint_by_group.get(grp))
                for grp in self._all_group_names()
            }
            current_ids = set(current_by_group.values())
            return [
                {
                    "name": ep.name,
                    "model": ep.model,
                    "priority": ep.priority,
                    "priority_by_group": dict(getattr(ep, "priority_by_group", None) or {}),
                    "is_current": ep.id in current_ids,
                    "current_groups": self._groups_pointing_at(ep.id),
                    "pool_groups": list(self._ep_groups(ep)),
                    "fail_count": ep._fail_count,
                    "in_cooldown": ep._cooldown_until > now,
                    "cooldown_remaining": max(0, int(ep._cooldown_until - now)),
                    "cooldown_reason": ep._cooldown_reason,
                    "manual_unlock_required": ep._manual_unlock_required,
                    "deferrable": ep.deferrable,
                    "is_deferred": ep._defer_until > now,
                    "defer_remaining": max(0, int(ep._defer_until - now)),
            "max_context_k": ep.max_context_k,
                    "use_proxy": ep.use_proxy,
                    "is_vision": ep.is_vision,
            "in_pool": ep.in_pool,
                    "health": ep._health,
                    "health_latency_ms": ep._health_latency_ms,
                    "health_error": ep._health_error,
                    "last_error": ep._last_error,
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
                ep._cooldown_reason = ""
                ep._manual_unlock_required = False
                ep._defer_until = 0
            self._current_endpoint_by_group.clear()
            self._manual_override_by_group.clear()

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
                models = self.fetch_models(
                    ep.base_url, ep.api_key, timeout=10, use_proxy=ep.use_proxy,
                    protocol=ep.protocol, default_headers=ep.default_headers,
                    extra_headers=ep.extra_headers, client_profile=ep.client_profile,
                )
                latency = int((time.time() - t0) * 1000)
                if models:
                    return ep.id, "ok", latency, ""
                else:
                    return ep.id, "bad", latency, "获取模型列表失败"
            except PoolIdentityUnavailable:
                return ep.id, "skipped", -1, POOL_IDENTITY_SKIP_REASON
            except Exception as e:
                return ep.id, "bad", int((time.time() - t0) * 1000), f"Models接口错误: {e}"
                
        payload = {"model": ep.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": self.PROBE_MAX_TOKENS, "stream": False}
        
        # Attempt 1
        t0 = time.time()
        try:
            reply, err = self._try_endpoint(ep, payload, timeout=10, log_usage=False, force_no_retry=True, is_probe=True)
        except PoolIdentityUnavailable:
            return ep.id, "skipped", -1, POOL_IDENTITY_SKIP_REASON
        latency = int((time.time() - t0) * 1000)
        
        if reply is not None and latency <= LATENCY_OK_MAX:
            return ep.id, "ok", latency, ""
            
        # Evaluate if we should retry
        err_str = err or ""
        hard_errors = ["auth error", "400", "401", "403", "404", "429"]
        if any(code in err_str for code in hard_errors):
            return ep.id, "bad", latency, err_str
            
        # Attempt 2 (Retry for cold start or transient glitch)
        t1 = time.time()
        try:
            reply2, err2 = self._try_endpoint(ep, payload, timeout=10, log_usage=False, force_no_retry=True, is_probe=True)
        except PoolIdentityUnavailable:
            return ep.id, "skipped", -1, POOL_IDENTITY_SKIP_REASON
        latency2 = int((time.time() - t1) * 1000)
        
        if reply2 is not None and latency2 <= LATENCY_OK_MAX:
            return ep.id, "ok", latency2, ""
            
        # If retry also fails or isn't fast enough, return the original attempt's status
        if reply is not None:
            # A successful response is healthy even when it is slow.  Latency is
            # reported separately; it must not turn a live endpoint into a fault.
            return ep.id, "slow", latency, ""
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

    @staticmethod
    def _normalize_strict400_retries(value):
        """严格校验 400 同端点重试上限：只允许 0/1/2（2026-09-12）。"""
        try:
            return max(0, min(2, int(value)))
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _normalize_max_retries(value):
        try:
            return min(3, max(0, int(value)))
        except (TypeError, ValueError):
            return 1

    _VISION_CACHE_TTL = 300  # 图片转译缓存有效期（秒）
    _VISION_CACHE_MAX = 256  # 缓存条目上限（超出按过期优先、最旧兜底淘汰）

    @staticmethod
    def _vision_cache_key(translation_msgs):
        """图片集合签名：全部 image_url 的有序 md5；无图 → None。"""
        urls = []
        for m in translation_msgs:
            content = m.get("content") if isinstance(m, dict) else None
            if not isinstance(content, list):
                continue
            for c in content:
                if isinstance(c, dict) and c.get("type") == "image_url":
                    u = c.get("image_url")
                    if isinstance(u, dict):
                        u = u.get("url", "")
                    if u:
                        urls.append(str(u))
        if not urls:
            return None
        return hashlib.md5("|".join(urls).encode("utf-8")).hexdigest()

    def _vision_cache_get(self, key):
        if not key:
            return None
        item = self._vision_cache.get(key)
        if not item:
            return None
        ts, desc = item
        if time.time() - ts > self._VISION_CACHE_TTL:
            self._vision_cache.pop(key, None)
            return None
        return desc

    def _vision_cache_set(self, key, desc):
        if not key or not desc:
            return
        if len(self._vision_cache) >= self._VISION_CACHE_MAX:
            now = time.time()
            for k in [k for k, (ts, _) in list(self._vision_cache.items())
                      if now - ts > self._VISION_CACHE_TTL]:
                self._vision_cache.pop(k, None)
            if len(self._vision_cache) >= self._VISION_CACHE_MAX:
                # 全部新鲜：按插入序丢最旧一条（近似 LRU，够用）
                self._vision_cache.pop(next(iter(self._vision_cache)), None)
        self._vision_cache[key] = (time.time(), desc)

    def _translate_images_sync(
        self, messages, active_eps, pool_group=None, request_id=None, request_deadline=None,
    ):
        """图片转译：候选来源 = 系统内置图片解析池（VISION_GROUP），不再从请求组内抓取任意视觉端点。

        降级策略（2026-09-10 拍板 B）：视觉池无可用成员时本函数静默返回原消息，
        图片原样交给目标端点；降级告警由调用方（chat 拦截处）记录一次。
        """
        vision_eps, vision_group = self._vision_pool_candidates()
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

        if not translation_msgs:
            return messages

        cache_key = self._vision_cache_key(translation_msgs)
        description = self._vision_cache_get(cache_key) or ""

        if description:
            sys_log(f"图片解析命中缓存，跳过转译（视觉池 '{vision_group}'）", "INFO")
        else:
            sys_prompt = "你是一个专业图像解析器。请将用户提供的图片内容转化为极其详细的文字描述（包括画面细节、OCR文字、代码片段等），只输出文字描述，不要有多余的客套话。"
            translation_msgs.insert(0, {"role": "system", "content": sys_prompt})

            for v_ep in vision_eps:
                v_label = self._endpoint_log_label(v_ep, vision_group, v_ep.model)
                sys_log(f"启动图片解析 -> 视觉池 '{vision_group}' 端点 '{v_label}'", "INFO")
                payload = {"model": v_ep.model, "messages": translation_msgs, "stream": False, "max_tokens": 4096}
                result, error = self._try_endpoint(
                    v_ep, payload, timeout=60, log_usage=True, force_no_retry=True,
                    pool_group=vision_group, request_id=request_id,
                    request_deadline=request_deadline,
                )
                if error:
                    sys_log(f"图片解析端点 '{v_label}' 请求失败: {error}", "WARNING")
                    # 失败写阶梯短冷却（幂等：已冷却不刷新），避免坏端点让每个图片请求重复付 60s 超时
                    with self._lock:
                        v_ep._fail_count = int(v_ep._fail_count or 0) + 1
                        self._set_probe_cooldown(v_ep, 60, 600)
                        v_ep._cooldown_reason = "vision_translate_failed"
                    continue

                if isinstance(result, str):
                    description = result
                    result_message = {}
                elif isinstance(result, dict):
                    result_message = (result.get("choices") or [{}])[0].get("message") or {}
                    description = result_message.get("content") or ""
                else:
                    result_message = {}
                    description = ""
                if not description.strip():
                    reasoning = result_message.get("reasoning_content") or ""
                    if reasoning:
                        description = reasoning
                if description:
                    with self._lock:
                        v_ep._fail_count = 0
                        v_ep._cooldown_until = 0
                        v_ep._cooldown_reason = ""
                    break

            if not description:
                sys_log(f"视觉池 '{vision_group}' 内所有端点图片解析均失败", "ERROR")
                return messages

            self._vision_cache_set(cache_key, description)

        new_msgs = _copy.deepcopy(messages)
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
        """探活全部池内端点；批次互斥且与后台探活共享端点级去重。"""
        # 不允许多个 HTTP 入口同时创建探活批次；第二个调用复用当前状态。
        if not self._health_check_lock.acquire(blocking=False):
            with self._lock:
                endpoints = [ep for ep in self._endpoints if ep.enabled and ep.in_pool]
                return [
                    {
                        "id": ep.id,
                        "health": ep._health,
                        "latency_ms": ep._health_latency_ms,
                        "error": ep._health_error or "健康检测进行中",
                    }
                    for ep in endpoints
                ]

        claimed_ids = set()
        try:
            with self._lock:
                endpoints = [ep for ep in self._endpoints if ep.enabled and ep.in_pool]
                for ep in endpoints:
                    if ep._manual_unlock_required or ep.id in self._probe_inflight:
                        continue
                    self._probe_inflight.add(ep.id)
                    claimed_ids.add(ep.id)
                    ep._health = "testing"
            if not endpoints:
                return []

            results = []
            claimed = [ep for ep in endpoints if ep.id in claimed_ids]
            if not claimed:
                return [
                    {
                        "id": ep.id,
                        "health": ep._health,
                        "latency_ms": ep._health_latency_ms,
                        "error": ep._health_error or "健康检测进行中",
                    }
                    for ep in endpoints
                ]
            with ThreadPoolExecutor(max_workers=min(len(claimed), self._health_probe_max_workers)) as pool_exec:
                futures = {pool_exec.submit(self._check_one_health, ep): ep for ep in claimed}
                for future in as_completed(futures):
                    ep = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as e:
                        results.append((ep.id, "bad", -1, str(e)))

            now = time.time()
            with self._lock:
                id_map = {ep.id: ep for ep in self._endpoints}
                for ep_id, health, latency, error in results:
                    ep = id_map.get(ep_id)
                    if ep is None:
                        continue
                    if health == "skipped":
                        # 池自身无可用客户端身份 → 本次未探测：端点状态一个字段都不改（伤害隔离）
                        continue
                    ep._health = health
                    ep._health_latency_ms = latency
                    ep._health_last_check = now
                    ep._health_error = error
                result_map = {
                    ep_id: {"id": ep_id, "health": health, "latency_ms": latency, "error": error}
                    for ep_id, health, latency, error in results
                }
                response = [
                    result_map.get(
                        ep.id,
                        {
                            "id": ep.id,
                            "health": ep._health,
                            "latency_ms": ep._health_latency_ms,
                            "error": (
                                "余额不足，仅支持手动解冻"
                                if ep._manual_unlock_required
                                else ep._health_error or "健康检测进行中"
                            ),
                        },
                    )
                    for ep in endpoints
                ]
            sys_log(f"健康检测完成: 检测了 {len(results)} 个端点", "INFO")
            return response
        finally:
            with self._lock:
                self._probe_inflight.difference_update(claimed_ids)
            self._health_check_lock.release()

    def _is_in_cooldown(self, ep):
        return ep._cooldown_until > time.time()

    # 终极兜底（priority=99）参数：正常轮转预算 530s，保底容错 60s（530+60=590 < Hermes 600s），
    # 锁定滑动窗口 300s（5 分钟无新请求视为任务结束，解锁后重新评估故障端点）
    _FALLBACK_DEADLINE_SECONDS = 530
    _FALLBACK_TIMEOUT_SECONDS = 60
    _FALLBACK_LOCK_SECONDS = 300
    # 子组→main 组级延迟回切的空闲窗口（2026-08-31 A0）：fallback 锁定后，
    # 无该子组请求满 N 秒才允许回组重试；窗口随每次该子组请求滑动顺延。
    _GROUP_FALLBACK_RETURN_SECONDS = 300

    def _get_fallback_endpoint(self):
        """返回优先级 99 的终极兜底端点（启用、在池、未冷却）。"""
        for ep in self._endpoints:
            if (ep.enabled and ep.in_pool and not ep._manual_unlock_required and ep.priority == 99
                    and not self._is_in_cooldown(ep)):
                return ep
        return None

    def _is_fallback_locked(self):
        return self._is_fallback_locked_group(self.MAIN_GROUP)

    def _is_deferred(self, ep):
        return ep._defer_until > time.time()

    @staticmethod
    def _classify_capacity_error(error_msg):
        text = str(error_msg or "").lower()
        balance_markers = (
            "余额不足", "余额已用尽", "余额耗尽", "insufficient balance",
            "insufficient funds", "no remaining balance", "credit balance exhausted",
            "payment required", "账户余额不足",
        )
        if any(marker in text for marker in balance_markers):
            return "balance_insufficient"

        quota_markers = (
            "配额不足", "配额已用尽", "配额耗尽", "quota exceeded", "quota exhausted",
            "quota has been exhausted",
            "insufficient quota", "usage limit exceeded", "usage limit reached",
            "daily limit exceeded", "daily limit reached", "monthly limit exceeded",
            "monthly limit reached", "token quota exceeded", "请求配额已用尽",
        )
        if any(marker in text for marker in quota_markers):
            return "quota_exceeded"
        return ""

    @staticmethod
    def _parse_quota_cooldown_seconds(error_msg, now=None):
        text = str(error_msg or "")
        lower = text.lower()
        now = time.time() if now is None else now

        patterns = (
            (r"retry[- ]after\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(seconds?|secs?|s)\b", 1),
            (r"(?:retry|try again|reset(?:s)?)(?:\s+after|\s+in)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(minutes?|mins?|m)\b", 60),
            (r"(?:retry|try again|reset(?:s)?)(?:\s+after|\s+in)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b", 3600),
            (r"retry[- ]after\s*[:=]?\s*(\d+(?:\.\d+)?)\b", 1),
            (r"(\d+(?:\.\d+)?)\s*(?:秒|秒钟)后", 1),
            (r"(\d+(?:\.\d+)?)\s*分钟后", 60),
            (r"(\d+(?:\.\d+)?)\s*小时后", 3600),
        )
        for pattern, multiplier in patterns:
            match = re.search(pattern, lower)
            if match:
                return max(1, int(float(match.group(1)) * multiplier))

        epoch_match = re.search(r"(?:reset(?:_at)?|重置时间)\s*[:=]?\s*(\d{10}(?:\.\d+)?)", lower)
        if epoch_match:
            return max(1, int(float(epoch_match.group(1)) - now))

        iso_match = re.search(
            r"(?:reset(?:s)?(?:\s+at)?|重置时间)\s*[:=]?\s*"
            r"(\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2}))",
            lower,
        )
        if iso_match:
            try:
                reset_at = datetime.fromisoformat(iso_match.group(1).replace("z", "+00:00"))
                return max(1, int(reset_at.timestamp() - now))
            except ValueError:
                pass

        retry_after_date = re.search(r"retry[- ]after\s*:\s*([^;]+(?:gmt|utc))", lower)
        if retry_after_date:
            try:
                reset_at = parsedate_to_datetime(retry_after_date.group(1))
                return max(1, int(reset_at.timestamp() - now))
            except (TypeError, ValueError):
                pass
        return None

    def _set_capacity_cooldown(self, ep, error_msg):
        kind = self._classify_capacity_error(error_msg)
        if kind == "balance_insufficient":
            ep._manual_unlock_required = True
            ep._cooldown_until = 0
            ep._cooldown_reason = kind
            ep._health = "bad"
            ep._health_error = "余额不足，仅支持手动解冻"
            return kind, None
        if kind == "quota_exceeded":
            seconds = self._parse_quota_cooldown_seconds(error_msg)
            if seconds is None:
                seconds = 5 * 60 * 60
            ep._manual_unlock_required = False
            ep._cooldown_until = time.time() + seconds
            ep._cooldown_reason = kind
            return kind, seconds
        # 纯限流同样尊重 Retry-After；没有明确恢复时间时才交给普通冷却。
        if "HTTP 429" in str(error_msg or ""):
            seconds = self._parse_quota_cooldown_seconds(error_msg)
            if seconds is not None:
                ep._manual_unlock_required = False
                ep._cooldown_until = time.time() + seconds
                ep._cooldown_reason = "rate_limited"
                return "rate_limited", seconds
        return "", None

    @staticmethod
    def _jitter_pct(ep):
        """确定性抖动系数 80%–120%：种子 sha256(ep.id:fail_count)。

        不同端点同批冻结时解冻时间错开（防惊群）；同一端点同一档位跨重启可复现。
        """
        import hashlib
        seed = f"{ep.id}:{int(ep._fail_count)}"
        return 80 + int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 41  # 80..120

    def _set_cooldown(self, ep):
        # 幂等：已在冷却中不刷新冻结时间（并发请求的失败是同一故障的重复观测，
        # 不延长冷却窗口；fail_count 仍由调用方累加）。窗口过期后的新失败自然触发新冻结。
        if ep._cooldown_until > time.time():
            return
        # 普通故障冷却：cooldown_minutes × [80%,120%] 抖动 × 连续失败次数，封顶 1 小时。
        # 阶梯（2026-09-01）：持续故障端点冷却线性拉长（5m→10m→15m→…→60m），
        # 成功/探活通过时 _fail_count 清零自动复位；配额/余额通道由 _set_capacity_cooldown 独立处理。
        n = max(int(ep._fail_count), 1)
        cd_seconds = min(max(ep.cooldown_minutes, 1) * 60 * self._jitter_pct(ep) / 100 * n, 3600)
        ep._cooldown_until = time.time() + cd_seconds

    def _set_probe_cooldown(self, ep, base_seconds, cap_seconds):
        """探活失败阶梯冷却：连续失败线性递增，封顶后稳定（2026-09-01）。

        需求：短时间内探活失败多次重试探活的端点需阶梯式延长冻结时间。
        n = 本次失败后的连续失败次数（_fail_count 由调用方累加；探活通过/请求成功清零），
        时长 = min(base_seconds × n, cap_seconds)。幂等：已在冷却中不刷新窗口。
        """
        if ep._cooldown_until > time.time():
            return
        n = max(int(ep._fail_count), 1)
        seconds = min(base_seconds * n, cap_seconds)
        ep._cooldown_until = time.time() + seconds
        ep._cooldown_reason = "probe_failed"

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
                    ep._cooldown_reason = ""
                    ep._manual_unlock_required = False
                    ep._health = "ok"
                    ep._health_error = ""
                    sys_log(f"端点 '{ep.name}' 手动解冻，错误状态已清除", "INFO")
                    return True
        return False

    def _apply_test_result(self, ep, error=None, latency_ms=-1, group=None):
        """将端点卡片的定向测试结果写回主池端点，并复用正式故障分类。"""
        error_text = str(error or "")
        with self._lock:
            ep._health_latency_ms = int(latency_ms) if latency_ms is not None else -1
            ep._health_last_check = time.time()
            if not error_text:
                ep._health = "ok" if ep._health_latency_ms < 0 or ep._health_latency_ms <= LATENCY_OK_MAX else "slow"
                ep._health_error = ""
                ep._fail_count = 0
                ep._last_error = ""
                ep._last_error_ts = 0
                ep._cooldown_until = 0
                ep._cooldown_reason = ""
                ep._manual_unlock_required = False
                return

            if self._classify_client_error(error_text):
                ep._last_error = error_text
                ep._last_error_ts = time.time()
                sys_log(f"端点 '{self._endpoint_log_label(ep, group)}' 测试返回客户端类错误，不冻结端点: {error_text}", "WARN")
                return

            # 伤害隔离（2026-09-12）：🧪 测试属于"池自身发起的出站"，非容量类失败**只写观测态**
            # （health / health_error / last_error），不冷却、不计 fail_count、不动路由指针。
            # 容量类（余额不足 / 配额耗尽 / 429）例外：必须继续摘端点，否则会被反复选中。
            capacity_kind, capacity_seconds = self._set_capacity_cooldown(ep, error_text)
            ep._health = "bad"
            ep._health_error = error_text
            ep._last_error = error_text
            ep._last_error_ts = time.time()
            test_label = self._endpoint_log_label(ep, group)
            if capacity_kind == "balance_insufficient":
                sys_log(f"端点 '{test_label}' 测试失败，余额不足，已冻结，仅支持手动解冻", "WARN")
            elif capacity_kind == "quota_exceeded":
                detail = f"{capacity_seconds} 秒" if capacity_seconds is not None else "默认 5 小时"
                sys_log(f"端点 '{test_label}' 测试失败，配额不足，冻结 {detail}", "WARN")
            elif capacity_kind == "rate_limited":
                sys_log(f"端点 '{test_label}' 测试失败，上游限流，按 Retry-After 冷却 {capacity_seconds} 秒", "WARN")
            else:
                sys_log(f"端点 '{test_label}' 测试失败（非容量类）：仅记录观测态，不冷却、不计失败次数", "WARN")

    def test_endpoint(self, ep, message="你好", image=None, group=None):
        """在不启用候选轮转的前提下测试主池中的指定端点。"""
        import uuid
        started = time.time()
        request_id = f"test-{uuid.uuid4().hex[:8]}"
        test_group = group or (self._ep_groups(ep)[0] if self._ep_groups(ep) else self.MAIN_GROUP)
        payload_message = message
        if image:
            payload_message = [
                {"type": "text", "text": message},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        payload = {
            "model": ep.model,
            "messages": [{"role": "user", "content": payload_message}],
        }
        sys_log(f"[req={request_id}] 测试端点 '{self._endpoint_log_label(ep, test_group, ep.model)}'", "INFO")
        try:
            result, error = self._try_endpoint(
                ep, payload, timeout=ep.timeout, pool_group=test_group,
                force_no_retry=False, request_id=request_id,
            )
        except PoolIdentityUnavailable:
            # 无可用身份 → 不发请求、不写回任何端点状态（伤害隔离）
            sys_log(f"[req={request_id}] 端点 '{self._endpoint_log_label(ep, test_group)}' {POOL_IDENTITY_SKIP_REASON}", "WARN")
            return None, POOL_IDENTITY_SKIP_REASON
        latency_ms = int((time.time() - started) * 1000)
        if error:
            sys_log(f"[req={request_id}] 端点 '{self._endpoint_log_label(ep, test_group)}' 测试请求失败: {error}", "ERROR")
            self._apply_test_result(ep, error=error, latency_ms=latency_ms, group=test_group)
            return None, error
        with self._lock:
            ep._total_calls += 1
            ep._last_success_ts = time.time()
        self._apply_test_result(ep, latency_ms=latency_ms, group=test_group)
        sys_log(f"[req={request_id}] 端点 '{self._endpoint_log_label(ep, test_group)}' 测试请求成功", "INFO")
        return result, ""

    def _cleanup_expired_cooldowns(self):
        """冷却过期端点：收集后丢给后台线程探活，不阻塞请求路径（2026-08-13 后台化改造）。

        探活结果在后台更新端点状态；下次请求自然使用恢复的端点（defer 逻辑兼容保留）。
        """
        now = time.time()
        defer_window = 300  # 5分钟活跃窗口
        expired = []
        with self._lock:
            for ep in self._endpoints:
                if (ep.in_pool and not ep._manual_unlock_required
                        and ep._cooldown_until > 0 and ep._cooldown_until <= now):
                    expired.append(ep)
            # 分组池在途泄漏兜底：10 分钟无活动仍未释放的在途登记直接清零
            # （正常路径非流式即释放、流式由 wrapper finally 释放；此为异常路径兜底）。
            if now - self._last_pool_activity > 600:
                self._inflight_owner.clear()

        for ep in expired:
            with self._lock:
                # 去重：30秒内已探活过的跳过，避免并发入口重复探活
                if now - ep._health_last_check < 30:
                    continue
                # 已在后台探活中的跳过（防重复入队）
                if ep.id in self._probe_inflight:
                    continue
                # 探活前快照当前端点：探活耗时数秒，期间并发请求可能修改指针，
                # 用快照保证 defer 判断基于探活开始时的状态（避免 999554"已恢复"竞态）
                probe_current_ids = {
                    grp: (self._manual_override_by_group.get(grp) or self._current_endpoint_by_group.get(grp))
                    for grp in self._all_group_names()
                }
                self._probe_inflight.add(ep.id)
            self._probe_executor.submit(self._background_probe, ep, probe_current_ids)

        # 滚动处理缓存保护产生的延迟回切：当前端点仍保护 cache 时延长，
        # 池空闲或当前端点关闭保护时解除。
        self._reconcile_deferred(now, defer_window)

    def _background_probe(self, ep, probe_current_ids=None):
        """后台探活单个端点：通过→清冷却+defer判断+更新各组指针；失败→继续冷却。

        2026-08-29 分组池：端点可属多组，指针更新遍历该端点所属各组判断；
        probe_current_ids 是探活开始时各组指针快照（防竞态）。
        兼容：传入单个 str 视为 main 组指针快照（旧签名）。
        """
        if isinstance(probe_current_ids, str) or probe_current_ids is None:
            probe_current_ids = {self.MAIN_GROUP: probe_current_ids} if probe_current_ids else {}
        try:
            probe_ok, probe_error = self._probe_endpoint(ep)
            if probe_ok is None:
                # 无可用身份 → 未探测：端点状态保持原样，不做任何恢复/冷却动作（伤害隔离）
                return False
            if probe_ok:
                now = time.time()
                with self._lock:
                    ep._fail_count = 0
                    ep._last_error = ""
                    ep._last_error_ts = 0
                    ep._cooldown_until = 0
                    ep._cooldown_reason = ""
                    ep._health = "ok"
                # 端点所属各组逐一评估：当前指针端点决定是否保护自身 cache。
                # 当前工作端点决定是否保护自身 cache：开启时延迟切走，
                # 关闭时恢复端点立即回切。恢复端点自己的开关不参与本次判断。
                pool_active = self._last_pool_activity > now - 300
                for grp in self._ep_groups(ep):
                    current_ep = None
                    current_id = probe_current_ids.get(grp)
                    if current_id:
                        current_ep = next((e for e in self._endpoints if e.id == current_id), None)
                    protect_current_cache = (
                        current_ep is not None
                        and current_ep is not ep
                        and current_ep.deferrable
                        and pool_active
                    )
                    if protect_current_cache:
                        with self._lock:
                            ep._defer_until = now + 300
                        current_label = self._endpoint_log_label(current_ep, grp) if current_ep else f"[{grp}]无"
                        sys_log(f"端点 '{self._endpoint_log_label(ep, grp)}' 冷却过期探活通过；当前端点 '{current_label}' 已开启缓存保护，延迟回切 5 分钟", "INFO")
                    else:
                        switched = False
                        with self._lock:
                            ep._defer_until = 0
                            # 手动指定端点不被后台恢复覆盖；自动路由则立即回切
                            # 到刚恢复的端点，兑现"关闭缓存保护=立即回切"。
                            if not self._get_manual(grp):
                                self._set_current(grp, ep.id)
                                switched = True
                        if switched and current_ep is not None and current_ep is not ep and not current_ep.deferrable:
                            sys_log(f"端点 '{self._endpoint_log_label(ep, grp)}' 冷却过期探活通过；当前端点 '{self._endpoint_log_label(current_ep, grp)}' 未开启缓存保护，立即回切", "INFO")
                        elif self._get_manual(grp):
                            sys_log(f"端点 '{self._endpoint_log_label(ep, grp)}' 冷却过期探活通过；保留手动端点 '{self._endpoint_log_label(current_ep, grp)}'", "INFO")
                        else:
                            sys_log(f"端点 '{self._endpoint_log_label(ep, grp)}' 冷却过期探活通过，已恢复", "INFO")
                    # 仅在该组当前端点不存在或已不可用时更新指针；恢复一个端点不应
                    # 在池仍使用其他健康端点时把路由无故改回 priority 最小端点。
                    with self._lock:
                        current_id = self._get_manual(grp) or self._get_current(grp)
                        current_ep = next(
                            (e for e in self._endpoints if e.id == current_id), None
                        ) if current_id else None
                        current_unavailable = (
                            current_ep is None
                            or not current_ep.enabled
                            or not current_ep.in_pool
                            or self._is_in_cooldown(current_ep)
                        )
                        if not self._get_manual(grp) and current_unavailable:
                            group_eps = [e for e in self._endpoints if e.enabled and e.in_pool
                                         and grp in self._ep_groups(e)
                                         and not self._is_in_cooldown(e)
]
                            if group_eps:
                                best = min(group_eps, key=lambda e: self._ep_priority(e, grp))
                                self._set_current(grp, best.id)
            else:
                with self._lock:
                    ep._fail_count += 1  # 探活失败计入连续失败，驱动阶梯冷却
                    capacity_kind, capacity_seconds = self._set_capacity_cooldown(ep, probe_error or "探活失败")
                if capacity_kind == "balance_insufficient":
                    sys_log(f"端点 '{self._endpoint_log_label(ep, self._ep_groups(ep)[0])}' 后台探活发现余额不足，已冻结，仅支持手动解冻", "WARN")
                elif capacity_kind == "quota_exceeded":
                    detail = f"{capacity_seconds} 秒" if capacity_seconds is not None else "默认 5 小时"
                    sys_log(f"端点 '{self._endpoint_log_label(ep, self._ep_groups(ep)[0])}' 后台探活发现配额不足，冻结 {detail}", "WARN")
                else:
                    with self._lock:
                        # 阶梯冷却：cooldown_minutes × 抖动 × 连续失败次数，封顶 1 小时
                        base = max(ep.cooldown_minutes, 1) * 60 * self._jitter_pct(ep) / 100
                        self._set_probe_cooldown(ep, base_seconds=base, cap_seconds=3600)
                    actual = max(0, (ep._cooldown_until - time.time()) / 60)
                    sys_log(f"端点 '{self._endpoint_log_label(ep, self._ep_groups(ep)[0])}' 冷却过期探活未通过，阶梯冷却 {actual:.1f} 分钟（连续第 {int(ep._fail_count)} 次）", "WARN")
        except Exception as e:
            sys_log(f"端点 '{self._endpoint_log_label(ep, self._ep_groups(ep)[0])}' 后台探活异常: {e}", "ERROR")
        finally:
            with self._lock:
                self._probe_inflight.discard(ep.id)

    def _reconcile_deferred(self, now=None, defer_window=300):
        """处理缓存保护产生的延迟回切状态。

        当前端点保护 cache 且池活跃时持续滚动；池空闲或当前端点关闭保护时，
        解除延迟并恢复自动路由。
        """
        if now is None:
            now = time.time()
        with self._lock:
            pool_active = self._last_pool_activity > now - defer_window
            current_by_group = {
                grp: (self._manual_override_by_group.get(grp) or self._current_endpoint_by_group.get(grp))
                for grp in self._all_group_names()
            }
            current_eps = {
                grp: next((e for e in self._endpoints if e.id == eid), None)
                for grp, eid in current_by_group.items() if eid
            }
            released = []
            for ep in self._endpoints:
                if not (ep.in_pool and ep._defer_until > 0):
                    continue
                # 兜底使用的 defer 清除已由 _on_success(clear_defer=True) 处理；
                # 这里若用实时 current 判断会误清"defer 前进入的并发请求"产生的 defer
                # （探活/请求期间 current 被并发修改，如场景9 竞态）。
                # 分组池：任一组的当前端点保护 cache 且池活跃即保持 defer。
                protected = any(
                    cur is not None and cur is not ep and cur.deferrable
                    for cur in current_eps.values()
                )
                if protected and pool_active:
                    ep._defer_until = now + defer_window  # 滚动延长，保持 cache
                else:
                    ep._defer_until = 0
                    released.append(ep)
                    reason = "池空闲" if not pool_active else "当前端点未开启缓存保护"
                    for grp in self._ep_groups(ep):
                        sys_log(f"端点 '{self._endpoint_log_label(ep, grp)}' 延迟回切解除（{reason}）", "INFO")
            if released:
                for grp in self._all_group_names():
                    if self._get_manual(grp):
                        continue
                    group_eps = [e for e in released if grp in self._ep_groups(e)]
                    if group_eps:
                        best = min(group_eps, key=lambda e: self._ep_priority(e, grp))
                        self._set_current(grp, best.id)

    def _failover_endpoints(self):
        """Return failover-eligible endpoints, including deferred recoveries.

        Deferred recovery blocks proactive failback only. If the current endpoint
        fails, a healthy deferred endpoint remains a valid failover target.
        """
        return [ep for ep in self._endpoints if ep.enabled and ep.in_pool
                and not ep._manual_unlock_required
                and not self._is_in_cooldown(ep)
]

    def _active_endpoints(self):
        available = [ep for ep in self._endpoints if ep.enabled and ep.in_pool
                     and not ep._manual_unlock_required
                     and not self._is_in_cooldown(ep)
                     and not self._is_deferred(ep)]
        if available:
            return available
        # 最后手段：全池无可用 → 返回优先级 99 终极兜底端点（若有）；否则空列表 → AllEndpointsFailed → Hermes fallback。
        # 不再返回冷却中的端点去试错（避免轮转超时 + Hermes 600s 叠加）。
        fb = self._get_fallback_endpoint()
        return [fb] if fb else []

    def _ordered_failover_candidates(self, failed_ep, active, exclude=None, group=None):
        """Return available endpoints in ring order.

        Ring order is calculated from the complete in-pool priority order instead
        of the filtered active list, because the failed endpoint is already in
        cooldown when this function is called. 分组隔离：按该组优先级排 ring。
        模型优先分桶已移除（2026-09-06）：渠道命名差异下字符串相等既漏判又误判，
        同模型/同类型互备由人工把相关端点排成相邻优先级实现。
        """
        grp = group or self.MAIN_GROUP
        excluded_ids = {ep.id for ep in (exclude or ())}
        active_ids = {ep.id for ep in active}
        pool = sorted(
            (ep for ep in self._endpoints if ep.enabled and ep.in_pool),
            key=lambda ep: (self._ep_priority(ep, grp), ep.priority),
        )
        if not pool:
            return []
        try:
            failed_index = next(i for i, ep in enumerate(pool) if ep is failed_ep)
        except StopIteration:
            failed_index = -1
        if failed_index >= 0:
            ring = pool[failed_index + 1:] + pool[:failed_index]
        else:
            ring = pool

        return [
            ep for ep in ring
            if ep.id in active_ids
            and ep is not failed_ep
            and ep.id not in excluded_ids
            and not self._is_in_cooldown(ep)
            and not self._is_manually_locked(ep)
        ]

    @staticmethod
    def _is_manually_locked(ep):
        return bool(ep._manual_unlock_required)

    def _pick_best(self, active):
        for ep in active:
            if not self._is_in_cooldown(ep):
                return ep
        return min(active, key=lambda e: e._cooldown_until) if active else None

    @staticmethod
    def _classify_client_error(error_msg):
        """客户端类错误识别（轮转不记账，upstream-borrow 设计改动二）。

        判定：HTTP 状态码 ∈ {400, 404, 413, 422} 且不含瞬态/配额字样，
        且不含已被内部 workaround 消化的标记（temperature/top_p 自动剥离
        后的重试在 _try_endpoint 内部完成，到达这里的都是二次失败或特例外）。
        这类失败是请求形状问题，不是端点健康问题：好端点也会拒绝畸形请求。
        """
        text = str(error_msg or "")
        if not text.startswith("HTTP "):
            return False
        try:
            code = int(text.split(":", 1)[0].replace("HTTP ", "").strip())
        except ValueError:
            return False
        if code not in (400, 404, 413, 422):
            return False
        lower = text.lower()
        transient_markers = (
            "rate limit", "rate-limited", "429", "quota", "balance",
            "余额", "配额", "限流", "temporarily", "overloaded",
        )
        return not any(marker in lower for marker in transient_markers)

    # 角色不兼容签名（2026-08-31 端点级探测确认，均为生产/探测真实文案）：
    # - Anthropic 风格反序列化：unknown variant `developer`
    # - SiliconFlow：Input tag 'developer' found using 'role'
    # - Opencode(Console Go)：Incorrect role information
    # - qnaigc：role错误，支持类型：user、system、assistant、function、tool
    _ROLE_REJECT_MARKERS = (
        "unknown variant `developer`",
        "input tag 'developer' found using 'role'",
        "incorrect role information",
        "role错误",
    )

    @classmethod
    def _is_role_reject_error(cls, error_msg):
        """客户端类 400 中的「上游不认 developer 角色」签名识别。

        仅在请求携带 developer 角色消息时由调用方使用；命中后同请求内
        把 developer 降级为 system 再轮转后续候选（OpenAI 官方语义中
        developer 即 system 的 GPT-5 形态，降级不改变提示内容）。
        """
        lower = str(error_msg or "").lower()
        if not lower.startswith("http 4"):
            return False
        if any(marker in lower for marker in cls._ROLE_REJECT_MARKERS):
            return True
        return "developer" in lower and "role" in lower

    @staticmethod
    def _downgrade_developer_role(messages):
        """把消息列表中的 developer 角色降级为 system（新列表，不原地修改）。"""
        downgraded = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "developer":
                item = dict(m)
                item["role"] = "system"
                downgraded.append(item)
            else:
                downgraded.append(m)
        return downgraded

    @staticmethod
    def _has_developer_role(messages):
        return any(isinstance(m, dict) and m.get("role") == "developer" for m in messages)

    # DeepSeek thinking 模式（带 tools 时）的严格校验 400 签名（2026-09-10）。
    # 观测：V4.1-Flash 起，中转/上游对历史形态的校验更严格，同一 payload 在不同
    # 上游实例上时通时不通；命中后同端点重试一次（第二次显式关闭 thinking）比直接
    # 轮转换成换模型更省事，也不改变请求所属端点。
    _STRICT_VALIDATION_MARKERS = (
        "must be passed back",
        "must be followed by tool messages",
        "insufficient tool messages",
    )

    @classmethod
    def _is_strict_validation_400(cls, error_msg):
        """识别 DeepSeek thinking 模式的严格校验 400（reasoning_content / tool 配对）。"""
        lower = str(error_msg or "").lower()
        if not lower.startswith("http 4"):
            return False
        return any(marker in lower for marker in cls._STRICT_VALIDATION_MARKERS)

    @staticmethod
    def _repair_dangling_tool_calls(messages):
        """给未被 tool 消息应答的 assistant tool_calls 补合成结果（新列表）。

        DeepSeek 校验「assistant tool_calls 后必须跟齐每条 tool_call_id 的 tool 消息」，
        中断/异常历史的悬空 tool_call 会触发 400。合成结果仅补齐结构，不伪造业务内容。
        """
        out = []
        pending = []  # [(msg_index_in_out, tool_call_id)]
        for m in messages:
            if not isinstance(m, dict):
                out.append(m)
                continue
            role = m.get("role")
            if role == "assistant":
                out.append(m)
                for tc in (m.get("tool_calls") or []):
                    tc_id = tc.get("id") or ""
                    if tc_id:
                        pending.append((len(out) - 1, tc_id))
            elif role == "tool":
                tc_id = m.get("tool_call_id") or ""
                if tc_id:
                    pending = [p for p in pending if p[1] != tc_id]
                out.append(m)
            else:
                out.append(m)
        if not pending:
            return messages
        # 把合成结果插到各自 assistant 消息之后（保持原有顺序的稳定重建）
        by_assistant = {}
        for _, tc_id in pending:
            by_assistant.setdefault(_, []).append(tc_id)
        rebuilt = []
        for i, m in enumerate(out):
            rebuilt.append(m)
            for tc_id in by_assistant.get(i, []):
                rebuilt.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": "[tool call aborted — no result was produced]",
                })
        return rebuilt

    def _rotate(self, failed_ep, error_msg, probe_failed=False, skip_cooldown=False, health_impact=True, group=None,
                expected_route_epoch=None, request_id=None):
        grp = group or self.MAIN_GROUP
        request_tag = f"[req={request_id}] " if request_id else ""
        if getattr(_POOL_INITIATED_CTX, "active", False):
            # 伤害隔离（2026-09-12）：池自身发起的请求（管理接口测试等）失败**只写观测态**：
            # 不冷却、不计 fail_count、不改当前/手动指针、不建立 fallback 锁。
            with self._lock:
                failed_ep._last_error = error_msg
                failed_ep._last_error_ts = time.time()
            sys_log(
                f"{request_tag}池自身发起的请求在端点 '{self._endpoint_log_label(failed_ep, grp)}' 失败"
                f"（只记观测态，不改路由）: {error_msg}",
                "WARN",
            )
            return self._get_route_epoch(grp)
        if health_impact:
            failed_ep._fail_count += 1
            failed_ep._total_failures += 1
            failed_ep._last_error = error_msg
            failed_ep._last_error_ts = time.time()
            # chat() 已带同一 request_id 记录过本次失败；直接调用 _rotate
            # （探活/测试等）才在这里补日志，避免同一错误重复两行。
            if request_id is None:
                sys_log(f"{request_tag}端点 '{self._endpoint_log_label(failed_ep, grp)}' 请求失败: {error_msg}", "ERROR")
        else:
            # 客户端类错误：请求形状问题，非端点故障。不冻结、fail_count 不增、
            # 不探活（探活小请求必然通过，无信息量），仅记录最后错误便于排查。
            # 也不改路由指针：端点本身健康，后续请求仍应粘性使用（本请求内的
            # 候选轮转由 chat() 的 client_error_tried 集合控制）。
            failed_ep._last_error = error_msg
            failed_ep._last_error_ts = time.time()
            sys_log(f"端点 '{self._endpoint_log_label(failed_ep, grp)}' 客户端类错误(不冻结/不记账)，同请求继续轮转: {error_msg}", "WARN")
            return self._get_route_epoch(grp)
        capacity_kind, capacity_seconds = self._set_capacity_cooldown(failed_ep, error_msg)
        if capacity_kind == "balance_insufficient":
            sys_log(f"端点 '{self._endpoint_log_label(failed_ep, grp)}' 余额不足，已冻结，仅支持手动解冻", "WARN")
        elif capacity_kind == "quota_exceeded":
            detail = f"{capacity_seconds} 秒" if capacity_seconds is not None else "默认 5 小时"
            sys_log(f"端点 '{self._endpoint_log_label(failed_ep, grp)}' 配额不足，冻结 {detail}", "WARN")
        elif capacity_kind == "rate_limited":
            sys_log(f"端点 '{self._endpoint_log_label(failed_ep, grp)}' 上游限流，按 Retry-After 冷却 {capacity_seconds} 秒", "WARN")
        elif probe_failed:
            # 探活失败：阶梯短冷却（30 秒 × 连续失败次数，封顶 30 分钟），
            # 首次不误杀冷启动端点，连续失败逐步拉长冻结时间。
            self._set_probe_cooldown(failed_ep, base_seconds=30, cap_seconds=1800)
            actual = max(0, (failed_ep._cooldown_until - time.time()) / 60)
            sys_log(f"端点 '{self._endpoint_log_label(failed_ep, grp)}' 探活失败，阶梯冷却 {actual:.1f} 分钟（连续第 {int(failed_ep._fail_count)} 次）", "WARN")
        elif skip_cooldown:
            # 端点级活跃判定：超时类失败但端点在 timeout 窗口内有成功响应
            # → 单请求饿死，非端点故障；不冻结、不切换当前端点。
            sys_log(f"端点 '{self._endpoint_log_label(failed_ep, grp)}' 活跃(窗口内有成功)，判定单请求饿死，不冻结不切换", "WARN")
            return self._get_route_epoch(grp)
        else:
            self._set_cooldown(failed_ep)
            # 抖动+阶梯后实际窗口为 cooldown_minutes×[80%,120%]×连续失败次数，日志展示真实值
            actual = max(0, (failed_ep._cooldown_until - time.time()) / 60)
            sys_log(f"端点 '{self._endpoint_log_label(failed_ep, grp)}' 触发冷却机制，下次可用时间在 {actual:.1f} 分钟后（连续第 {int(failed_ep._fail_count)} 次）", "WARN")
        # 分组池：轮转切换限定在失败请求所属组内（含 bg 组 fallback 逻辑见 chat()）。
        active = [e for e in self._failover_endpoints() if grp in self._ep_groups(e)]
        candidates = self._ordered_failover_candidates(failed_ep, active, group=grp)
        with self._lock:
            # 请求启动后若已有更新的路由决策，保留该决策；本请求仍可在局部
            # active 列表中继续重试，但不得让迟到失败覆盖 current/manual。
            if expected_route_epoch is not None and self._get_route_epoch(grp) != expected_route_epoch:
                return expected_route_epoch
            if candidates:
                self._set_current(grp, candidates[0].id)
            else:
                self._set_current(grp, None)
            self._set_manual(grp, None)  # 当前路由决策内的失败才清除手动覆盖
            return self._get_route_epoch(grp)

    def _on_success(self, ep, result=None, clear_defer=True, group=None, expected_route_epoch=None):
        grp = group or self.MAIN_GROUP
        now = time.time()
        self._last_pool_activity = now  # 记录池活跃时间（用于 defer 判断）
        # 仅在"请求开始时端点已在 defer（兜底使用）"时清除延迟状态。
        # 竞态保护：defer 设置前已进入的并发请求（请求开始时不在 defer）成功后
        # 不清 defer，避免并发请求破坏延迟回迁保护窗口。
        if clear_defer:
            ep._defer_until = 0
        ep._total_calls += 1
        ep._last_success_ts = now
        ep._health = "ok"
        ep._fail_count = 0
        ep._last_error = ""
        ep._health_error = ""
        if not ep._manual_unlock_required:
            ep._cooldown_reason = ""
        # 仅在非冷却中清除冷却，防止并发请求穿透冷却保护（429→冷却→并发成功→清冷却→再429）
        if not self._is_in_cooldown(ep):
            self._clear_cooldown(ep)
        # 分组池：请求成功只更新该组粘性指针（内存态）。
        # runtime_state 落盘改为事件驱动（2026-09-01 方案 A）：
        # - 手动切换/模型切换等显式操作即时写盘（低频，用户意图）
        # - 正常停止前 SIGTERM 快照全量落盘（main() 注册 snapshot_runtime_state）
        # - 自动路由成功不再热路径读写文件（零 IO）；崩溃重启由启动恢复 + 残留自愈兜底
        with self._lock:
            route_is_current = (
                expected_route_epoch is None
                or self._get_route_epoch(grp) == expected_route_epoch
            )
            if route_is_current:
                self._set_current(grp, ep.id)
                if self._get_manual(grp) and self._get_manual(grp) != ep.id:
                    self._set_manual(grp, None)
    def _rewrite_tool_call_ids(self, messages, prefix):
        """确定性重写 tool_call id：原 id → 前缀+md5 后缀，保持 assistant/tool 配对。

        DeepSeek 官方（如 Kcne）校验历史里 tool_call id 必须为其生成的格式（call_00_ET_*），
        跨端点切换后历史混入其他端点生成的 id 会导致 HTTP 400。重写为统一前缀后，
        服务端视为新消息跳过校验。md5 保证确定性（跨轮次稳定，不破坏缓存）且配对一致。

        非变异（2026-09-12）：只复制命中的消息与其 tool_calls 列表，绝不改动传入的历史对象。
        轮转的各次尝试共用同一份 Hermes 历史，原地改写会把「某端点需要的重写结果」带给后续
        异构端点（违反按端点隔离铁律），并使「改回原始 id 再试一次」在实现上不可能。
        返回 (新消息列表, 实际重写条数)；未命中任何 id 时 changed=0，调用方据此判定变体重试无意义。
        """
        import hashlib

        mapping = {}
        out = list(messages)
        changed = 0
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant" and message.get("tool_calls"):
                new_calls = None
                for call_index, call in enumerate(message.get("tool_calls") or []):
                    if not isinstance(call, dict):
                        continue
                    original_id = call.get("id", "")
                    if not original_id or original_id.startswith(prefix):
                        continue
                    if original_id not in mapping:
                        mapping[original_id] = prefix + hashlib.md5(original_id.encode()).hexdigest()[:16]
                    if new_calls is None:
                        new_calls = [dict(item) if isinstance(item, dict) else item
                                     for item in message["tool_calls"]]
                    new_calls[call_index]["id"] = mapping[original_id]
                    changed += 1
                if new_calls is not None:
                    rewritten = dict(message)
                    rewritten["tool_calls"] = new_calls
                    out[index] = rewritten
            elif message.get("role") == "tool" and message.get("tool_call_id"):
                original_id = message["tool_call_id"]
                if original_id and original_id in mapping:
                    rewritten = dict(message)
                    rewritten["tool_call_id"] = mapping[original_id]
                    out[index] = rewritten
                    changed += 1
        return out, changed

    @staticmethod
    def _is_deepseek_endpoint(ep):
        """Return whether an endpoint needs DeepSeek-specific message fields."""
        model = str(getattr(ep, "model", "") or "").lower()
        base_url = str(getattr(ep, "base_url", "") or "").lower()
        prefix = str(getattr(ep, "tool_call_id_prefix", "") or "").lower()
        return "deepseek" in model or "deepseek" in base_url or bool(prefix)

    @staticmethod
    def _is_glm_endpoint(ep):
        """GLM 系列端点：交错/保留式思考要求回传 reasoning_content。"""
        return "glm" in str(getattr(ep, "model", "") or "").lower()

    @classmethod
    def _keep_reasoning_fields(cls, ep):
        """端点 reasoning_content 回传策略（端点级 reasoning_policy 覆盖启发式）。

        keep/strip 显式覆盖；auto 家族对齐 Hermes _REASONING_ECHO_RULES（docs:
        DeepSeek/Kimi/Moonshot/MiMo 回传校验）+ GLM（交错/保留式思考），其余剥离。
        """
        policy = str(getattr(ep, "reasoning_policy", "") or "").strip().lower()
        if policy == "keep":
            return True
        if policy == "strip":
            return False
        model = str(getattr(ep, "model", "") or "").lower()
        base_url = str(getattr(ep, "base_url", "") or "").lower()
        if cls._is_deepseek_endpoint(ep) or cls._is_glm_endpoint(ep):
            return True
        # Kimi/Moonshot、Xiaomi MiMo：与 Hermes 相同的回传校验家族
        return any(k in model or k in base_url for k in ("kimi", "moonshot", "mimo"))

    # GLM 官方 reasoning_effort 映射（docs.bigmodel.cn 深度思考页，2026-09-08）：
    # GLM-5.3/5.3-FLASH 标准 API 仅接受 max/high/low，其余报错；GLM-5.2 服务端自动
    # 映射 medium→high、xhigh→max，透传即可。Coding Plan 语义：low→low，
    # medium/high→high，xhigh/max→max。
    _GLM_EFFORT_MAP = {
        "none": "low", "minimal": "low", "low": "low",
        "medium": "high", "high": "high",
        "xhigh": "max", "max": "max",
    }
    # GLM-5.3 官方契约：thinking.type 仅支持 enabled —— 思考不可关闭，传 disabled
    # 直接 400「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」。
    # 官方迁移建议：原发 {"type":"disabled"} 的调用方改为 {"type":"enabled"} +
    # reasoning_effort "low"。直连实测（ps.air-outer glm-5.3，2026-09-11）：
    # disabled / reasoning_effort none / medium 命中同一 400；low 正常。
    _GLM_DISABLE_THINKING_TYPES = frozenset({"disabled", "none", "off"})

    @classmethod
    def _normalize_glm_thinking(cls, payload, ep):
        """把 GLM-5.3 端点上「关闭思考」的表达折叠成官方允许的形态。

        数据式映射（与 Hermes agent/reasoning_effort.py 的 GLM53/OX_ALPHA 声明
        同型），不在调用点按 vendor 名散写分支。仅处理显式关闭意图。
        """
        model = str(getattr(ep, "model", "") or "").lower()
        if "glm-5.3" not in model:
            return
        thinking = payload.get("thinking")
        if not isinstance(thinking, dict):
            return
        if str(thinking.get("type", "") or "").lower() not in cls._GLM_DISABLE_THINKING_TYPES:
            return
        payload["thinking"] = {"type": "enabled"}
        payload.setdefault("reasoning_effort", "low")
    _KIMI_K3_EFFORT_MAP = {
        "none": "low", "minimal": "low", "low": "low",
        "medium": "high", "high": "high",
        "xhigh": "max", "max": "max",
    }
    _KIMI_K2_EFFORT_MAP = {
        "none": "low", "minimal": "low", "low": "low",
        "medium": "medium", "high": "high",
        "xhigh": "high", "max": "high",
    }
    _DEEPSEEK_V4_EFFORT_MAP = {
        "none": "low", "minimal": "low", "low": "low",
        "medium": "medium", "high": "high",
        "xhigh": "max", "max": "max",
    }

    @staticmethod
    def _is_kimi_endpoint(ep):
        model = str(getattr(ep, "model", "") or "").lower()
        base_url = str(getattr(ep, "base_url", "") or "").lower()
        return any(k in model or k in base_url for k in ("kimi", "moonshot"))

    @staticmethod
    def _is_kimi_k3_model(model):
        """匹配 k3、k3-*、kimi-k3*，但不误判 kimi-k2.6。"""
        return re.search(r"(?:^|[^a-z0-9])k3(?:[^a-z0-9]|$)", model) is not None

    @classmethod
    def _map_reasoning_effort(cls, payload, ep):
        """按端点模型/家族改写或剔除顶层 reasoning_effort。仅处理显式值。"""
        if "reasoning_effort" not in payload:
            return
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        model = str(getattr(ep, "model", "") or "").lower()
        if "glm-5.3" in model:
            payload["reasoning_effort"] = cls._GLM_EFFORT_MAP.get(effort, "high")
        elif "glm" in model and "glm-5.2" not in model:
            # glm-4.x 等不支持该参数的 GLM 模型：剔除，交服务端默认
            payload.pop("reasoning_effort", None)
        elif cls._is_kimi_endpoint(ep):
            mapping = cls._KIMI_K3_EFFORT_MAP if cls._is_kimi_k3_model(model) else cls._KIMI_K2_EFFORT_MAP
            payload["reasoning_effort"] = mapping.get(effort, "high")
        elif cls._is_deepseek_endpoint(ep) and "deepseek-v3" not in model:
            payload["reasoning_effort"] = cls._DEEPSEEK_V4_EFFORT_MAP.get(effort, "high")

    _REASONING_PLACEHOLDER = " "  # tool-call 轮次的空 reasoning 占位（上游要求非空回传）

    @classmethod
    def _normalize_reasoning_shape(cls, messages):
        """归一化历史里 assistant 的 reasoning 形态（非变异；仅在需要回传 reasoning 的端点生效）。

        规则（2026-09-12，外部实践 + 我们 09-10 实证：上游对"思考内容为空"的历史直接 400）：
        - assistant **带 tool_calls**：reasoning 缺失/空串/纯空白 → 补占位 " "（须非空回传）
        - assistant **不带 tool_calls**：reasoning 缺失/空 → 删掉该字段（不发送空值）
        - 有真实 reasoning：原样保留

        字段名取该消息已存在的那个（reasoning_content 优先，其次 reasoning_text）；都不存在时补
        reasoning_content。只复制被改动的消息，原始历史对象不变（轮转各次尝试共用同一份历史）。
        """
        out = None
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            field = next((name for name in ("reasoning_content", "reasoning_text") if name in message),
                         "reasoning_content")
            value = message.get(field)
            if value is not None and not (isinstance(value, str) and not value.strip()):
                continue  # 有真实 reasoning
            if not message.get("tool_calls"):
                if "reasoning_content" not in message and "reasoning_text" not in message:
                    continue  # 本来就没带该字段 → 无需改动（零拷贝）
                item = dict(message)
                item.pop("reasoning_content", None)
                item.pop("reasoning_text", None)
            else:
                item = dict(message)
                item[field] = cls._REASONING_PLACEHOLDER
            if out is None:
                out = list(messages)
            out[index] = item
        return out if out is not None else messages

    @classmethod
    def _messages_for_endpoint(cls, messages, ep):
        """Strip reasoning_content/reasoning_text for endpoints that don't accept them.

        GLM（交错/保留式思考）与 DeepSeek 一样要求回传 reasoning_content，一并保留；
        端点级 reasoning_policy=keep/strip 可强制覆盖启发式。
        保留分支同时做**回传形态归一化**（空 reasoning 按有无 tool_calls 分别补占位/删字段）。
        """
        if cls._keep_reasoning_fields(ep):
            return cls._normalize_reasoning_shape(messages)

        cleaned = []
        for message in messages:
            if not isinstance(message, dict):
                cleaned.append(message)
                continue
            item = dict(message)
            item.pop("reasoning_content", None)
            item.pop("reasoning_text", None)
            cleaned.append(item)
        return cleaned

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

    def _pool_identity_headers(self, ep, label=""):
        """池自身出站的出站头（确定性身份，2026-09-12）。

        与"有客户端上下文（代理路径）"分支相对：那时按端点 profile 或忠实透传当前请求头。
        解析不到任何确定身份时抛 PoolIdentityUnavailable —— 调用方必须跳过本次出站。"""
        headers, source = resolve_pool_identity(
            ep, self._client_profiles, self._probe_client_profile, label=label,
        )
        if source == "none":
            self._log_identity_skip(ep, label)
            raise PoolIdentityUnavailable(POOL_IDENTITY_SKIP_REASON)
        return headers, source

    def _log_identity_skip(self, ep=None, label=""):
        """无身份跳过的 WARN 去重（同端点同原因 10 分钟一次），避免探活线程刷屏。"""
        ep_id = getattr(ep, "id", None) or (ep if isinstance(ep, str) else "") or ""
        name = getattr(ep, "name", None) or label or ep_id or "?"
        key = (ep_id, "no-identity")
        now = time.time()
        with self._lock:
            if now - self._identity_skip_log_ts.get(key, 0) < 600:
                return
            self._identity_skip_log_ts[key] = now
        sys_log(
            f"池自身出站：{name} {POOL_IDENTITY_SKIP_REASON}"
            "（该端点未配置 client_profile，池级 probe_client_profile 为空，且无客户端基线）",
            "WARN",
        )

    @staticmethod
    def _strict400_backoff(level):
        """严格校验 400 重试前的抖动退避秒数（模块常量 _STRICT400_BACKOFF_MS，测试可置 0）。"""
        import random
        base_ms = _STRICT400_BACKOFF_MS[min(level, len(_STRICT400_BACKOFF_MS) - 1)]
        if base_ms <= 0:
            return 0.0
        return (base_ms * random.uniform(1.0, 1.4)) / 1000.0

    def _probe_endpoint(self, ep):
        """对单个端点做快速探活，成功返回 True、失败返回 False、无可用身份返回 None（跳过）。统一用 chat ping 检测。"""
        started = time.time()
        if ep.health_mode == "none":
            with self._lock:
                ep._health = "unknown"
                ep._health_latency_ms = 0
                ep._health_last_check = started
                ep._health_error = "已禁用健康检测"
            return True, ""  # 关闭检测视为可用
        # 统一使用 chat ping 探活：models 接口可访问不代表模型可响应
        with self._lock:
            prev_health, prev_health_error = ep._health, ep._health_error
            ep._health = "testing"
            ep._health_last_check = started
            ep._health_error = ""
        payload = {"model": ep.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": self.PROBE_MAX_TOKENS, "stream": False}
        try:
            reply, err = self._try_endpoint(ep, payload, timeout=10, log_usage=False, force_no_retry=True, is_probe=True)
        except PoolIdentityUnavailable:
            # 无可用客户端身份 → 本次未探测；恢复 probing 前的观测态（不改动任何状态）
            with self._lock:
                ep._health = "unknown" if prev_health == "testing" else prev_health
                ep._health_error = prev_health_error
            return None, POOL_IDENTITY_SKIP_REASON
        latency = int((time.time() - started) * 1000)
        success = reply is not None
        with self._lock:
            ep._health = "ok" if success and latency <= LATENCY_OK_MAX else ("slow" if success else "bad")
            ep._health_latency_ms = latency
            ep._health_last_check = time.time()
            ep._health_error = "" if success else (str(err) if err else "未知错误")
        return success, err

    def chat(self, messages, model=None, extra_payload=None, timeout=None, return_endpoint=False, request_id=None):
        self._cleanup_expired_cooldowns()
        if request_id is None:
            import uuid
            request_id = uuid.uuid4().hex[:8]
        request_tag = f"[req={request_id}] "
        _chat_start = time.time()  # 530s 轮转预算计时起点（与 Hermes 600s 超时窗口对齐）
        request_deadline = _chat_start + self._FALLBACK_DEADLINE_SECONDS
        debug_trace = [] if _DEBUG_LOGGING else None
        # ── 分组池路由（2026-08-29 spec）：model 字段 → 组选择器 ──
        # "api-pool"→main 别名（存量流量零感知）；精确匹配组名；无匹配→main。
        group = self._resolve_request_group(model)
        # 子组→main 组级延迟回切锁（2026-08-31 A0 触发点 3=锁检查）：
        # fallback 锁定期间该子组请求直走 main，不重复解析死组；每次请求把
        # 空闲窗口顺延（无请求 N 秒后才回组）。期满后本请求回组试探。
        if group != self.MAIN_GROUP:
            with self._lock:
                lock_until = self._group_fallback_lock_until.get(group, 0)
                if lock_until > time.time():
                    # 与端点 defer 同构（2026-09-12 用户定稿）：锁定期路由静默走 main，
                    # 状态经 /api/chain 的 fallback_lock_remaining + ↩main 徽标呈现；
                    # 转移事件由入口/耗尽 fallback 的 WARN 行承担，不逐请求打日志
                    # （曾逐请求刷屏：24h 235 行）。
                    self._group_fallback_lock_until[group] = time.time() + self._GROUP_FALLBACK_RETURN_SECONDS
                    group = self.MAIN_GROUP
                else:
                    # 锁已期满：本请求即回本组试探 = 回切已发生，⏸待回切状态退场
                    # （组内仍全挂会由下方入口/耗尽 fallback 重新上锁 + 重新标记）
                    self._group_fallback_pending.pop(group, None)
        request_route_epoch = self._get_route_epoch(group)
        group_fallback_used = False  # bg 组入口/耗尽 fallback 到 main 的标记
        # 终极兜底锁定：锁定期间该组请求直连 prio99（per-group 滑动窗口，prio99 仅 main 组语义生效）
        if self._is_fallback_locked_group(group):
            fb = self._get_fallback_endpoint()
            if fb is not None:
                sys_log(f"终极兜底锁定中（组 '{group}'），直连端点 '{fb.name}'", "INFO")
                active = [fb]
                starved = False
            else:
                with self._lock:
                    self._fallback_lock_until_by_group[group] = 0  # 兜底不可用，解除锁定
                active, starved = self._group_sticky_candidates(group)
        else:
            active, starved = self._group_sticky_candidates(group)
        if starved:
            sys_log(f"组 '{group}' 互斥过滤后无可用端点，main 保底特权忽略互斥", "WARN")
        # bg → main 入口兜底（触发点 1）：bg 组无可用端点 → 整请求改走 main 组正常路由
        if not active and group != self.MAIN_GROUP:
            active, starved2 = self._group_sticky_candidates(self.MAIN_GROUP)
            if active:
                group_fallback_used = True
                sys_log(f"组 '{group}' 无可用端点，入口 fallback 到 main 组", "WARN")
                # A0：建立组级延迟回切锁（滑动空闲窗口，无请求 N 秒后回组）+ 标记待回切
                with self._lock:
                    self._group_fallback_lock_until[group] = time.time() + self._GROUP_FALLBACK_RETURN_SECONDS
                    self._group_fallback_pending[group] = True
                group = self.MAIN_GROUP
                request_route_epoch = self._get_route_epoch(group)
        if not active:
            raise ValueError("没有可用的 API 端点")
        errors = []
        client_error_tried = set()  # 本请求内已因客户端类错误轮转过的端点（防不冻结路径死循环）
        role_downgraded = False  # 已因角色不兼容 400 触发 developer→system 降级（每请求一次）
        # 严格校验 400 的请求级重试预算（2026-09-12 定案：上限 1）：唯一一次重试就是
        # 「改用客户端原始 tool_call id」的换形态请求（替换原「原样复读」重试）。
        # 严格校验 400 的同端点重试（2026-09-12 两级口径）：第 1 级原样重试（实例级随机故障），
        # 第 2 级「原始 tool_call id」变体（仅当本次尝试确实应用过前缀重写）；上限取端点
        # strict400_retries（0/1/2，默认 2；前缀为空的端点实际只走第 1 级）。
        strict_validation_retries = 0
        strict_variant_used = False    # 第 2 级变体是否已用（每请求最多 1 次）
        skip_prefix_rewrite = False    # 下一次尝试跳过 tool_call_id_prefix 重写（消费一次即清除）
        tried = 0
        total = len(active)
        # 按优先级排序：每次从最高优先级端点开始尝试，故障自动降级，恢复后自动回迁
        # 分组隔离（2026-08-29）：按请求所属组的组内优先级排序
        active.sort(key=lambda e: self._ep_priority(e, group))
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
        # 悬空 tool_calls（assistant 有 tool_calls 但缺对应 tool 结果）→ 补合成结果：
        # DeepSeek 校验「tool_calls 后必须跟齐每条 tool_call_id」（2026-09-10 观测）。
        messages = self._repair_dangling_tool_calls(messages)

        # 当前端点保持粘性：无明确故障/手动切换/恢复回迁时，后续请求继续使用
        # 最近成功或故障转移选中的端点，而不是每次回到最高优先级端点。
        # 分组池：粘性指针按组读取（fallback 到 main 后读 main 组指针）。
        current_id = self._get_manual(group) or self._get_current(group)
        if current_id:
            current_ep = next((ep for ep in active if ep.id == current_id), None)
            if current_ep is not None:
                active.remove(current_ep)
                active.insert(0, current_ep)

        idx = 0
        previous_attempt_site_id = None
        while tried < total:
            ep = active[idx]
            # 客户端类错误轮转终止：不冻结路径没有冷却收缩 total，靠请求级已试集合收口。
            if ep.id in client_error_tried:
                tried += 1
                idx = (idx + 1) % total
                continue
            # 并发保护：端点已被其他请求冻结时，跳过它转向下一个可用端点。
            # 仅当池中还存在非冷却端点时才跳过；全部冷却时走 _active_endpoints 的
            # fallback（按解冻时间排序尝试），保留"宁可试冷却端点也不报错"的兜底语义。
            if self._is_in_cooldown(ep) and any(not self._is_in_cooldown(e) for e in active):
                tried += 1
                idx = (idx + 1) % total
                continue
            # 530s 预算：正常轮转超时 → 立即切终极兜底（不再继续轮转，避免 Hermes 600s 超时并发重试）
            # 分组池：prio99 终极兜底语义仅 main 组生效（spec），预算跨组共享不翻倍。
            if (time.time() - _chat_start) >= self._FALLBACK_DEADLINE_SECONDS and group == self.MAIN_GROUP:
                fb = self._get_fallback_endpoint()
                if fb is not None and fb is not ep and not self._is_in_cooldown(fb):
                    sys_log(f"轮转超 {self._FALLBACK_DEADLINE_SECONDS}s，切换终极兜底 '{fb.name}'", "WARN")
                    with self._lock:
                        self._fallback_lock_until_by_group[group] = time.time() + self._FALLBACK_LOCK_SECONDS
                    if fb in active:
                        idx = active.index(fb)
                        continue
            # 快照请求开始时的缓存保护延迟状态：用于 _on_success 判断是否为回切后的请求。
            # 竞态保护：defer 设置前已进入的请求（快照=0）成功后不清 defer。
            defer_at_request = ep._defer_until
            ep_timeout = timeout or ep.timeout
            ep_model = ep.model
            # 分组池在途登记：本请求即将占用该端点（组内互斥用；流式请求在整个
            # 流生命周期内保持占用，由 stream_generator 的 finally 释放）。
            with self._lock:
                self._acquire_inflight(ep.id, group)
            
            # 按目标端点隔离 DeepSeek 专属 reasoning 字段；每次轮转都
            # 从同一份 Hermes 历史构造独立消息，避免污染后续端点。
            # 角色降级（2026-08-31）：同请求内已有端点因「不认 developer 角色」
            # 400 轮转时，后续候选统一把 developer 降级为 system，避免逐个
            # 撞同类 400（端点级差异：GLM/DS/Qwen 部分端点不收 developer）。
            if role_downgraded:
                base_messages = self._downgrade_developer_role(messages)
            else:
                base_messages = messages
            loop_messages = self._messages_for_endpoint(base_messages, ep)
            is_anthropic = (getattr(ep, "protocol", "openai") == "anthropic")
            # tool_call id 前缀重写：端点配置 tool_call_id_prefix 非空时，把消息里所有 tool_call id
            # 重写为该前缀格式（如 DeepSeek 官方 call_00_ET_）。跨端点切换后历史里混入其他端点
            # 生成的 id（不同前缀/格式），Kcne 等 DeepSeek 官方会校验并报 400
            # "reasoning_text must be passed back"；重写为统一格式后服务端视为新消息跳过校验。
            ep_prefix = getattr(ep, "tool_call_id_prefix", "") or ""
            attempt_prefix_applied = 0
            if ep_prefix and not is_anthropic and not skip_prefix_rewrite:
                # 非变异重写：返回新列表，原始历史（轮转各次尝试共用）不被改动
                loop_messages, attempt_prefix_applied = self._rewrite_tool_call_ids(loop_messages, ep_prefix)
            # 变体开关只作用于紧随其后的那一次尝试（消费一次即清除）：轮转后各端点
            # 按自己的配置重新决定是否重写，不会把本端点的变体状态带给其他端点。
            skip_prefix_rewrite = False
            
            payload = {
                "model": ep_model, "messages": loop_messages,
                **(extra_payload or {}),
            }
            # GLM 系列推理强度适配：reasoning_effort 按官方映射改写/剔除；
            # preserved_thinking 开关：注入 clear_thinking=False 开启保留式思考
            if not is_anthropic:
                self._map_reasoning_effort(payload, ep)
                if getattr(ep, "preserved_thinking", False):
                    t = payload.get("thinking")
                    if isinstance(t, dict):
                        t["clear_thinking"] = False
                    else:
                        payload["thinking"] = {"type": "enabled", "clear_thinking": False}
                # GLM-5.3 思考不可关闭：客户端/nightly LLM 下发的 disabled 在这里
                # 折叠为官方形态（enabled + effort low），避免上游 400。
                self._normalize_glm_thinking(payload, ep)
            
            # [VISION TRANSLATION INTERCEPT]
            if self._has_images(payload["messages"]) and getattr(ep, "is_vision", True) is False:
                vision_cands, vision_grp = self._vision_pool_candidates()
                if not vision_cands:
                    # 降级策略 B（2026-09-10）：视觉池无可用端点 → 图片原样直发，
                    # 打一次 WARNING 保持可观测；不回退"组内任意 is_vision 端点"。
                    _why = f"图片解析池 '{vision_grp}' 无可用端点"
                    sys_log(f"{_why}，图片未转译，原样转发目标端点", "WARNING")
                else:
                    if payload.get("stream"):
                        def vision_wrapper(tgt_ep, pld, t_out, a_eps, _grp=group):
                            import json
                            yield f"data: {{'choices':[{{'delta':{{'content':'[API Pool: 检测到图片，当前目标不支持视觉，正在调用视觉模型进行解析...]\\n\\n'}}}}]}}\n\n".replace("'", '"')
                            translated_msgs = self._translate_images_sync(
                                pld["messages"], a_eps, _grp,
                                request_id=request_id, request_deadline=request_deadline,
                            )
                            yield f"data: {{'choices':[{{'delta':{{'content':'[图片解析完成，交由目标模型继续处理...]\\n\\n'}}}}]}}\n\n".replace("'", '"')
                            pld["messages"] = translated_msgs
                            gen, err = self._try_endpoint(
                                tgt_ep, pld, t_out, pool_group=group,
                                request_id=request_id, request_deadline=request_deadline,
                            )
                            if err:
                                yield f"data: {{'choices':[{{'delta':{{'content':'\\n\\n[API Pool Error: 请求最终目标失败: {err}]'}}}}]}}\n\n".replace("'", '"')
                            else:
                                # 实际请求成功后标记端点成功
                                # clear_defer：仅当请求开始时端点已在 defer（兜底使用）才清除
                                with self._lock:
                                    self._on_success(
                                        tgt_ep, clear_defer=defer_at_request > 0, group=group,
                                        expected_route_epoch=request_route_epoch,
                                    )
                                yield from gen
                                with self._lock:
                                    self._release_inflight(tgt_ep.id, group)
                        return vision_wrapper(ep, payload, ep_timeout, vision_cands)
                    else:
                        payload["messages"] = self._translate_images_sync(
                            payload["messages"], vision_cands, group,
                            request_id=request_id, request_deadline=request_deadline,
                        )
            
            if tried == 0:
                sys_log(f"{request_tag}收到 API 请求，尝试请求端点 '{self._endpoint_log_label(ep, group, ep_model)}'", "INFO")
            else:
                sys_log(f"{request_tag}重试请求，尝试请求端点 '{self._endpoint_log_label(ep, group, ep_model)}'", "INFO")
            # 上下文长度检查：超过限制时跳过该端点（不冻结、不记失败），轮转到下一个
            if ep.max_context_k > 0:
                estimated = self._estimate_context_tokens(loop_messages)
                if estimated > ep.max_context_k * 1000:
                    sys_log(f"端点 '{self._endpoint_log_label(ep, group)}' 上下文约 {estimated}t 超过限制 {ep.max_context_k}K，跳过", "INFO")
                    tried += 1
                    idx = (idx + 1) % total
                    continue

            force_no_retry = getattr(ep, "priority", 0) == 99
            # 每组最近成功 usage 的 site_id 与当前不同 = 新缓存账户。覆盖同请求
            # 故障轮转、手动切换和恢复回切；只修正本地统计，不改客户端 usage。
            reset_cached_stats = (
                self._should_reset_cached_stats(group, ep.site_id)
                or (previous_attempt_site_id is not None and previous_attempt_site_id != ep.site_id)
            )
            if debug_trace is None:
                result, error = self._try_endpoint(
                    ep, payload, ep_timeout, force_no_retry=force_no_retry,
                    pool_group=group, reset_cached_stats=reset_cached_stats,
                    request_id=request_id, request_deadline=request_deadline,
                )
            else:
                result, error = self._try_endpoint(
                    ep,
                    payload,
                    ep_timeout,
                    force_no_retry=force_no_retry,
                    debug_trace=debug_trace,
                    pool_group=group,
                    reset_cached_stats=reset_cached_stats,
                    request_id=request_id, request_deadline=request_deadline,
                )
            previous_attempt_site_id = ep.site_id
            if debug_trace is not None:
                if result is not None:
                    outcome = "stream_opened" if payload.get("stream") else "success"
                    debug_trace.append({"endpoint": ep.name, "result": outcome})
                else:
                    error_text = str(error)
                    if error_text.startswith("HTTP "):
                        error_kind = error_text.split(":", 1)[0].replace(" ", "_").lower()
                    elif "timeout" in error_text.lower() or "超时" in error_text:
                        error_kind = "timeout"
                    elif "auth error" in error_text:
                        error_kind = "auth"
                    elif "rate-limited" in error_text:
                        error_kind = "rate_limited"
                    elif error_text.startswith("fake-success"):
                        error_kind = "fake_success"
                    else:
                        error_kind = "request_error"
                    if self._classify_client_error(error_text):
                        error_kind = "client_error"
                    debug_trace.append({"endpoint": ep.name, "result": "error", "kind": error_kind})
            if result is not None:
                with self._lock:
                    self._on_success(
                        ep, result, clear_defer=defer_at_request > 0, group=group,
                        expected_route_epoch=request_route_epoch,
                    )
                    # 终极兜底成功 → 滑动刷新锁定窗口（5 分钟无新请求视为任务结束）
                    if getattr(ep, "priority", 0) == 99 and group == self.MAIN_GROUP:
                        self._fallback_lock_until_by_group[group] = time.time() + self._FALLBACK_LOCK_SECONDS
                    if not (payload.get("stream") and isinstance(result, _types.GeneratorType)):
                        # 非流式（或流已由 _try_endpoint 内部消费完）→ 立即释放在途占用；
                        # 流式 generator 的占用由消费方迭代结束时释放（见 stream close 路径）。
                        self._release_inflight(ep.id, group)
                sys_log(f"{request_tag}端点 '{self._endpoint_log_label(ep, group)}' 请求成功", "INFO")
                # 请求耗时 DEBUG（API_POOL_DEBUG / /api/debug 开关控制）
                if _DEBUG_LOGGING:
                    try:
                        _req_elapsed = int((time.time() - _chat_start) * 1000)
                        endpoint_records = [item for item in (debug_trace or []) if item.get("result") in ("success", "stream_opened", "error")]
                        switch_count = sum(
                            1 for previous, current in zip(endpoint_records, endpoint_records[1:])
                            if previous.get("endpoint") != current.get("endpoint")
                        )
                        diagnostic_status = "建立" if payload.get("stream") else "完成"
                        sys_log(
                            f"[DEBUG] 请求诊断{diagnostic_status} elapsed_ms={_req_elapsed} "
                            f"switches={switch_count} "
                            f"attempts={json.dumps(debug_trace, ensure_ascii=False, separators=(',', ':'))}",
                            "INFO",
                        )
                    except Exception:
                        pass
                if return_endpoint: return result, ep
                # 分组池：流式结果包一层释放在途占用的 wrapper（迭代完成/关闭/异常时释放）
                if payload.get("stream") and isinstance(result, _types.GeneratorType):
                    return self._wrap_stream_release(result, ep.id, group)
                return result
            errors.append(f"[{ep.name}] {error}")
            sys_log(f"{request_tag}端点 '{self._endpoint_log_label(ep, group)}' 请求失败: {error}", "ERROR")
            # 敏感词诊断只在 DEBUG 且确实出现相关拦截错误时执行；使用当前失败尝试的 payload，避免共享状态串扰。
            if _DEBUG_LOGGING and "sensitive" in str(error).lower():
                d = self._build_cf_diag(payload)
                sys_log(
                    f"CF-DIAG ep={ep.name} hyphen={d['hyphen']} underscore={d['underscore']} "
                    f"space={d['space']} messages={d['messages']} tools={d['tools']}",
                    "INFO",
                )
                if d.get("hyphen_paths"):
                    sys_log(f"CF-DIAG hyphen_paths={d['hyphen_paths']}", "INFO")
                if d.get("underscore_paths"):
                    sys_log(f"CF-DIAG underscore_paths={d['underscore_paths']}", "INFO")
            # 2026-08-15: 502/503/504 网关级错误 → 跳过候选端点探活，直接重试。
            # 探活(ping max_tokens=3) 无法鉴别网关故障：小请求通过≠真实请求可用，
            # 避免"探活通过→重试超时"空转。超时/连接错误仍走探活（瞬态防误杀）。
            gateway_error = any(f"HTTP {c}" in error for c in ("502", "503", "504"))
            # DeepSeek thinking 严格校验 400（2026-09-10，V4.1-Flash 起观测）：同端点重试
            # 一次。放在冻结/轮转之前：这两类错误是客户端类（不冻结、不计账），且证据显示
            # 相邻请求在同一端点上时通时不通 —— 先原地重试比直接换成别的模型代价小。
            # 2026-09-11 收口：删除原第 2 次「显式关闭 thinking」重试。该 400 校验的是
            # 历史消息形态（旧轮 assistant 的 " " 占位 reasoning_content），请求级
            # thinking=disabled 改不了已发历史 → 结构性无效（42h 窗口内 0 成功），且
            # 该字段会随轮转污染异构端点（GLM 报「不支持关闭思考」）。上限维持请求级 1。
            if self._is_strict_validation_400(error):
                # 前提（硬约束）：本分支只处理 _try_endpoint 在**首包前**返回的失败——流式响应在
                # 返回给 chat() 之前已预读首个业务 chunk，因此此刻客户端尚未收到任何字节，
                # 重试无副作用（不会出现"半截响应 + 重新计费"）。不要在流中途重试。
                strict_label = self._endpoint_log_label(ep, group)
                budget = self._normalize_strict400_retries(getattr(ep, "strict400_retries", 2))
                retry_now = False
                retry_desc = ""
                delay = 0.0
                if strict_validation_retries < 1 and budget >= 1:
                    # 第 1 级：同端点原样重试（带抖动退避）。该类 400 是上游实例级随机故障，
                    # 外部实证同 body 背靠背重放失败率 15–25% → 原样重放本身就有较高命中率。
                    strict_validation_retries = 1
                    delay = self._strict400_backoff(0)
                    retry_now = True
                    retry_desc = f"第 1 级 原样重试（退避 {delay:.2f}s）"
                elif budget >= 2 and not strict_variant_used and attempt_prefix_applied > 0:
                    # 第 2 级：换形态——跳过 tool_call_id_prefix 重写、改回客户端原始 tool_call id。
                    # 仅当本尝试确实应用过前缀重写时才发（否则与刚失败的请求完全相同，白耗上游）。
                    strict_validation_retries = 2
                    strict_variant_used = True
                    skip_prefix_rewrite = True
                    delay = self._strict400_backoff(1)
                    retry_now = True
                    retry_desc = f"第 2 级 改用客户端原始 tool_call id 重试（跳过前缀重写 '{ep_prefix}'，退避 {delay:.2f}s）"
                if retry_now and request_deadline is not None and time.time() + delay >= request_deadline:
                    retry_now = False
                    sys_log(
                        f"{request_tag}端点 '{strict_label}' 命中 DeepSeek 严格校验 400，"
                        f"但请求预算不足以再等 {delay:.2f}s，转为轮转",
                        "WARN",
                    )
                if retry_now:
                    sys_log(f"{request_tag}端点 '{strict_label}' 命中 DeepSeek 严格校验 400，同端点{retry_desc}", "WARN")
                    if delay > 0:
                        time.sleep(delay)
                    # 释放本尝试的在途占用，循环顶部会重新 acquire（否则计数器泄漏）
                    with self._lock:
                        self._release_inflight(ep.id, group)
                    idx = active.index(ep)
                    continue
                if not retry_now:
                    reason = ("重试预算用尽" if strict_validation_retries >= 1
                              else "端点未开启严格校验重试（strict400_retries=0）")
                    sys_log(
                        f"{request_tag}端点 '{strict_label}' 命中 DeepSeek 严格校验 400，{reason}，转为轮转",
                        "WARN",
                    )
            # _try_endpoint 内部已按 max_retries 重试完毕，直接冻结+轮转
            with self._lock:
                # 端点级活跃判定（2026-08-14）：超时类错误但端点在 timeout 窗口内
                # 有成功响应 → 单请求饿死（并发挤压），非端点故障 → 不冻结仅轮转。
                # 窗口=端点自己的 timeout 配置（如 Opencode 60s/Tokenrhythm 90s）。
                skip_cooldown = False
                if "连接/超时错误" in error and ep._last_success_ts > 0:
                    since_success = time.time() - ep._last_success_ts
                    if since_success < ep.timeout:
                        skip_cooldown = True
                        sys_log(f"端点 '{self._endpoint_log_label(ep, group)}' 超时失败但 {since_success:.0f}s 前有成功响应（<timeout {ep.timeout}s），判定单请求饿死，不冻结", "WARN")
                # 客户端类错误（改动二 A′）：不冻结、fail_count 不增、不探活，
                # 同请求内继续轮转其余候选；全部候选同类失败时按现状把错误返回给客户端。
                client_error = self._classify_client_error(error)
                if client_error and not role_downgraded and self._has_developer_role(messages):
                    if self._is_role_reject_error(error):
                        role_downgraded = True
                        sys_log(f"{request_tag}端点 '{self._endpoint_log_label(ep, group)}' 拒绝 developer 角色，后续候选降级为 system 重试", "WARN")
                request_route_epoch = self._rotate(
                    ep, error, skip_cooldown=skip_cooldown,
                    health_impact=not client_error, group=group,
                    expected_route_epoch=request_route_epoch,
                    request_id=request_id,
                )
                with self._lock:
                    self._release_inflight(ep.id, group)
                if client_error:
                    client_error_tried.add(ep.id)
                if skip_cooldown:
                    # 单请求饿死不是 API Pool 的端点故障。不要探活或切换到
                    # 其他模型；交回 Hermes，由其现有请求重试机制善后。
                    break
                active = [e for e in self._failover_endpoints() if group in self._ep_groups(e)]
                active.sort(key=lambda e: self._ep_priority(e, group))
                total = len(active)
                if total == 0:
                    break
                tried = 0  # 轮转后重置尝试计数，用刷新后的 total 重新计算
                # 从失败端点之后的环形顺序选择候选（同模型优先分桶已移除，纯优先级顺延）。
                candidates = self._ordered_failover_candidates(ep, active, group=group)
                if client_error:
                    # 客户端类错误不冻结端点，冷却机制不会收缩 total；
                    # 排除本请求内已试过的端点，全部试完即终止轮转。
                    candidates = [e for e in candidates if e.id not in client_error_tried]
                next_ep = candidates[0] if candidates else None
                if next_ep is not None:
                    idx = active.index(next_ep)
                if next_ep is None:
                    for i, e in enumerate(active):
                        if e is ep:
                            idx = (i + 1) % len(active)
                            break
                else:
                    if gateway_error or client_error:
                        # 网关错误：跳过候选探活直接重试（探活小请求无法鉴别网关故障）
                        # 客户端类错误：探活必然通过（小请求不带畸形 payload），无信息量，直接轮转
                        sys_log(f"跳过候选端点 '{self._endpoint_log_label(next_ep, group)}' 探活直接重试", "INFO")
                        continue
                    # 对候选端点做探活
                    sys_log(f"对候选端点 '{self._endpoint_log_label(next_ep, group)}' 进行探活...", "INFO")
                    probe_ok, probe_error = self._probe_endpoint(next_ep)
                    if probe_ok is None:
                        # 无可用身份：探活无信息量（同网关错误处理），跳过探活直接用它试一次
                        sys_log(f"候选端点 '{self._endpoint_log_label(next_ep, group)}' {POOL_IDENTITY_SKIP_REASON}，跳过探活直接重试", "WARN")
                        continue
                    if probe_ok:
                        sys_log(f"候选端点 '{self._endpoint_log_label(next_ep, group)}' 探活通过，准备重试请求", "INFO")
                        continue  # 探活通过，回到循环顶部用 next_ep 发起实际请求
                    else:
                        sys_log(f"候选端点 '{self._endpoint_log_label(next_ep, group)}' 探活失败，跳过", "WARN")
                        self._rotate(next_ep, probe_error or "探活失败", probe_failed=True, group=group)
                        # 下一级探活也失败 → 并发探活剩余所有候选端点。
                        # 用 _check_one_health（含 Attempt 2 重试，最坏 20s）做参考性探测；
                        # 两阶段等待：11s 内返回结果的端点直接按优先级使用，最多等 21s。
                        remaining = [e for e in candidates if e is not next_ep]
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
                                            probe_results[_e.id] = _health
                                        except Exception:
                                            probe_results[_e.id] = "bad"
                                    for _e in remaining:
                                        if probe_results.get(_e.id) in ("ok", "slow") and _e is not ep:
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
                                                probe_results[_e.id] = _health
                                            except Exception:
                                                probe_results[_e.id] = "bad"
                                    for _e in remaining:
                                        if probe_results.get(_e.id) in ("ok", "slow") and _e is not ep:
                                            chosen = _e
                                            break
                            if chosen is not None:
                                sys_log(f"候选端点 '{chosen.name}' 并发探活通过，准备重试请求", "INFO")
                                idx = active.index(chosen)
                                # 其余探活失败的端点标记短冷却，避免后续轮转再撞上
                                for e in remaining:
                                    # Only an explicit completed failure is evidence
                                    # for cooldown. Missing results mean the probe
                                    # exceeded the observation window, not that it failed.
                                    if e is not chosen and probe_results.get(e.id) == "bad" and not self._is_in_cooldown(e):
                                        self._rotate(e, "并发探活失败", probe_failed=True, group=group)
                                continue
                            else:
                                # 全部探活失败：全部短冷却，回到循环顶部走 fallback 兜底
                                sys_log(f"剩余候选端点全部探活失败", "WARN")
                                for e in remaining:
                                    if probe_results.get(e.id) == "bad" and not self._is_in_cooldown(e):
                                        self._rotate(e, "并发探活失败", probe_failed=True, group=group)
                                active = [e for e in self._failover_endpoints() if group in self._ep_groups(e)]
                                active.sort(key=lambda e: self._ep_priority(e, group))
                                total = len(active)
                                if total == 0:
                                    break
                                candidates = self._ordered_failover_candidates(
                                    ep, active, exclude=(next_ep,), group=group
                                )
                                next_ep2 = candidates[0] if candidates else None
                                if next_ep2 is not None:
                                    idx = active.index(next_ep2)
                                if next_ep2 is None:
                                    # 没有更多候选了，尝试任何非当前端点
                                    for i, e in enumerate(active):
                                        if e is ep:
                                            idx = (i + 1) % len(active)
                                            break
                tried += 1
        # bg → main 轮转耗尽兜底（触发点 2）：bg 组内轮转完仍失败 → 追加 main 组一轮；
        # 再失败才报 AllEndpointsFailed 交 Hermes。530s 预算跨组共享不翻倍（剩余预算内执行）。
        # 仅当请求原生属于非 main 组且尚未 fallback 过（入口触发点 1 会改写 group）。
        if group != self.MAIN_GROUP and not group_fallback_used and errors:
            sys_log(f"组 '{group}' 轮转耗尽仍失败，fallback 到 main 组追加一轮", "WARN")
            # A0：耗尽 fallback 同样建立组级延迟回切锁（+ 标记待回切）
            with self._lock:
                self._group_fallback_lock_until[group] = time.time() + self._GROUP_FALLBACK_RETURN_SECONDS
                self._group_fallback_pending[group] = True
            try:
                return self.chat(messages, model=None, extra_payload=extra_payload,
                                 timeout=timeout, return_endpoint=return_endpoint,
                                 request_id=request_id)
            except (AllEndpointsFailed, ValueError):
                # main 组也失败：合并两组错误原样上报（errors 已含本组失败详情）
                pass
        if _DEBUG_LOGGING:
            try:
                _fail_elapsed = int((time.time() - _chat_start) * 1000)
                endpoint_records = [item for item in (debug_trace or []) if item.get("result") == "error"]
                switch_count = sum(
                    1 for previous, current in zip(endpoint_records, endpoint_records[1:])
                    if previous.get("endpoint") != current.get("endpoint")
                )
                sys_log(
                    f"[DEBUG] 请求诊断失败 elapsed_ms={_fail_elapsed} "
                    f"switches={switch_count} "
                    f"attempts={json.dumps(debug_trace, ensure_ascii=False, separators=(',', ':'))}",
                    "INFO",
                )
            except Exception:
                pass
        raise AllEndpointsFailed(errors)

    def _cf_probe_count(self, payload, needle):
        """统计 payload 内所有字符串值中出现 needle 的次数（不含原文，仅计数）。"""
        def walk(node):
            c = 0
            if isinstance(node, str):
                return node.count(needle)
            if isinstance(node, dict):
                for v in node.values():
                    c += walk(v)
            elif isinstance(node, list):
                for it in node:
                    c += walk(it)
            return c
        return walk(payload)

    def _cf_probe_paths(self, payload, needle, limit=20):
        """返回命中 needle 的字段路径列表（不含原文）。格式: messages[3].content 等。"""
        out = []

        def walk(node, path):
            if len(out) >= limit:
                return
            if isinstance(node, str):
                if needle in node:
                    out.append(path)
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{path}.{k}" if path else str(k))
            elif isinstance(node, list):
                for i, it in enumerate(node):
                    walk(it, f"{path}[{i}]" if path else f"[{i}]")
        walk(payload, "")
        return out

    def _build_cf_diag(self, payload):
        """按需生成当前失败请求的敏感词诊断，不在正常热路径执行。"""
        hyp = "role" + "-" + "play"
        und = "role" + "_" + "play"
        spc = "role" + " " + "play"
        return {
            "hyphen": self._cf_probe_count(payload, hyp),
            "underscore": self._cf_probe_count(payload, und),
            "space": self._cf_probe_count(payload, spc),
            "messages": len(payload.get("messages", [])) if isinstance(payload.get("messages"), list) else -1,
            "tools": len(payload.get("tools", [])) if isinstance(payload.get("tools"), list) else -1,
            "hyphen_paths": self._cf_probe_paths(payload, hyp),
            "underscore_paths": self._cf_probe_paths(payload, und),
        }

    def _try_endpoint(
        self, ep, payload, timeout, log_usage=True, force_no_retry=False,
        is_probe=False, stream_stall_retry_used=False, debug_trace=None,
        pool_group=None, reset_cached_stats=False, request_id=None, request_deadline=None,
    ):
        req_t0 = time.time()
        endpoint_log_label = self._endpoint_log_label(ep, pool_group)
        request_tag = f"[req={request_id}] " if request_id else ""
        prompt_text_to_log = extract_prompt_text(payload) if log_usage and not ep.name.startswith("test_") else ""
        
        # 协议层处理：Anthropic 端点做完整格式转换以保证 Kcne 缓存 key 一致性
        is_anthropic = (getattr(ep, "protocol", "openai") == "anthropic")
        is_responses = (getattr(ep, "protocol", "openai") == "responses")
        is_gemini = (getattr(ep, "protocol", "openai") == "gemini")
        if is_gemini:
            # Gemini 原生方言：模型名走 URL 路径，body 为 contents/systemInstruction/generationConfig
            url = _gemini_url(ep.base_url, payload.get("model") or ep.model, stream=bool(payload.get("stream")))
            data = json.dumps(
                _gemini_payload_from_chat(payload, ep),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        elif is_responses:
            url = ep.base_url.rstrip("/") + "/responses"
            responses_payload = _responses_body_from_chat(payload)
            responses_payload.setdefault("model", ep.model)
            responses_payload.pop("response_format", None)
            data = json.dumps(responses_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        elif is_anthropic:
            url = ep.base_url.rstrip("/") + "/messages"
            anthropic_payload = {
                "model": payload.get("model", ep.model),
                "max_tokens": payload.get("max_tokens", 4096),
            }
            if "temperature" in payload: anthropic_payload["temperature"] = payload["temperature"]
            if "top_p" in payload: anthropic_payload["top_p"] = payload["top_p"]
            if "stream" in payload: anthropic_payload["stream"] = payload["stream"]
            if "thinking" in payload: anthropic_payload["thinking"] = payload["thinking"]
            sys_prompt = ""
            messages = []
            for m in payload.get("messages", []):
                role = m.get("role")
                content = m.get("content")
                if role == "system":
                    sys_prompt += content or ""
                    sys_prompt += "\n"
                    continue
                if role == "tool":
                    tool_result = {
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": content if content is not None else "",
                    }
                    messages.append({"role": "user", "content": [tool_result]})
                    continue
                if role == "assistant" and m.get("tool_calls"):
                    blocks = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tool_call in m.get("tool_calls", []):
                        function = tool_call.get("function") or {}
                        try:
                            tool_input = json.loads(function.get("arguments") or "{}")
                        except (TypeError, ValueError):
                            tool_input = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tool_call.get("id", ""),
                            "name": function.get("name", ""),
                            "input": tool_input,
                        })
                    messages.append({"role": "assistant", "content": blocks})
                    continue
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
            # tools 转换：OpenAI → Anthropic 格式
            anthropic_tools = _anthropic_tools_from_chat(payload.get("tools"))
            if anthropic_tools:
                anthropic_payload["tools"] = anthropic_tools
                tool_choice = _anthropic_tool_choice_from_chat(payload.get("tool_choice"))
                if tool_choice:
                    anthropic_payload["tool_choice"] = tool_choice
            # stop_sequences 转换
            if "stop" in payload:
                stop = payload["stop"]
                anthropic_payload["stop_sequences"] = stop if isinstance(stop, list) else [stop]
            # user metadata
            if "user" in payload:
                anthropic_payload["metadata"] = {"user_id": str(payload["user"])}
            # Anthropic prompt caching：使用显式块级断点（block-level cache_control）
            # 2026-08-22 实测：ps.air-outer.com 顶层 cache_control 被无视，
            # 只认消息块级显式 breakpoint（create=4564, read=4564 验证通过）
            # 给 system 和最后一条消息的最后文本块加断点
            if "system" in anthropic_payload and isinstance(anthropic_payload["system"], str):
                s = anthropic_payload["system"].strip()
                if s:
                    anthropic_payload["system"] = [{"type": "text", "text": s, "cache_control": {"type": "ephemeral"}}]
            if anthropic_payload.get("messages"):
                last_msg = anthropic_payload["messages"][-1]
                content = last_msg.get("content")
                if isinstance(content, str):
                    anthropic_payload["messages"][-1] = {
                        "role": last_msg["role"],
                        "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                    }
                elif isinstance(content, list) and content:
                    last_block = content[-1]
                    if isinstance(last_block, dict) and last_block.get("type") == "text":
                        last_block["cache_control"] = {"type": "ephemeral"}
            data = json.dumps(
                anthropic_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        elif not (is_responses or is_gemini):
            url = ep.base_url.rstrip("/") + "/chat/completions"
            data = json.dumps(payload).encode("utf-8")
            
        is_stream = payload.get("stream", False)
        retries = 0 if force_no_retry else ep.max_retries
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            safe_api_key = ep.api_key.encode('ascii', 'ignore').decode('ascii').strip()
            if is_anthropic:
                req.add_header("x-api-key", safe_api_key)
                req.add_header("anthropic-version", "2023-06-01")
            if is_gemini:
                req.add_header("x-goog-api-key", safe_api_key)
            req.add_header("Authorization", f"Bearer {safe_api_key}")
            # 出站客户端特征（2026-09-11 透明网关；2026-09-12 池自身身份确定性）：
            # 有客户端上下文（代理路径）→ 端点 profile，否则忠实透传当前请求的入站头；
            # 无客户端上下文（探活/测试/拉模型/健康检测/管理接口触发的测试）→ 确定性身份，
            # 解析不到则抛 PoolIdentityUnavailable 跳过本次出站（绝不编造身份）。
            # 合并优先级 客户端特征 < default_headers < extra_headers
            if getattr(_client_ctx, "headers", None):
                outbound_headers = resolve_outbound_headers(
                    getattr(ep, "client_profile", ""), self._client_profiles, label=f"端点 '{ep.name}'"
                )
            else:
                outbound_headers, _identity_source = self._pool_identity_headers(ep, label=f"端点 '{ep.name}'")
            for k, v in outbound_headers.items():
                req.add_header(k, v)
            for k, v in ep.default_headers.items():
                req.add_header(k, v)

            for k, v in ep.extra_headers.items():
                req.add_header(k, v)
                
            try:
                # 超时语义区分（2026-08-15 超时体系重构）：
                # - 流式：timeout=ep.timeout 是 TTFB（等响应头/首包），首包后由 stall/max_duration 管控
                # - 非流式：上游必须全量生成完才返回，60/90s TTFB 必然误杀大请求。
                #   总时长语义放宽到 max(ep.timeout, 600)，与 Hermes 侧 stale watchdog(600s) 对齐，
                #   超时由 Hermes 侧统一判定，避免双层 90s 叠加空转。
                _open_timeout = timeout or ep.timeout
                if not is_stream and not is_probe:
                    _open_timeout = max(_open_timeout, 600)
                if request_deadline is not None:
                    remaining_budget = request_deadline - time.time()
                    if remaining_budget <= 0:
                        return None, "request budget exhausted before upstream call"
                    _open_timeout = min(_open_timeout, max(0.1, remaining_budget))
                if getattr(ep, "use_proxy", True) is False:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    resp = opener.open(req, timeout=_open_timeout)
                else:
                    resp = urllib.request.urlopen(req, timeout=_open_timeout)

                # 响应解压（2026-09-10 客户端伪装）：仅当上游声明 Content-Encoding: gzip/deflate 时解压，
                # 其余（含未知编码）一律透传；解压失败向上抛（按上游失败处理），绝不吐损坏字节。
                resp = _DecodedResponse(resp)

                if is_stream:
                    # 在 generator 返回给 chat() 前预读到首个有效业务 chunk。
                    # 这样首包前的 SSE 业务错误仍处于 chat() 的重试/轮转上下文内。
                    prefetched_lines = []
                    pending_error_event = False
                    _first_pkt_timeout = getattr(ep, "stream_first_packet_timeout", 0)
                    _sock1 = _get_resp_socket(resp)
                    if _first_pkt_timeout > 0:
                        if _sock1 is not None:
                            try:
                                _sock1.settimeout(_first_pkt_timeout)
                            except Exception as e:
                                sys_log(f"{request_tag}端点 '{endpoint_log_label}' 首包预读 socket 不可用({e})，依赖请求超时/总时长兜底", "WARN")
                        else:
                            # 2026-08-15: _get_resp_socket 失败时无法设 socket 超时，
                            # 只能依赖 urllib 的 timeout 参数（=ep.timeout），显式记录避免排障盲区
                            sys_log(f"{request_tag}端点 '{endpoint_log_label}' 首包预读未取得 socket，依赖 urllib timeout({timeout or ep.timeout}s) 兜底", "WARN")

                    def _stream_error(chunk, event_error=False):
                        if not isinstance(chunk, dict):
                            return "invalid stream error event" if event_error else ""
                        error = chunk.get("error")
                        error_type = ""
                        message = ""
                        if isinstance(error, dict):
                            error_type = str(error.get("type") or "")
                            message = str(error.get("message") or error.get("detail") or "")
                        elif error is not None:
                            message = str(error)
                        chunk_type = str(chunk.get("type") or "")
                        if event_error or chunk_type == "error" or error is not None or error_type == "upstream_error":
                            detail = ": ".join(part for part in (error_type or chunk_type, message) if part)
                            return detail or "upstream stream error"
                        return ""

                    def _is_business_chunk(chunk):
                        if not isinstance(chunk, dict):
                            return False
                        if is_gemini:
                            # 首包判定：只有 parts 里的业务增量算（与 openai 分支同语义：
                            # 只有空收尾帧的流仍按「无业务首包」失败）。
                            # 例外：整轮被拦截时上游只回 promptFeedback.blockReason（无
                            # candidates），那是有效终局，不能当 502 触发端点轮转。
                            _g_pf = chunk.get("promptFeedback")
                            if isinstance(_g_pf, dict) and _g_pf.get("blockReason"):
                                return True
                            return any(p.get("text") or p.get("functionCall") for p in _gemini_chunk_parts(chunk))
                        if is_responses and str(chunk.get("type", "")).startswith("response."):
                            return chunk.get("type") in ("response.output_text.delta", "response.output_item.added", "response.function_call_arguments.delta", "response.completed")
                        if is_anthropic:
                            ctype = chunk.get("type")
                            delta = chunk.get("delta") or {}
                            if ctype == "content_block_delta":
                                return bool(delta.get("text") or delta.get("thinking") or delta.get("partial_json"))
                            if ctype == "content_block_start":
                                return (chunk.get("content_block") or {}).get("type") == "tool_use"
                            return False
                        choices = chunk.get("choices") or []
                        if not choices:
                            return False
                        delta = choices[0].get("delta") or {}
                        return bool(delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"))

                    try:
                        # 只读上游、逐行前进。bounded give-up：长时间只有心跳/空 delta
                        # 而无业务首包时，不在此无限等待，把控制交还 generator 的既有
                        # 停滞机制（_timeout_abort 内部重试/可见错误），保持 2026-08-22
                        # 起冻结解耦语义；预读行会原样重放，不丢帧。
                        _stall_budget = getattr(ep, "stream_stall_timeout", 0)
                        _max_budget = getattr(ep, "stream_max_duration", 0)
                        _gate_elapsed_stall = False
                        _gate_elapsed_maxdur = False
                        _gate_stall_deadline = time.time() + _stall_budget if _stall_budget > 0 else None
                        _gate_max_deadline = time.time() + _max_budget if _max_budget > 0 else None
                        _gate_max_lines = 200
                        while True:
                            if _gate_stall_deadline is not None and time.time() > _gate_stall_deadline:
                                _gate_elapsed_stall = True
                                break
                            if _gate_max_deadline is not None and time.time() > _gate_max_deadline:
                                _gate_elapsed_maxdur = True
                                break
                            if len(prefetched_lines) >= _gate_max_lines:
                                break
                            line = resp.readline()
                            if not line:
                                resp.close()
                                return None, "HTTP 502: upstream stream ended before first business chunk"
                            prefetched_lines.append(line)
                            stripped = line.strip()
                            if not stripped or stripped.startswith(b":"):
                                continue
                            if stripped.lower().startswith(b"event:"):
                                pending_error_event = stripped[6:].strip().lower() == b"error"
                                continue
                            if not stripped.startswith(b"data:"):
                                continue
                            raw_data = stripped[5:].strip()
                            if not raw_data:
                                continue
                            if raw_data == b"[DONE]":
                                resp.close()
                                return None, "HTTP 502: upstream stream ended before first business chunk"
                            try:
                                chunk = json.loads(raw_data.decode("utf-8"))
                            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                                resp.close()
                                return None, f"HTTP 502: invalid upstream SSE before first business chunk: {e}"
                            stream_error = _stream_error(chunk, pending_error_event)
                            pending_error_event = False
                            if stream_error:
                                resp.close()
                                return None, f"HTTP 502: upstream stream error: {stream_error}"
                            if _is_business_chunk(chunk):
                                break
                    except socket.timeout:
                        _timeout_label = f"{_first_pkt_timeout}s" if _first_pkt_timeout > 0 else f"{timeout or ep.timeout}s(urllib)"
                        sys_log(f"{request_tag}端点 '{endpoint_log_label}' 流式首包超时（{_timeout_label}，第 {attempt+1} 次）", "WARN")
                        try:
                            resp.close()
                        except Exception:
                            pass
                        if attempt < retries:
                            if debug_trace is not None:
                                debug_trace.append({"endpoint": ep.name, "result": "retry", "attempt": attempt + 1, "kind": "stream_first_packet_timeout"})
                            retry_delay = 3 * (2 ** attempt)
                            if request_deadline is not None and time.time() + retry_delay >= request_deadline:
                                return None, f"stream first packet timeout ({_first_pkt_timeout}s; retry skipped: request budget exhausted)"
                            sys_log(f"{request_tag}端点 '{endpoint_log_label}' {retry_delay} 秒后进行第 {attempt+1}/{retries} 次原端点重试（流式首条数据超时）", "INFO")
                            time.sleep(retry_delay)
                            continue
                        return None, f"stream first packet timeout ({_first_pkt_timeout}s)"
                    except Exception as e:
                        try:
                            resp.close()
                        except Exception:
                            pass
                        return None, f"HTTP 502: upstream stream pre-read failed: {type(e).__name__}: {e}"

                    def stream_generator():
                        stream_id = f"chatcmpl-{int(time.time()*1000)}"
                        final_prompt_tokens = 0
                        final_completion_tokens = 0
                        final_total_tokens = 0
                        final_cached_tokens = 0
                        final_reasoning_tokens = None
                        has_usage = False
                        final_completion_text = ""
                        final_reasoning_text = ""
                        gemini_finish_raw = ""
                        gemini_has_tool_calls = False
                        gemini_tool_index = 0
                        gemini_block_reason = ""
                        anthropic_tool_blocks = {}
                        anthropic_stop_reason = None
                        anthropic_message_stopped = False
                        responses_event = ""
                        responses_reasoning_seen = False
                        responses_tool_states = {}

                        def finish_chunk(reason):
                            return b"data: " + json.dumps({
                                "id": stream_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": ep.model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]
                            }).encode("utf-8") + b"\n\n"

                        def delta_chunk(delta):
                            return b"data: " + json.dumps({
                                "id": stream_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": ep.model,
                                "choices": [{"index": 0, "delta": delta, "finish_reason": None}]
                            }).encode("utf-8") + b"\n\n"

                        stall_timeout = getattr(ep, "stream_stall_timeout", 0)
                        _sock2 = _get_resp_socket(resp)
                        if _sock2 is not None and stall_timeout > 0:
                            try:
                                _sock2.settimeout(stall_timeout)
                            except Exception as e:
                                sys_log(f"{request_tag}端点 '{endpoint_log_label}' 设置 stream_stall_timeout 失败({e})，依赖 stream_max_duration 兜底", "WARN")

                        def _timeout_abort(reason):
                            """Handle an upstream stream stall without treating it as endpoint failure."""
                            has_output = bool(final_completion_text.strip() or final_reasoning_text.strip())
                            sys_log(f"{request_tag}端点 '{endpoint_log_label}' {reason}（流式事务失败，不冻结端点）", "ERROR")
                            if not has_output and not stream_stall_retry_used:
                                # Before any downstream bytes were emitted, retry the
                                # same upstream endpoint inside API Pool. Hermes must
                                # not see an internal upstream stall as its own failure.
                                try:
                                    resp.close()
                                except Exception:
                                    pass
                                retry_result, retry_error = self._try_endpoint(
                                    ep, payload, timeout, log_usage=log_usage,
                                    force_no_retry=True, is_probe=is_probe,
                                    stream_stall_retry_used=True,
                                    pool_group=pool_group, request_id=request_id,
                                    request_deadline=request_deadline,
                                )
                                if retry_result is not None:
                                    yield from retry_result
                                    return
                                sys_log(
                                    f"{request_tag}端点 '{endpoint_log_label}' 流式停滞后原端点内部重试失败: {retry_error}",
                                    "WARN",
                                )
                            if has_output:
                                # The partial response is already visible to Hermes;
                                # do not replay it and create duplicated text. Stall and
                                # duration-budget aborts both leave a truncated answer, so
                                # the finish must stay an error: Hermes converts an error
                                # finish after partial delivery into a length-truncated
                                # stub and runs its continuation path. A silent "stop"
                                # would end the turn as if the answer were complete.
                                visible_reason = reason if reason.startswith(_TRUNCATED_STREAM_REASONS) else ""
                                yield b'data: ' + json.dumps({
                                    "choices": [{
                                        "delta": {"content": f"\n\n[API Pool Error: {visible_reason}，输出已截断]"} if visible_reason else {},
                                        "finish_reason": "error" if visible_reason else "stop",
                                    }],
                                }, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
                            else:
                                yield b'data: ' + json.dumps({
                                    "choices": [{
                                        "delta": {"content": f"\n\n[API Pool Error: {reason}]"},
                                        "finish_reason": "error",
                                    }],
                                }, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
                            yield b"data: [DONE]\n\n"

                        # 流总时长上限：循环内绝对时间检查。
                        # 说明（2026-08-13）：真实停滞场景是 SSE keep-alive（带换行注释行）→
                        # readline 每行返回、循环体能执行 → 此检查生效；曾试过 watchdog 线程
                        # 方案，但 http.client 的 readline 是 C 层循环（持 GIL），watchdog 被
                        # 饿死不可靠，已废弃。完全无数据场景由 stall 超时（socket）兜底。
                        stream_deadline = (
                            time.time() - 1
                            if _gate_elapsed_maxdur
                            else (time.time() + ep.stream_max_duration if ep.stream_max_duration > 0 else None)
                        )
                        # 业务增量停滞检测（2026-08-31 B1）：socket 停滞超时只能拦「无网络数据」；
                        # SSE keep-alive 注释行/空行/空 delta 持续有字节到达，会给坏死的流续命。
                        # 仅 content / reasoning / tool 增量或结束事件刷新活动时间；心跳类字节
                        # 不刷新。超过 stream_stall_timeout 无有效业务增量 → 按停滞处置（复用
                        # _timeout_abort：无输出原端点内部重试一次，已有输出不重放、不冻结）。
                        # 0 = 禁用（与 socket stall 超时语义一致）。
                        business_stall_deadline = (
                            time.time() - 1
                            if (_gate_elapsed_stall and stall_timeout > 0)
                            else ((time.time() + stall_timeout) if stall_timeout > 0 else None)
                        )

                        def _business_activity(chunk, is_anthropic_chunk):
                            """该 chunk 是否携带有效业务增量（刷新停滞时钟）。"""
                            try:
                                if is_gemini:
                                    # parts 增量 / finishReason / usageMetadata / 拦截终局都算活动：
                                    # 只带 usage 的收尾帧不得被判为「无业务数据」。
                                    if any(p.get("text") or p.get("functionCall") for p in _gemini_chunk_parts(chunk)):
                                        return True
                                    if not isinstance(chunk, dict):
                                        return False
                                    if chunk.get("usageMetadata"):
                                        return True
                                    _g_pf = chunk.get("promptFeedback")
                                    if isinstance(_g_pf, dict) and _g_pf.get("blockReason"):
                                        return True
                                    _g_cands = chunk.get("candidates") or []
                                    return bool(_g_cands and isinstance(_g_cands[0], dict) and _g_cands[0].get("finishReason"))
                                if not is_anthropic_chunk:
                                    choices = chunk.get("choices") or []
                                    if choices:
                                        delta = choices[0].get("delta") or {}
                                        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
                                            return True
                                        if choices[0].get("finish_reason"):
                                            return True
                                    return bool(chunk.get("usage"))
                                ctype = chunk.get("type")
                                if ctype in ("content_block_delta", "content_block_start", "content_block_stop",
                                             "message_stop", "message_delta"):
                                    delta = chunk.get("delta") or {}
                                    if ctype == "content_block_delta" and delta.get("type") == "text_delta":
                                        return bool(delta.get("text"))
                                    if ctype == "message_delta":
                                        # stop_reason/usage 收尾事件 = 结束信号，刷新
                                        return True
                                    return True
                                if ctype == "message_start":
                                    return bool((chunk.get("message") or {}).get("usage"))
                                return False
                            except Exception:
                                return False
                        try:
                            # 预读阶段已同步消费首个业务 chunk 前的所有行；把它们
                            # 原样重放给下游，再继续消费上游剩余流，避免首包丢失。
                            lines = itertools.chain(prefetched_lines, resp)
                            for line in lines:
                                if stream_deadline is not None and time.time() > stream_deadline:
                                    yield from _timeout_abort(f"流式总时长超限（非停滞，{ep.stream_max_duration}s）")
                                    return
                                if business_stall_deadline is not None and time.time() > business_stall_deadline:
                                    yield from _timeout_abort(f"流式无有效业务增量停滞（连续 {stall_timeout}s，心跳/空行不计）")
                                    return
                                if is_responses:
                                    if not line.strip():
                                        continue
                                    if line.startswith(b"event: "):
                                        responses_event = line[7:].strip().decode("utf-8", errors="ignore")
                                        continue
                                    if not line.startswith(b"data: ") or line.startswith(b"data: [DONE]"):
                                        continue
                                    try:
                                        chunk = json.loads(line[6:].decode("utf-8"))
                                        ctype = chunk.get("type") or responses_event
                                        if ctype == "response.output_text.delta":
                                            final_completion_text += chunk.get("delta", "")
                                            yield b"data: " + json.dumps({"choices": [{"delta": {"content": chunk.get("delta", "")}, "finish_reason": None}]}).encode() + b"\n\n"
                                        elif ctype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                                            # Responses 的推理文本（需请求体带 reasoning.summary）→ chat 的 reasoning_content。
                                            rdelta = chunk.get("delta", "") or ""
                                            if rdelta:
                                                responses_reasoning_seen = True
                                                final_reasoning_text += rdelta
                                                yield b"data: " + json.dumps({"choices": [{"delta": {"reasoning_content": rdelta}, "finish_reason": None}]}).encode() + b"\n\n"
                                        elif ctype == "response.output_item.done":
                                            # 兜底：部分上游不逐字下发摘要，只在 item 收尾时带完整 summary。
                                            item = chunk.get("item") or {}
                                            if item.get("type") == "reasoning" and not responses_reasoning_seen:
                                                summary_text = "".join(
                                                    p.get("text", "") for p in (item.get("summary") or [])
                                                    if isinstance(p, dict) and p.get("type") == "summary_text")
                                                if summary_text:
                                                    responses_reasoning_seen = True
                                                    final_reasoning_text += summary_text
                                                    yield b"data: " + json.dumps({"choices": [{"delta": {"reasoning_content": summary_text}, "finish_reason": None}]}).encode() + b"\n\n"
                                        elif ctype == "response.output_item.added":
                                            item = chunk.get("item") or {}
                                            if item.get("type") == "function_call":
                                                idx = chunk.get("output_index", len(responses_tool_states))
                                                responses_tool_states[idx] = {"item_id": item.get("id", ""), "call_id": item.get("call_id", ""), "name": item.get("name", ""), "arguments": item.get("arguments", "")}
                                                yield b"data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": idx, "id": item.get("call_id", ""), "type": "function", "function": {"name": item.get("name", ""), "arguments": ""}}]}, "finish_reason": None}]}).encode() + b"\n\n"
                                        elif ctype == "response.function_call_arguments.delta":
                                            idx = chunk.get("output_index", len(responses_tool_states))
                                            state = responses_tool_states.setdefault(idx, {"item_id": chunk.get("item_id", ""), "call_id": "", "name": "", "arguments": ""})
                                            arg_delta = chunk.get("delta", "")
                                            state["arguments"] += arg_delta
                                            yield b"data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": idx, "function": {"arguments": arg_delta}}]}, "finish_reason": None}]}).encode() + b"\n\n"
                                        elif ctype == "response.completed":
                                            usage = (chunk.get("response") or {}).get("usage") or {}
                                            final_prompt_tokens = usage.get("input_tokens", 0) or 0
                                            final_completion_tokens = usage.get("output_tokens", 0) or 0
                                            final_total_tokens = usage.get("total_tokens", 0) or 0
                                            # Responses usage 的缓存与推理明细：不解析则 stats 命中率恒 0
                                            # （ps.air-outer 实际返回 input_tokens_details.cached_tokens）。
                                            _in_details = usage.get("input_tokens_details") or {}
                                            if isinstance(_in_details, dict) and _in_details.get("cached_tokens"):
                                                final_cached_tokens = _in_details.get("cached_tokens") or 0
                                            _out_details = usage.get("output_tokens_details") or {}
                                            if isinstance(_out_details, dict) and _out_details.get("reasoning_tokens") is not None:
                                                final_reasoning_tokens = _out_details.get("reasoning_tokens")
                                            yield finish_chunk("stop")
                                            yield b"data: " + json.dumps({"choices": [], "usage": {
                                                "prompt_tokens": final_prompt_tokens,
                                                "completion_tokens": final_completion_tokens,
                                                "total_tokens": final_total_tokens,
                                                "prompt_tokens_details": {"cached_tokens": final_cached_tokens},
                                                "completion_tokens_details": {"reasoning_tokens": final_reasoning_tokens or 0},
                                            }}).encode() + b"\n\n"
                                    except Exception:
                                        continue
                                    responses_event = ""
                                    continue
                                if is_gemini:
                                    # Gemini 原生 SSE → OpenAI chat.completion.chunk（2026-09-12，T3）
                                    stripped_line = line.strip()
                                    if not stripped_line.startswith(b"data:"):
                                        continue
                                    raw_data = stripped_line[5:].strip()
                                    if not raw_data or raw_data == b"[DONE]":
                                        continue
                                    try:
                                        chunk = json.loads(raw_data.decode("utf-8"))
                                    except (UnicodeDecodeError, json.JSONDecodeError):
                                        continue
                                    if stall_timeout > 0 and _business_activity(chunk, False):
                                        business_stall_deadline = time.time() + stall_timeout
                                    _g_pf = chunk.get("promptFeedback")
                                    if isinstance(_g_pf, dict) and _g_pf.get("blockReason"):
                                        # 整轮被拦截：无 candidates，终局 finish=content_filter
                                        gemini_block_reason = _g_pf.get("blockReason")
                                        sys_log(f"{request_tag}端点 '{endpoint_log_label}' Gemini 流式响应被拦截"
                                                f"（promptFeedback.blockReason={gemini_block_reason}）", "WARN")
                                    _g_err = _stream_error(chunk)
                                    if _g_err:
                                        # 中流错误：与 openai 分支同语义（记录中断），再复用事务失败
                                        # 收尾让错误对下游可见，绝不伪装成正常 stop。
                                        sys_log(f"{request_tag}端点 '{endpoint_log_label}' 流式传输中断/异常: upstream stream error: {_g_err}", "ERROR")
                                        yield from _timeout_abort(f"上游流式错误: {_g_err}")
                                        return
                                    _g_cands = chunk.get("candidates") or []
                                    _g_cand = _g_cands[0] if _g_cands and isinstance(_g_cands[0], dict) else {}
                                    for _g_part in _gemini_chunk_parts(chunk):
                                        if _g_part.get("functionCall") is not None:
                                            _g_fc = _g_part["functionCall"] or {}
                                            _g_args = _g_fc.get("args")
                                            gemini_has_tool_calls = True
                                            # 整段参数一次给全；id 由池生成，签名按 id 登记供续跑回填
                                            yield delta_chunk({"tool_calls": [{
                                                "index": gemini_tool_index,
                                                "id": _gemini_remember_tool_call(
                                                    _g_fc.get("name") or "", _g_part.get("thoughtSignature") or ""),
                                                "type": "function",
                                                "function": {
                                                    "name": _g_fc.get("name") or "",
                                                    "arguments": json.dumps(_g_args if isinstance(_g_args, dict) else {},
                                                                            ensure_ascii=False),
                                                },
                                            }]})
                                            gemini_tool_index += 1
                                        elif _g_part.get("text"):
                                            if _g_part.get("thought"):
                                                final_reasoning_text += _g_part["text"]
                                                yield delta_chunk({"reasoning_content": _g_part["text"]})
                                            else:
                                                final_completion_text += _g_part["text"]
                                                yield delta_chunk({"content": _g_part["text"]})
                                    if _g_cand.get("finishReason"):
                                        gemini_finish_raw = _g_cand.get("finishReason")
                                    _g_meta = chunk.get("usageMetadata")
                                    if isinstance(_g_meta, dict) and _g_meta:
                                        # 写入既有 final_*：finally 的 usage 记账自动生效
                                        _g_usage = _gemini_usage_to_chat_usage(_g_meta)
                                        final_prompt_tokens = _g_usage["prompt_tokens"]
                                        final_completion_tokens = _g_usage["completion_tokens"]
                                        final_total_tokens = _g_usage["total_tokens"]
                                        final_cached_tokens = _g_usage["prompt_tokens_details"]["cached_tokens"]
                                        final_reasoning_tokens = _g_usage["completion_tokens_details"]["reasoning_tokens"]
                                        has_usage = True
                                    continue
                                if is_anthropic:
                                    if line.startswith(b"data: [DONE]"):
                                        continue
                                    try:
                                        chunk = json.loads(line[6:].decode("utf-8"))
                                        if stall_timeout > 0 and _business_activity(chunk, True):
                                            business_stall_deadline = time.time() + stall_timeout
                                        ctype = chunk.get("type")
                                        if ctype == "content_block_start":
                                            block = chunk.get("content_block", {})
                                            if block.get("type") == "tool_use":
                                                idx = chunk.get("index", len(anthropic_tool_blocks))
                                                anthropic_tool_blocks[idx] = {
                                                    "id": block.get("id", ""),
                                                    "name": block.get("name", ""),
                                                    "arguments": ""
                                                }
                                        elif ctype == "content_block_delta":
                                            delta = chunk.get("delta", {})
                                            if delta.get("type") == "input_json_delta":
                                                idx = chunk.get("index", 0)
                                                if idx in anthropic_tool_blocks:
                                                    anthropic_tool_blocks[idx]["arguments"] += delta.get("partial_json", "")
                                            elif delta.get("type") == "thinking_delta":
                                                thinking = delta.get("thinking", "")
                                                final_reasoning_text += thinking
                                                if thinking:
                                                    o_chunk = {
                                                        "id": stream_id,
                                                        "object": "chat.completion.chunk",
                                                        "created": int(time.time()),
                                                        "model": ep.model,
                                                        "choices": [{"index": 0, "delta": {"reasoning_content": thinking}}]
                                                    }
                                                    yield b"data: " + json.dumps(o_chunk).encode("utf-8") + b"\n\n"
                                            else:
                                                text = delta.get("text", "")
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
                                        elif ctype == "content_block_stop":
                                            idx = chunk.get("index", 0)
                                            block = anthropic_tool_blocks.get(idx)
                                            if block:
                                                o_chunk = {
                                                    "id": stream_id,
                                                    "object": "chat.completion.chunk",
                                                    "created": int(time.time()),
                                                    "model": ep.model,
                                                    "choices": [{
                                                        "index": 0,
                                                        "delta": {
                                                            "tool_calls": [{
                                                                "index": idx,
                                                                "id": block["id"],
                                                                "type": "function",
                                                                "function": {
                                                                    "name": block["name"],
                                                                    "arguments": block["arguments"]
                                                                }
                                                            }]
                                                        },
                                                        "finish_reason": "tool_calls"
                                                    }]
                                                }
                                                yield b"data: " + json.dumps(o_chunk).encode("utf-8") + b"\n\n"
                                        elif ctype == "message_stop":
                                            anthropic_message_stopped = True
                                            finish_reason = "tool_calls" if anthropic_tool_blocks else {
                                                "end_turn": "stop",
                                                "stop_sequence": "stop",
                                                "max_tokens": "length",
                                                "tool_use": "tool_calls",
                                            }.get(anthropic_stop_reason, "stop")
                                            yield finish_chunk(finish_reason)
                                            usage_chunk = {
                                                "id": stream_id,
                                                "object": "chat.completion.chunk",
                                                "created": int(time.time()),
                                                "model": ep.model,
                                                "choices": [],
                                                "usage": {
                                                    "prompt_tokens": final_prompt_tokens,
                                                    "completion_tokens": final_completion_tokens,
                                                    "total_tokens": final_total_tokens,
                                                    "prompt_tokens_details": {"cached_tokens": final_cached_tokens} if final_cached_tokens else {}
                                                }
                                            }
                                            yield b"data: " + json.dumps(usage_chunk).encode("utf-8") + b"\n\n"
                                            yield b"data: [DONE]\n\n"
                                        elif ctype == "message_delta" and "usage" in chunk:
                                            u = chunk["usage"]
                                            final_completion_tokens += u.get("output_tokens", 0)
                                            final_total_tokens += u.get("output_tokens", 0)
                                            has_usage = True
                                            if isinstance(chunk.get("delta"), dict) and chunk["delta"].get("stop_reason"):
                                                anthropic_stop_reason = chunk["delta"]["stop_reason"]
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
                                            if stall_timeout > 0 and _business_activity(chunk, False):
                                                business_stall_deadline = time.time() + stall_timeout
                                            if isinstance(chunk, dict) and (chunk.get("type") == "error" or chunk.get("error") is not None):
                                                # 首包后的中流错误：错误帧原样透传给下游，此处只记录中断。
                                                _err = chunk.get("error") or {}
                                                _msg = _err.get("message") if isinstance(_err, dict) else (str(_err) if _err else "")
                                                sys_log(f"{request_tag}端点 '{endpoint_log_label}' 流式传输中断/异常: upstream stream error: {_msg}", "ERROR")
                                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                                delta = chunk["choices"][0].get("delta", {})
                                                if "content" in delta:
                                                    final_completion_text += delta.get("content") or ""
                                            if "usage" in chunk and chunk["usage"]:
                                                u = chunk["usage"]
                                                final_prompt_tokens = u.get("prompt_tokens", 0)
                                                final_completion_tokens = u.get("completion_tokens", 0)
                                                final_total_tokens = u.get("total_tokens", 0)
                                                if "prompt_tokens_details" in u and isinstance(u["prompt_tokens_details"], dict):
                                                    final_cached_tokens = u["prompt_tokens_details"].get("cached_tokens", 0)
                                                details = u.get("completion_tokens_details") or {}
                                                if isinstance(details, dict) and "reasoning_tokens" in details:
                                                    final_reasoning_tokens = details.get("reasoning_tokens")
                                                has_usage = True
                                        except Exception:
                                            pass
                            if is_responses:
                                # Upstream Responses stream is normalized here; the public Responses adapter
                                # consumes the normalized Chat stream below.
                                has_usage = bool(final_total_tokens or final_prompt_tokens or final_completion_tokens)
                                if has_usage:
                                    final_usage = {"input_tokens": final_prompt_tokens, "output_tokens": final_completion_tokens, "total_tokens": final_total_tokens}
                            if is_responses:
                                # Native Responses streams still end with the Chat-compatible DONE marker.
                                yield b"data: [DONE]\n\n"
                            if is_gemini:
                                # Gemini SSE 没有 [DONE]，finishReason/usageMetadata 也只在候选块里
                                # 出现；流末统一补 finish + usage + [DONE]，否则下游等不到结束标记。
                                if gemini_block_reason:
                                    yield finish_chunk("content_filter")
                                elif gemini_finish_raw or gemini_has_tool_calls:
                                    yield finish_chunk(_gemini_finish_reason(gemini_finish_raw, gemini_has_tool_calls))
                                if has_usage:
                                    yield b"data: " + json.dumps({
                                        "id": stream_id,
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": ep.model,
                                        "choices": [],
                                        "usage": {
                                            "prompt_tokens": final_prompt_tokens,
                                            "completion_tokens": final_completion_tokens,
                                            "total_tokens": final_total_tokens,
                                            "prompt_tokens_details": {"cached_tokens": final_cached_tokens},
                                            "completion_tokens_details": {"reasoning_tokens": final_reasoning_tokens or 0},
                                        },
                                    }).encode("utf-8") + b"\n\n"
                                yield b"data: [DONE]\n\n"
                            if is_anthropic and not anthropic_message_stopped:
                                yield from _timeout_abort("Anthropic 流在 message_stop 前提前结束")
                                return
                        except socket.timeout:
                            yield from _timeout_abort(f"流式无新数据停滞（连续 {stall_timeout}s）")
                            return
                        except Exception as e:
                            # 2026-08-15: 原逻辑静默吞掉流内所有异常——23:58:26 假死请求
                            # "收到后无任何日志"的直接原因。区分客户端断开(常见噪音)与上游异常(需记录)。
                            if isinstance(e, (ConnectionResetError, BrokenPipeError)):
                                sys_log(f"{request_tag}端点 '{endpoint_log_label}' 流式响应客户端断开: {type(e).__name__}", "WARN")
                            else:
                                sys_log(f"{request_tag}端点 '{endpoint_log_label}' 流式响应异常: {type(e).__name__}: {e}", "ERROR")
                        finally:
                            if has_usage and log_usage and not ep.name.startswith("test_"):
                                stats_cached_tokens = 0 if reset_cached_stats else final_cached_tokens
                                token_tracker.add_usage(ep.name, ep.model, final_prompt_tokens, final_completion_tokens, final_total_tokens, stats_cached_tokens)
                                chat_logger.add_log(ep.name, ep.model, prompt_text_to_log, final_completion_text.strip() or final_reasoning_text.strip(), final_total_tokens, int((time.time() - req_t0) * 1000), pool_group, final_prompt_tokens, stats_cached_tokens, final_reasoning_tokens)
                                self._mark_cache_stats_account(pool_group or self.MAIN_GROUP, ep.site_id)
                            resp.close()
                    return stream_generator(), ""
                else:
                    body = json.loads(resp.read().decode("utf-8"))
                    if is_gemini:
                        # Gemini 原生响应 → OpenAI chat.completion（2026-09-12，T2）
                        _candidates = body.get("candidates") or []
                        _cand = _candidates[0] if _candidates else {}
                        g_text, g_reasoning, g_tool_calls = _gemini_parts_to_chat(_cand)
                        g_block = (body.get("promptFeedback") or {}).get("blockReason")
                        if g_block:
                            sys_log(f"{request_tag}端点 '{endpoint_log_label}' Gemini 响应被拦截"
                                    f"（promptFeedback.blockReason={g_block}）", "WARN")
                        g_finish = "content_filter" if g_block else _gemini_finish_reason(
                            _cand.get("finishReason"), bool(g_tool_calls))
                        g_message = {"role": "assistant", "content": g_text}
                        if g_reasoning:
                            g_message["reasoning_content"] = g_reasoning
                        if g_tool_calls:
                            g_message["tool_calls"] = g_tool_calls
                        # 假成功检测（仅端点启用时）：与 openai 分支同语义（同端点重试后放弃）
                        if ep.check_fake_success:
                            _g_visible = (g_text or g_reasoning or "").strip()
                            if _g_visible and any(p in _g_visible for p in FAKE_SUCCESS_PATTERNS):
                                sys_log(f"端点 '{endpoint_log_label}' 假成功（内容匹配拒绝模式）", "WARNING")
                                if attempt < retries:
                                    retry_delay = 3 * (2 ** attempt)
                                    if request_deadline is None or time.time() + retry_delay < request_deadline:
                                        sys_log(f"{request_tag}端点 '{endpoint_log_label}' {retry_delay} 秒后进行第 {attempt+1}/{retries} 次原端点重试（假成功）", "INFO")
                                        time.sleep(retry_delay)
                                        continue
                                return None, "fake-success: 内容匹配拒绝模式"
                        g_meta = body.get("usageMetadata")
                        g_usage = _gemini_usage_to_chat_usage(g_meta)
                        if log_usage and not ep.name.startswith("test_") and isinstance(g_meta, dict):
                            stats_cached = 0 if reset_cached_stats else g_usage["prompt_tokens_details"]["cached_tokens"]
                            token_tracker.add_usage(ep.name, ep.model, g_usage["prompt_tokens"],
                                                    g_usage["completion_tokens"], g_usage["total_tokens"], stats_cached)
                            chat_logger.add_log(ep.name, ep.model, prompt_text_to_log,
                                                g_text.strip() or g_reasoning.strip(),
                                                g_usage["total_tokens"], int((time.time() - req_t0) * 1000),
                                                pool_group, g_usage["prompt_tokens"], stats_cached,
                                                g_usage["completion_tokens_details"]["reasoning_tokens"])
                            self._mark_cache_stats_account(pool_group or self.MAIN_GROUP, ep.site_id)
                        return {
                            "id": body.get("responseId") or f"chatcmpl-{int(time.time())}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": body.get("modelVersion") or ep.model,
                            "choices": [{"index": 0, "message": g_message, "finish_reason": g_finish}],
                            "usage": g_usage,
                        }, ""
                    if is_responses:
                        response_text, response_tools, response_reasoning = _responses_output_to_chat_message(body.get("output", []))
                        response_message = {"role": "assistant", "content": response_text}
                        if response_reasoning:
                            response_message["reasoning_content"] = response_reasoning
                        if response_tools:
                            response_message["tool_calls"] = response_tools
                        response_usage = _responses_usage_to_chat_usage(body.get("usage"))
                        if log_usage and not ep.name.startswith("test_"):
                            # 非流式 responses 分支此前直接返回、完全不记账（token_stats 无行）。
                            _cached = (response_usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                            _reasoning = (response_usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                            stats_cached = 0 if reset_cached_stats else _cached
                            token_tracker.add_usage(ep.name, ep.model, response_usage.get("prompt_tokens", 0),
                                                    response_usage.get("completion_tokens", 0),
                                                    response_usage.get("total_tokens", 0), stats_cached)
                            chat_logger.add_log(ep.name, ep.model, prompt_text_to_log, (response_text or "").strip(),
                                                response_usage.get("total_tokens", 0), int((time.time() - req_t0) * 1000),
                                                pool_group, response_usage.get("prompt_tokens", 0), stats_cached, _reasoning)
                            self._mark_cache_stats_account(pool_group or self.MAIN_GROUP, ep.site_id)
                        return {
                            "id": body.get("id", f"chatcmpl-{int(time.time())}"),
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": body.get("model", ep.model),
                            "choices": [{"index": 0, "message": response_message, "finish_reason": "tool_calls" if response_tools else "stop"}],
                            "usage": response_usage,
                        }, ""
                    if is_anthropic:
                        reply = ""
                        reasoning = ""
                        tool_calls = []
                        for c in body.get("content", []):
                            if c.get("type") == "text":
                                reply += c.get("text") or ""
                            elif c.get("type") == "thinking":
                                reasoning += c.get("thinking") or ""
                            elif c.get("type") == "tool_use":
                                tool_calls.append({
                                    "id": c.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": c.get("name", ""),
                                        "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False),
                                    },
                                })
                        u = body.get("usage", {})
                        prompt_t = 0; cached = 0; tot = 0
                        if u:
                            prompt_t = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                            tot = prompt_t + u.get("output_tokens", 0)
                            cached = u.get("cache_read_input_tokens", 0)
                            if log_usage and not ep.name.startswith("test_"):
                                stats_cached = 0 if reset_cached_stats else cached
                                token_tracker.add_usage(ep.name, ep.model, prompt_t, u.get("output_tokens", 0), tot, stats_cached)
                                chat_logger.add_log(ep.name, ep.model, prompt_text_to_log, reply.strip() or reasoning.strip(), tot, int((time.time() - req_t0) * 1000), pool_group, prompt_t, stats_cached)
                                self._mark_cache_stats_account(pool_group or self.MAIN_GROUP, ep.site_id)
                        stop_reason = body.get("stop_reason")
                        finish_reason = {
                            "tool_use": "tool_calls",
                            "max_tokens": "length",
                            "end_turn": "stop",
                            "stop_sequence": "stop",
                        }.get(stop_reason, "stop")
                        message = {
                            "role": "assistant",
                            "content": reply.strip()
                        }
                        if reasoning:
                            message["reasoning_content"] = reasoning
                        if tool_calls:
                            message["tool_calls"] = tool_calls
                        o_body = {
                            "id": f"chatcmpl-{int(time.time())}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": ep.model,
                            "choices": [{
                                "index": 0,
                                "message": message,
                                "finish_reason": finish_reason
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
                                sys_log(f"端点 '{endpoint_log_label}' 假成功（内容匹配拒绝模式）", "WARNING")
                                return None, "fake-success: 内容匹配拒绝模式"
                        return o_body, ""
                    else:
                        u = body.get("usage", {})
                        content = body["choices"][0]["message"].get("content") or ""
                        reasoning = body["choices"][0]["message"].get("reasoning_content") or ""
                        if u:
                            tot = u.get("total_tokens", 0)
                            cached = 0
                            if "prompt_tokens_details" in u and isinstance(u["prompt_tokens_details"], dict):
                                cached = u["prompt_tokens_details"].get("cached_tokens", 0)
                            completion_details = u.get("completion_tokens_details") or {}
                            reasoning_tokens = completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else None
                            if log_usage and not ep.name.startswith("test_"):
                                stats_cached = 0 if reset_cached_stats else cached
                                token_tracker.add_usage(ep.name, ep.model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), tot, stats_cached)
                                log_text = content.strip() or reasoning.strip()
                                chat_logger.add_log(ep.name, ep.model, prompt_text_to_log, log_text, tot, int((time.time() - req_t0) * 1000), pool_group, u.get("prompt_tokens", 0), stats_cached, reasoning_tokens)
                                self._mark_cache_stats_account(pool_group or self.MAIN_GROUP, ep.site_id)
                        # 假成功检测（仅端点启用时）
                        if ep.check_fake_success:
                            _content_text = (content or reasoning or "").strip()
                            if _content_text and any(p in _content_text for p in FAKE_SUCCESS_PATTERNS):
                                sys_log(f"端点 '{endpoint_log_label}' 假成功（内容匹配拒绝模式）", "WARNING")
                                if attempt < retries:
                                    retry_delay = 3 * (2 ** attempt)
                                    if request_deadline is None or time.time() + retry_delay < request_deadline:
                                        sys_log(f"{request_tag}端点 '{endpoint_log_label}' {retry_delay} 秒后进行第 {attempt+1}/{retries} 次原端点重试（假成功）", "INFO")
                                        time.sleep(retry_delay)
                                        continue
                                return None, "fake-success: 内容匹配拒绝模式"
                        return body, ""
                    
                    
            except urllib.error.HTTPError as e:
                err_body = ""
                try: err_body = _DecodedResponse(e).read().decode("utf-8", errors="ignore")[:1000]
                except Exception: pass
                msg = f"HTTP {e.code}: {err_body}"
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    msg += f"; Retry-After: {retry_after}"
                if (e.code == 400 and not force_no_retry
                        and any(k in payload for k in ("temperature", "top_p"))
                        and ("temperature" in err_body or "top_p" in err_body)):
                    cleaned = {k: v for k, v in payload.items() if k not in ("temperature", "top_p")}
                    sys_log(f"\u7aef\u70b9 '{ep.name}' \u4e0d\u652f\u6301 temperature/top_p\uff0c\u5df2\u81ea\u52a8\u79fb\u9664\u540e\u91cd\u8bd5", "WARNING")
                    return self._try_endpoint(
                        ep,
                        cleaned,
                        timeout,
                        log_usage=log_usage,
                        force_no_retry=True,
                        debug_trace=debug_trace,
                        pool_group=pool_group,
                        reset_cached_stats=reset_cached_stats,
                        request_id=request_id,
                        request_deadline=request_deadline,
                    )
                if e.code == 429: return None, msg + " (429 rate-limited)"
                if e.code in (401, 403): return None, msg + " (auth error)"
                if e.code >= 500:
                    if attempt < retries:
                        if debug_trace is not None:
                            debug_trace.append({"endpoint": ep.name, "result": "retry", "attempt": attempt + 1, "kind": f"http_{e.code}"})
                        retry_delay = 3 * (2 ** attempt)
                        if request_deadline is not None and time.time() + retry_delay >= request_deadline:
                            return None, msg + "; retry skipped: request budget exhausted"
                        sys_log(f"{request_tag}端点 '{endpoint_log_label}' {retry_delay} 秒后进行第 {attempt+1}/{retries} 次原端点重试（HTTP {e.code}）", "INFO")
                        time.sleep(retry_delay)
                        continue
                    return None, msg
                return None, msg
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                msg = f"连接/超时错误: {e}"
                if attempt < retries:
                    if debug_trace is not None:
                        debug_trace.append({"endpoint": ep.name, "result": "retry", "attempt": attempt + 1, "kind": "timeout"})
                    # 端点在本次 timeout 窗口内仍有其他成功请求，说明是单请求
                    # 饿死；在重放前止损，避免健康端点重复计算/计费。
                    if ep._last_success_ts > 0 and time.time() - ep._last_success_ts < ep.timeout:
                        return None, msg + "; recent endpoint success, retry skipped"
                    retry_delay = 3 * (2 ** attempt)
                    if request_deadline is not None and time.time() + retry_delay >= request_deadline:
                        return None, msg + "; retry skipped: request budget exhausted"
                    risk = "，请求可能已提交，存在重复计算风险" if any(
                        marker in str(e).lower() for marker in ("write operation timed out", "read timed out", "timed out")
                    ) else ""
                    sys_log(f"{request_tag}端点 '{endpoint_log_label}' {retry_delay} 秒后进行第 {attempt+1}/{retries} 次原端点重试（连接/超时{risk}）", "INFO")
                    time.sleep(retry_delay)
                    continue
                return None, msg
            except Exception as e:
                return None, f"未知错误: {e}"
        return None, "重试次数用尽"

    def fetch_models(self, base_url, api_key, timeout=10, use_proxy=True, protocol="openai", default_headers=None, extra_headers=None, client_profile=""):
        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, method="GET")
        safe_api_key = api_key.encode('ascii', 'ignore').decode('ascii').strip()
        req.add_header("Authorization", f"Bearer {safe_api_key}")
        # 客户端特征与代理路径同源（2026-09-11）；2026-09-12：拉模型属于"池自身出站"，
        # 走确定性身份；解析不到直接抛 PoolIdentityUnavailable（调用方按"跳过"处理，不编造身份）
        identity_headers, _identity_source = self._pool_identity_headers(client_profile, label="拉模型")
        for k, v in identity_headers.items():
            req.add_header(k, v)
        for k, v in (default_headers or {}).items():
            req.add_header(k, v)
        for k, v in (extra_headers or {}).items():
            req.add_header(k, v)
            
        try:
            if not use_proxy:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urllib.request.urlopen(req, timeout=timeout)

            # 响应解压（2026-09-12）：与 _try_endpoint 同源。profile/客户端基线声明
            # Accept-Encoding: gzip, deflate 后上游会真实压缩，拉模型此前未解压，
            # 直接 decode 抛 "'utf-8' codec can't decode byte 0x8b"。
            resp = _DecodedResponse(resp)

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

    @staticmethod
    def _tiny_png_base64():
        """合法 32x32 PNG（base64）。Qwen3-VL 系列要求图片尺寸 > 28px 且 PNG 校验严格；
        旧 1x1 坏图（IDAT checksum 错误）会被 SiliconFlow 400 拒绝。"""
        rows = []
        for y in range(32):
            row = bytearray([0])
            for x in range(32):
                row += bytes([(x * 8) % 256, (y * 8) % 256, 200])
            rows.append(bytes(row))
        raw = b"".join(rows)

        def _chunk(tag, data):
            chunk_body = tag + data
            return struct.pack(">I", len(data)) + chunk_body + struct.pack(">I", zlib.crc32(chunk_body) & 0xffffffff)

        png = (b"\x89PNG\r\n\x1a\n"
               + _chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
               + _chunk(b"IDAT", zlib.compress(raw, 9))
               + _chunk(b"IEND", b""))
        return base64.b64encode(png).decode()

    def test_vision(self, base_url, api_key, model, timeout=15, use_proxy=True, protocol="openai", user_agent="", client_profile=""):
        ep = Endpoint(name="test_vision", base_url=base_url, api_key=api_key, model=model, max_retries=0, use_proxy=use_proxy, protocol=protocol, default_headers={"User-Agent": user_agent} if user_agent else {}, client_profile=client_profile)
        tiny_png = self._tiny_png_base64()
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "describe this image in 3 words"}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_png}"}}]}],
            "max_tokens": self.PROBE_MAX_TOKENS,
        }
        t0 = time.time()
        try:
            reply, err = self._try_endpoint(ep, payload, timeout)
        except PoolIdentityUnavailable as exc:
            return {"ok": False, "supports_vision": False, "skipped": True, "latency_ms": 0, "reply": "", "error": str(exc)}
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

    def test_model_latency(self, base_url, api_key, model, timeout=15, use_proxy=True, protocol="openai", user_agent="", client_profile=""):
        ep = Endpoint(name="test_latency", base_url=base_url, api_key=api_key, model=model, max_retries=0, use_proxy=use_proxy, protocol=protocol, default_headers={"User-Agent": user_agent} if user_agent else {}, client_profile=client_profile)
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": self.PROBE_MAX_TOKENS}
        t0 = time.time()
        try:
            reply, err = self._try_endpoint(ep, payload, timeout)
        except PoolIdentityUnavailable as exc:
            return {"ok": False, "status": "skipped", "skipped": True, "latency_ms": 0, "reply": "", "error": str(exc)}
        latency = int((time.time() - t0) * 1000)
        
        if reply is not None:
            status = "ok" if latency <= LATENCY_OK_MAX else ("slow" if latency <= LATENCY_SLOW_MAX else "bad")
            return {"ok": True, "status": status, "latency_ms": latency, "reply": reply, "error": ""}
        else:
            return {"ok": False, "status": "bad", "latency_ms": latency, "reply": "", "error": err}

CONFIG_FILE = "api_config.json"
RUNTIME_STATE_FILE = "api_runtime_state.json"
_config_lock = threading.Lock()
_runtime_state_lock = threading.Lock()


def load_runtime_state():
    """读取运行态：新格式 {"groups": {grp: ep_id}, "cooldowns": {ep_id: {...}},
    "group_fallback": {counts/locks/fallback_locks}}；兼容旧扁平 {"last_success_endpoint_id"}
    （迁移为 main 组）。返回完整 dict（可能含 cooldowns/group_fallback）；
    旧调用方只取 .get("groups")，语义不变。"""
    try:
        with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        out: dict = {}
        groups = data.get("groups")
        if isinstance(groups, dict) and groups:
            out["groups"] = {str(k): v for k, v in groups.items() if isinstance(v, str) and v}
        else:
            endpoint_id = data.get("last_success_endpoint_id")
            if isinstance(endpoint_id, str) and endpoint_id:
                out["groups"] = {APIPool.MAIN_GROUP: endpoint_id}
        cooldowns = data.get("cooldowns")
        if isinstance(cooldowns, dict) and cooldowns:
            out["cooldowns"] = cooldowns
        fallback = data.get("group_fallback")
        if isinstance(fallback, dict) and fallback:
            out["group_fallback"] = fallback
        return out
    except (OSError, ValueError, TypeError):
        return {}


def save_runtime_state(endpoint_id):
    """旧签名兼容：写入 main 组指针（外部仅 /api/switch-endpoint 使用）。"""
    state = load_runtime_state() or {}
    loaded = state.get("groups") if isinstance(state.get("groups"), dict) else None
    groups_state: dict = dict(loaded) if loaded is not None else {}
    groups_state[APIPool.MAIN_GROUP] = endpoint_id
    return save_runtime_state_groups(groups_state)


def save_runtime_state_groups(groups_state, cooldowns=None, replace_groups=False, fallback=None):
    """保存运行态（2026-09-01 冷却持久化；2026-09-06 组 fallback 状态持久化）：
    - 默认合并模式：更新 groups 指针并保留文件既有 cooldowns / group_fallback
      （手动切换等显式操作语义，传完整 groups 时与旧精确覆盖等价，且不会抹掉冷却态）；
    - replace_groups=True：groups 精确覆盖（SIGTERM 快照全量覆盖、启动残留清理语义），
      避免把将要删除的残留键合并回去。cooldowns / fallback 显式传入时同样精确覆盖。"""
    state = load_runtime_state() or {}
    if not isinstance(state, dict):
        state = {}
    prev_groups = state.get("groups") if isinstance(state.get("groups"), dict) else {}
    prev_cooldowns = state.get("cooldowns") if isinstance(state.get("cooldowns"), dict) else {}
    prev_fallback = state.get("group_fallback") if isinstance(state.get("group_fallback"), dict) else {}
    if replace_groups:
        payload: dict = {"groups": groups_state}
    else:
        payload = {"groups": {**prev_groups, **groups_state}}
    if cooldowns is not None:
        payload["cooldowns"] = cooldowns
    elif prev_cooldowns:
        payload["cooldowns"] = prev_cooldowns
    if fallback is not None:
        payload["group_fallback"] = fallback
    elif prev_fallback:
        payload["group_fallback"] = prev_fallback
    tmp_file = f"{RUNTIME_STATE_FILE}.tmp"
    try:
        with _runtime_state_lock:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, RUNTIME_STATE_FILE)
        return True
    except OSError as exc:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass
        sys_log(f"保存运行态失败，不影响当前请求: {exc}", "WARN")
        return False


def _collect_cooldown_state():
    """收集端点冷却/冻结状态用于快照（2026-09-01 冷却持久化）。

    仅记录有状态价值的端点：冷却中（cooldown_until>now）、余额手动解冻、
    连续失败计数>0（阶梯冷却基数）、探活 bad。defer/展示性字段不持久化。"""
    now = time.time()
    out: dict = {}
    for ep in pool._endpoints:
        rec: dict = {}
        if ep._cooldown_until > now:
            rec["cooldown_until"] = ep._cooldown_until
            rec["cooldown_reason"] = ep._cooldown_reason or ""
        if ep._manual_unlock_required:
            rec["manual_unlock_required"] = True
        if ep._fail_count > 0:
            rec["fail_count"] = ep._fail_count
        if ep._health == "bad":
            rec["health"] = "bad"
            if ep._health_error:
                rec["health_error"] = ep._health_error
        if rec:
            out[ep.id] = rec
    return out


def _collect_fallback_state():
    """收集组级 fallback 状态用于快照（2026-09-06 组 fallback 持久化）。

    仅记录有状态价值的项：组 fallback 回切锁（未过期）、main 组 prio99 终极兜底锁（未过期）。
    过期锁不落盘。历史 fallback 计数已移除（2026-09-06，不再跨重启累计）。"""
    now = time.time()
    locks: dict = {}
    fallback_locks: dict = {}
    for grp, until in pool._group_fallback_lock_until.items():
        try:
            ts = float(until)
        except (TypeError, ValueError):
            continue
        if ts > now:
            locks[grp] = ts
    for grp, until in pool._fallback_lock_until_by_group.items():
        try:
            ts = float(until)
        except (TypeError, ValueError):
            continue
        if ts > now:
            fallback_locks[grp] = ts
    out: dict = {}
    if locks:
        out["locks"] = locks
    if fallback_locks:
        out["fallback_locks"] = fallback_locks
    return out


def snapshot_runtime_state():
    """停止前快照：dump 内存指针态 + 冷却/冻结状态全量写盘（2026-09-01 方案 A + 冷却持久化）。

    热路径零写盘：自动路由成功/冷却设置不再实时落盘；仅手动切换/模型切换等显式操作
    即时写盘，正常停止/重启前由 SIGTERM handler 调用本函数全量覆盖。
    崩溃场景由启动恢复 + 残留自愈兜底（文件为上次停止/手动操作时的状态）。
    """
    try:
        groups_state: dict = {}
        for grp in pool._all_group_names():
            ep_id = pool._get_manual(grp) or pool._get_current(grp)
            if isinstance(ep_id, str) and ep_id:
                groups_state[grp] = ep_id
        cooldowns = _collect_cooldown_state()
        fallback_state = _collect_fallback_state()
        if save_runtime_state_groups(
            groups_state, cooldowns=cooldowns, replace_groups=True, fallback=fallback_state,
        ):
            sys_log(
                f"停止前运行态快照已保存（{len(groups_state)} 个组，{len(cooldowns)} 个端点冷却态，"
                f"{len(fallback_state.get('locks', {})) + len(fallback_state.get('fallback_locks', {}))} 把 fallback 锁）",
                "INFO",
            )
            return True
    except Exception as exc:
        sys_log(f"停止前运行态快照失败: {exc}", "WARN")
    return False

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f).get("api_endpoints", [])
    except Exception:
        return []

def load_client_profiles():
    """读取 client_profiles 顶层键；旧配置无此键 → {}。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f).get("client_profiles", {})
    except Exception:
        return {}

def load_probe_client_profile():
    """读取 probe_client_profile 顶层键：池自身出站（探活/测试/拉模型/健康检测）的默认身份
    profile 名（2026-09-12）；旧配置/key 缺失 → ""（此时退化为"最近一次真实客户端指纹"）。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return str(json.load(f).get("probe_client_profile", "") or "")
    except (OSError, ValueError, TypeError):
        return ""

def _sanitize_profile_headers(headers):
    """过滤 profile 保留头（由 pool 按端点协议管理）；返回 (清洗后 dict, 被过滤头名列表)。"""
    clean, dropped = {}, []
    for k, v in (headers or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.strip().lower() in _PROFILE_RESERVED_HEADERS:
            dropped.append(k)
            continue
        clean[k] = v
    return clean, dropped

def _filter_accept_encoding(headers):
    """把 Accept-Encoding 收敛到解压链支持的编码；无可用 token 时丢弃该头（不让上游压缩）。"""
    for k in list(headers):
        if k.strip().lower() != "accept-encoding":
            continue
        keep = [t.strip() for t in str(headers[k]).split(",") if t.strip().lower() in _SUPPORTED_ACCEPT_ENCODINGS]
        if keep:
            headers[k] = ", ".join(keep)
        else:
            headers.pop(k)
    return headers


def resolve_outbound_headers(profile_name, client_profiles=None, label=""):
    """出站头统一解析（2026-09-11 透明网关）。

    profile_name 非空 → 伪装：仅用该 profile 的头（不叠加客户端头，避免混合指纹）；
    为空 → 透传：用客户端真实入站头（探活/测试/拉模型线程无客户端上下文时用客户端基线），
    客户端无 UA 时回退默认库标识。"""
    name = (profile_name or "").strip()
    if name:
        entry = (client_profiles or {}).get(name)
        if isinstance(entry, dict):
            clean, dropped = _sanitize_profile_headers(entry.get("headers", {}))
            if dropped:
                sys_log(f"客户端伪装：profile '{name}' 包含保留头 {sorted(dropped)}，已过滤", "WARN")
            return _filter_accept_encoding(clean)
        sys_log(f"客户端伪装：{label or '端点'} 引用了未知 profile '{name}'，降级为透传", "WARN")
    return passthrough_client_headers()


def passthrough_client_headers():
    """透传分支：当前线程客户端头 → 客户端基线；按 skip 表过滤 + Accept-Encoding 收敛。

    不为缺失的 UA 补任何默认值（2026-09-12：原 `_DEFAULT_OUTBOUND_UA` 编造值已删除）——
    真实客户端没带 UA 就原样转发，由上游自己判。"""
    headers = {}
    for k, v in _current_client_headers().items():
        if k.strip().lower() in _PASSTHROUGH_SKIP_HEADERS:
            continue
        headers[k] = v
    return _filter_accept_encoding(headers)


def resolve_pool_identity(ep, client_profiles=None, probe_profile="", label=""):
    """池自身出站的**确定性身份**（2026-09-12）。

    优先级：端点 client_profile → 池级 probe_client_profile → 最近一次真实客户端指纹。
    返回 (headers, source)，source ∈ {"endpoint-profile", "pool-profile", "client-baseline", "none"}；
    source == "none" 时 headers 为空 dict，调用方**必须跳过本次出站**（不得编造身份）。

    ep 可传端点对象，也可直接传 profile 名字符串（fetch_models 等只有名字的调用点）。"""
    endpoint_profile = (ep if isinstance(ep, str) else str(getattr(ep, "client_profile", "") or "")).strip()
    if endpoint_profile:
        headers = resolve_outbound_headers(endpoint_profile, client_profiles, label=label or endpoint_profile)
        if headers:
            return headers, "endpoint-profile"
    pool_profile = str(probe_profile or "").strip()
    if pool_profile:
        headers = resolve_outbound_headers(pool_profile, client_profiles, label=label or pool_profile)
        if headers:
            return headers, "pool-profile"
    headers = passthrough_client_headers()
    if headers:
        return headers, "client-baseline"
    return {}, "none"

def load_group_defs_config():
    """读取组实体定义（2026-08-30 组管理）：pool_group_defs 顶层键。旧配置无此键 → None（走派生）。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f).get("pool_group_defs")
    except Exception:
        return None

def save_config(endpoints_data, group_defs=None, client_profiles=None, probe_client_profile=None):
    tmp_file = os.path.join(
        os.path.dirname(os.path.abspath(CONFIG_FILE)),
        f".{os.path.basename(CONFIG_FILE)}.tmp",
    )
    payload: dict = {"api_endpoints": endpoints_data}
    # 组管理（2026-08-30）：仅显式传入时写入（None=保持不落盘，旧配置首次编辑前零迁移）
    if group_defs is not None:
        payload["pool_group_defs"] = group_defs
    # 客户端伪装（2026-09-10）：与组管理同模式，None=不落盘
    if client_profiles is not None:
        payload["client_profiles"] = client_profiles
    # 池自身出站身份（2026-09-12）：None=不落盘，保持与组管理/伪装同模式
    if probe_client_profile is not None:
        payload["probe_client_profile"] = probe_client_profile
    try:
        with _config_lock:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, CONFIG_FILE)
    except OSError:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass
        raise

def save_content_filter_config(section):
    """原子写 content_filter.json + 热重载；结构无效时回滚旧配置（2026-09-11）。

    - 备份：写前复制当前文件为 content_filter.json.bak.<ts>
    - 写入：tmp + flush/fsync + os.replace（与 save_config 同模式）
    - 校验：写入后 content_filter.load() 失败（返回 False 或抛异常）→ 回滚备份并重新 load
    """
    if not isinstance(section, dict):
        raise ValueError("content_filter 配置段必须是对象")
    directory = os.path.dirname(os.path.abspath(CONTENT_FILTER_FILE))
    tmp_file = os.path.join(directory, f".{os.path.basename(CONTENT_FILTER_FILE)}.tmp")
    backup_file = os.path.join(
        directory,
        f"{os.path.basename(CONTENT_FILTER_FILE)}.bak.{time.strftime('%Y%m%d_%H%M%S')}",
    )
    try:
        with open(CONTENT_FILTER_FILE, "r", encoding="utf-8") as f:
            old_content = f.read()
    except FileNotFoundError:
        old_content = None
    except OSError as e:
        raise ValueError(f"读取当前过滤配置失败: {e}") from e
    try:
        if old_content is not None:
            # 写前落时间戳备份（人工回滚入口；回滚本身走内存副本）
            with open(backup_file, "w", encoding="utf-8") as f:
                f.write(old_content)
                f.flush()
                os.fsync(f.fileno())
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump({"content_filter": section}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, CONTENT_FILTER_FILE)
    except OSError as e:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass
        raise ValueError(f"写入过滤配置失败: {e}") from e
    # 热重载校验：无效正则等异常会以 re.error 直接抛出（282 行 compile 无 try 包裹），
    # 必须用 Exception 兜底；失败回滚，保证运行态不降级。
    try:
        ok = content_filter.load()
    except Exception:
        ok = False
    if not ok:
        if old_content is not None:
            try:
                with open(CONTENT_FILTER_FILE, "w", encoding="utf-8") as f:
                    f.write(old_content)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass
        try:
            content_filter.load()
        except Exception:
            pass  # 旧配置自身损坏时 load 内部已降级为禁用
        raise ValueError("过滤配置结构无效，已回滚")
    return {"content_filter": section}

def ensure_config():
    if not os.path.exists(CONFIG_FILE): save_config([])

ensure_config()

pool = APIPool()
for ep_data in load_config():
    if "in_pool" not in ep_data: ep_data["in_pool"] = True
    # 批量加载不逐次重排：增量 renumber 以加载顺序为 tiebreak，会覆盖 config 中
    # 已保存的组内优先级（2026-09-07 修复，见 _load_group_defs 后的统一收尾）。
    pool.add_endpoint(ep_data, renumber=False)
# 组管理（2026-08-30）：加载组实体定义；旧配置无 defs → 从端点声明派生（selector=组名，mixed；
# 仅内存态，首次组编辑时 _sync_to_config 才把 pool_group_defs 落盘）
_defs_raw = load_group_defs_config()
if isinstance(_defs_raw, list):
    pool._load_group_defs(_defs_raw)
pool._derive_group_defs()
# 分组池：旧扁平路由状态（若有）迁移为 main 组状态
pool._migrate_legacy_state()
# 统一收尾：端点与组实体全部就绪后按 config 已保存的组内优先级重排一次。
# 单次全量重排按现有 pbg 排序编号，无并列时幂等，不会破坏持久化优先级。
pool._renumber_pool_priorities()
# 按组恢复重启前的粘性指针（runtime_state 新格式 {"groups": {...}}，兼容旧扁平格式→main）。
restored_state = load_runtime_state()
restored_groups = restored_state.get("groups") if isinstance(restored_state, dict) else {}
restored_cooldowns = restored_state.get("cooldowns") if isinstance(restored_state, dict) else {}
restored_fallback = (
    restored_state.get("group_fallback")
    if isinstance(restored_state, dict) and isinstance(restored_state.get("group_fallback"), dict)
    else {}
)
stale_state_keys: list[str] = []
if isinstance(restored_groups, dict):
    for grp, ep_id in restored_groups.items():
        if not isinstance(ep_id, str) or not ep_id:
            continue
        restored_endpoint = next(
            (
                ep for ep in pool._endpoints
                if ep.id == ep_id and ep.enabled and ep.in_pool
                and not ep._manual_unlock_required
                and grp in pool._ep_groups(ep)
            ),
            None,
        )
        if restored_endpoint is not None:
            # 重启恢复的是"当前端点"而非一次性首请求偏好。复用手动覆盖路径，
            # 直到该端点失败/进入冷却或用户主动切换，才允许离开该端点。
            pool._set_current(grp, restored_endpoint.id)
            pool._set_manual(grp, restored_endpoint.id)
            pool._set_persisted(grp, restored_endpoint.id)
        else:
            sys_log(f"组 '{grp}' 的恢复端点 {ep_id} 不存在或不可用，忽略", "WARN")
            # 残留自愈（2026-09-01 方案 A）：组改名/删除、端点删除遗留的键
            # （组名已不在当前配置 或 端点 id 已不存在）从 runtime_state 删除，
            # 避免每次重启重复 WARN。端点存在但暂不属于该组则保留（防误删合法指针）。
            group_unknown = (
                grp not in pool._group_defs
                and not any(grp in pool._ep_groups(ep) for ep in pool._endpoints)
            )
            endpoint_missing = not any(ep.id == ep_id for ep in pool._endpoints)
            if group_unknown or endpoint_missing:
                stale_state_keys.append(grp)
if stale_state_keys and isinstance(restored_groups, dict):
    for grp in stale_state_keys:
        restored_groups.pop(grp, None)
    if save_runtime_state_groups(
        restored_groups, cooldowns=restored_cooldowns, replace_groups=True, fallback=restored_fallback,
    ):
        sys_log(f"已清理运行态残留组键: {', '.join(sorted(stale_state_keys))}", "INFO")

# 冷却/冻结状态恢复（2026-09-01 冷却持久化）：SIGTERM 快照写入的 cooldowns 键。
# 恢复 cooldown_until/reason、manual_unlock_required、fail_count（阶梯基数）与
# 探活 bad 状态；过期冷却不恢复；端点已不存在的键清理（防每次重启重复 WARN）。
stale_cooldown_keys: list[str] = []
if isinstance(restored_cooldowns, dict):
    _now = time.time()
    for ep_id, rec in restored_cooldowns.items():
        ep = next((e for e in pool._endpoints if e.id == ep_id), None)
        if ep is None or not isinstance(rec, dict):
            stale_cooldown_keys.append(ep_id)
            continue
        try:
            until = float(rec.get("cooldown_until", 0))
        except (TypeError, ValueError):
            until = 0
        if until > _now:
            ep._cooldown_until = until
            ep._cooldown_reason = str(rec.get("cooldown_reason") or "")
        if rec.get("manual_unlock_required"):
            ep._manual_unlock_required = True
        try:
            ep._fail_count = max(0, int(rec.get("fail_count", 0)))
        except (TypeError, ValueError):
            pass
        if rec.get("health") == "bad":
            ep._health = "bad"
            ep._health_error = str(rec.get("health_error") or "")
    if stale_cooldown_keys:
        for k in stale_cooldown_keys:
            restored_cooldowns.pop(k, None)
        if save_runtime_state_groups(restored_groups, cooldowns=restored_cooldowns, replace_groups=True):
            sys_log(f"已清理运行态残留冷却键: {', '.join(sorted(stale_cooldown_keys))}", "INFO")
    if restored_cooldowns:
        sys_log(f"已恢复 {len(restored_cooldowns)} 个端点的冷却/冻结状态", "INFO")

# 组级 fallback 状态恢复（2026-09-06 组 fallback 持久化）：SIGTERM 快照写入的
# group_fallback 键。恢复未过期的 fallback 回切锁 / main 组 prio99 兜底锁
# （锁按绝对时间戳恢复剩余窗口）；过期锁剔除；组已不存在（组名不在 defs 且无端点归属）
# 的键清理（与 groups 残留自愈一致）。历史 fallback 计数已移除（2026-09-06）：
# 旧快照中的 counts 键不再恢复，回写时剔除，避免跨重启残留累计数。
if isinstance(restored_fallback, dict) and restored_fallback:
    _now = time.time()
    _legacy_counts = restored_fallback.pop("counts", None)
    if isinstance(_legacy_counts, dict) and _legacy_counts:
        sys_log("旧快照含组 fallback 累计计数（已弃用移除，不恢复）", "INFO")
    _fb_locks = restored_fallback.get("locks") if isinstance(restored_fallback.get("locks"), dict) else {}
    _fb_fallback_locks = (
        restored_fallback.get("fallback_locks")
        if isinstance(restored_fallback.get("fallback_locks"), dict) else {}
    )
    for _grp, _until in _fb_locks.items():
        try:
            _ts = float(_until)
        except (TypeError, ValueError):
            continue
        if _ts > _now:
            pool._group_fallback_lock_until[_grp] = _ts
    for _grp, _until in _fb_fallback_locks.items():
        try:
            _ts = float(_until)
        except (TypeError, ValueError):
            continue
        if _ts > _now:
            pool._fallback_lock_until_by_group[_grp] = _ts
    # 残留组清理：锁键指向不存在的组时剔除并回写，防每次重启重复残留。
    # 回写无条件执行（进入本块即发生过状态读取），清空时显式写 {} 覆盖残留键。
    _stale_fb_groups = {
        grp for grp in {*_fb_locks, *_fb_fallback_locks}
        if grp not in pool._group_defs
        and not any(grp in pool._ep_groups(ep) for ep in pool._endpoints)
    }
    if _stale_fb_groups:
        for _grp in _stale_fb_groups:
            pool._group_fallback_lock_until.pop(_grp, None)
            pool._fallback_lock_until_by_group.pop(_grp, None)
        _fb_locks = {g: t for g, t in _fb_locks.items() if g not in _stale_fb_groups}
        _fb_fallback_locks = {g: t for g, t in _fb_fallback_locks.items() if g not in _stale_fb_groups}
        restored_fallback = {}
        if _fb_locks:
            restored_fallback["locks"] = _fb_locks
        if _fb_fallback_locks:
            restored_fallback["fallback_locks"] = _fb_fallback_locks
    if _stale_fb_groups:
        sys_log(f"已清理运行态残留组 fallback 键: {', '.join(sorted(_stale_fb_groups))}", "INFO")
    if _fb_locks or _fb_fallback_locks:
        sys_log(
            f"已恢复组 fallback 锁：{len(_fb_locks) + len(_fb_fallback_locks)} 把",
            "INFO",
        )
    save_runtime_state_groups(
        restored_groups, cooldowns=restored_cooldowns, replace_groups=True, fallback=restored_fallback,
    )


def api_handler(method, path, body):
    parsed = urlparse(path)
    cp = parsed.path

    # ================= OpenAI 兼容模型目录 =================
    if method == "GET" and cp in ("/v1/models", "/models"):
        # 分组池（2026-08-29）：目录列出全部组选择器 id（api-pool 历史别名 + 各组 selector），
        # dedicated 组 selector=真实模型名时即真实可用模型列表。
        # 组管理（2026-08-30）：selector 取组实体 model 字段（无实体定义回退组名）。
        selector_ids = ["api-pool"]
        context_by_selector = {}
        for grp in pool._all_group_names():
            sid = pool._group_selector(grp) or grp
            if sid not in selector_ids:
                selector_ids.append(sid)
            # 组级上下文长度（2026-09-12）：显式声明 → 成员最小值；无值则不带该键
            ctx = pool._group_context_tokens(grp)
            if ctx:
                context_by_selector[sid] = ctx
        data = []
        for mid in selector_ids:
            entry = {"id": mid, "object": "model", "created": 0, "owned_by": "api-pool"}
            if mid in context_by_selector:
                entry["context_length"] = context_by_selector[mid]
            data.append(entry)
        return 200, {"object": "list", "data": data}, False

    if method == "GET" and cp.startswith("/v1/models/"):
        # 单模型查询（Hermes 上下文解析链第 5 级本地探针路径）：选择器 → 组声明窗口
        mid = unquote(cp.split("/v1/models/", 1)[1]).strip("/")
        for grp in pool._all_group_names():
            if (pool._group_selector(grp) or grp) == mid:
                entry = {"id": mid, "object": "model", "created": 0, "owned_by": "api-pool"}
                ctx = pool._group_context_tokens(grp)
                if ctx:
                    entry["context_length"] = ctx
                return 200, entry, False
        return 404, {"error": {"message": f"model '{mid}' not found",
                               "type": "invalid_request_error", "param": None, "code": None}}, False

    if method == "GET" and (cp.startswith("/v1/responses/") or cp.startswith("/responses/")):
        rid = unquote(cp.rsplit("/", 1)[-1])
        stored = _responses_store_get(rid)
        if stored:
            return 200, stored.get("response", stored), False
        return 404, {"error": {"message": "Response not found", "type": "invalid_request_error", "param": None, "code": None}}, False

    if method == "DELETE" and (cp.startswith("/v1/responses/") or cp.startswith("/responses/")):
        rid = unquote(cp.rsplit("/", 1)[-1])
        if _responses_store_delete(rid):
            return 200, {"deleted": True, "id": rid}, False
        return 404, {"error": {"message": "Response not found", "type": "invalid_request_error", "param": None, "code": None}}, False

    if method == "POST" and cp in ("/v1/responses", "/responses"):
        return _handle_responses(body)

    # ================= 代理接口 =================
    if method == "POST" and cp in ("/v1/chat/completions", "/chat/completions"):
        messages = body.get("messages", [])
        is_stream = body.get("stream", False)
        
        # 入口敏感字清洗（唯一执行点）：清洗结果贯穿端点选择/协议转换/重试/日志
        try:
            body, filter_stats = content_filter.filter_payload(body, return_stats=True)
            messages = body.get("messages", messages)
        except ContentFilterError as e:
            sys_log(f"敏感字过滤不可用，拒绝请求: {e}", "ERROR")
            return 503, {"error": {"message": "API Pool content filter unavailable", "type": "service_unavailable"}}, False
        if filter_stats.get("matched"):
            sys_log(f"敏感字过滤命中 {filter_stats['matched']} 处 ({filter_stats['duration_ms']}ms)", "INFO")
        extra_payload = {k: v for k, v in body.items() if k not in ("messages", "model")}
        extra_payload.pop("extra_body", None)
        response_format = extra_payload.pop("response_format", None)
        if response_format is not None:
            extra_payload["_responses_response_format"] = response_format
        
        try:
            # 分组池：model 字段作为组选择器传入 chat()（api-pool→main 别名，
            # 精确匹配组名，无匹配→main；Hermes 侧配置 api-pool-bg 即路由 bg 组）
            result = pool.chat(messages, model=body.get("model"), extra_payload=extra_payload)
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

    if method == "GET" and cp == "/api/debug":
        return 200, {"debug_enabled": _DEBUG_LOGGING}, False

    if method == "POST" and cp == "/api/debug":
        enabled = bool(body.get("enabled", False)) if isinstance(body, dict) else False
        _set_debug_logging(enabled)
        sys_log(f"请求特征 DEBUG 日志已{'开启' if _DEBUG_LOGGING else '关闭'}", "INFO")
        return 200, {"debug_enabled": _DEBUG_LOGGING}, False

    if method == "DELETE" and cp == "/api/logs":
        sys_logger.clear_logs()
        return 200, {"ok": True}, False

    if method == "GET" and cp == "/api/chat-logs":
        qs = dict(q.split("=") for q in parsed.query.split("&") if "=" in q) if parsed.query else {}
        limit = int(qs.get("limit", 50))
        offset = int(qs.get("offset", 0))
        # 列表页 detail=false 不拉正文（33MB→KB 级）；详情面板按 id 单独取
        detail = qs.get("detail", "true").lower() != "false"
        return 200, chat_logger.get_logs(limit=limit, offset=offset, detail=detail), False
    if method == "GET" and cp == "/api/chat-log":
        qs = dict(q.split("=") for q in parsed.query.split("&") if "=" in q) if parsed.query else {}
        log_id = qs.get("id", "")
        if not log_id:
            return 400, {"error": "缺少 id 参数"}, False
        one = chat_logger.get_log_by_id(log_id)
        if one is None:
            return 404, {"error": "记录不存在"}, False
        return 200, one, False

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
    if method == "GET" and cp.startswith("/api/endpoints/") and cp.endswith("/models"):
        ep_id = unquote(cp[len("/api/endpoints/"):-len("/models")]).strip("/")
        if not ep_id:
            return 400, {"error": "缺少端点 id"}, False
        try:
            models = pool.fetch_endpoint_models(ep_id)
            return 200, {"ok": True, "models": models, "count": len(models)}, False
        except KeyError as exc:
            return 404, {"error": str(exc.args[0])}, False
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = _DecodedResponse(exc).read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                pass
            return 200, {"ok": False, "error": f"HTTP {exc.code}: {err_body}"}, False
        except Exception as exc:
            return 200, {"ok": False, "error": str(exc)}, False
    if method == "GET" and cp == "/api/chain":
        chain = pool.get_active_chain()
        # 分组池：附加 per-group 汇总（各组当前指针 + fallback 锁实时状态），驱动 UI 组视图
        group_summary = {}
        for grp in pool._all_group_names():
            cur = pool._get_manual(grp) or pool._get_current(grp)
            cur_ep = next((e for e in pool._endpoints if e.id == cur), None)
            _fb_until = pool._group_fallback_lock_until.get(grp, 0)
            group_summary[grp] = {
                "current_endpoint": cur_ep.name if cur_ep else None,
                "current_endpoint_id": cur,
                # 整组 fallback 锁剩余秒数（>0 = 该组正借道 main，UI 显示 ↩main）
                "fallback_lock_remaining": max(0, int(_fb_until - time.time())) if _fb_until else 0,
                # 待回切（锁已期满、尚无该组请求回组试探，UI 显示 ⏸待回切，点击立即切回）
                "fallback_return_pending": bool(pool._group_fallback_pending.get(grp)),
                "members": sum(1 for e in pool._endpoints if e.in_pool and grp in pool._ep_groups(e)),
                "is_vision": grp == pool.VISION_GROUP,
                # 图片解析池可用成员数（调度候选口径，与 _vision_pool_candidates 一致）
                "available": len(pool._vision_pool_candidates()[0]) if grp == pool.VISION_GROUP else 0,
            }
        return 200, {"chain": chain, "groups": group_summary}, False
    # ================= 组管理（2026-08-30）=================
    if method == "GET" and cp == "/api/groups":
        groups = []
        for grp in pool._all_group_names():
            gd = pool._group_defs.get(grp, {})
            cur = pool._get_manual(grp) or pool._get_current(grp)
            cur_ep = next((e for e in pool._endpoints if e.id == cur), None)
            groups.append({
                "name": grp,
                "type": gd.get("type", "mixed"),
                "model": gd.get("model", grp),
                "context_tokens": pool._group_context_tokens(grp) or 0,
                "is_vision": grp == pool.VISION_GROUP,
                "members": sum(1 for e in pool._endpoints if e.in_pool and grp in pool._ep_groups(e)),
                "current_endpoint": cur_ep.name if cur_ep else None,
                "is_main": grp == pool.MAIN_GROUP,
            })
        return 200, {"groups": groups}, False
    if method == "POST" and cp == "/api/groups":
        name = str(body.get("name", "")).strip()
        gtype = body.get("type", "mixed")
        model = str(body.get("model", "") or "").strip()
        ok, msg = pool.create_group(name, gtype, model, body.get("context_tokens", 0))
        if not ok:
            return 400, {"error": msg}, False
        _sync_to_config()
        return 201, {"ok": True, "name": msg}, False
    if method == "POST" and cp.startswith("/api/groups/") and cp.endswith("/replace-model"):
        gname = unquote(cp[len("/api/groups/"):-len("/replace-model")]).strip("/")
        source_ep_id = str(body.get("endpoint_id", "") or "").strip()
        model = str(body.get("model", "") or "").strip()
        if not gname or not source_ep_id or not model:
            return 400, {"error": "需要分组、endpoint_id 和 model"}, False
        try:
            replacement, created, pointer_moved = pool.replace_group_model(gname, source_ep_id, model)
            _sync_to_config()
            if pointer_moved:
                state = load_runtime_state() or {}
                loaded = state.get("groups") if isinstance(state.get("groups"), dict) else None
                groups_state: dict = dict(loaded) if loaded is not None else {}
                groups_state[gname] = replacement.id
                if not save_runtime_state_groups(groups_state):
                    return 500, {"error": "运行态指针持久化失败"}, False
            return 200, {
                "ok": True,
                "created": created,
                "endpoint": pool._ep_to_dict(replacement, False, time.time()),
            }, False
        except KeyError as exc:
            return 404, {"error": str(exc.args[0])}, False
        except ValueError as exc:
            return 400, {"error": str(exc)}, False
    if method == "PUT" and cp.startswith("/api/groups/"):
        gname = unquote(cp[len("/api/groups/"):])
        ok, msg = pool.update_group(gname, body)
        if not ok:
            return 400, {"error": msg}, False
        _sync_to_config()
        return 200, {"ok": True, "name": msg}, False
    if method == "DELETE" and cp.startswith("/api/groups/"):
        gname = unquote(cp[len("/api/groups/"):])
        ok, msg = pool.delete_group(gname)
        if not ok:
            return 400, {"error": msg}, False
        _sync_to_config()
        return 200, {"ok": True}, False
    if method == "POST" and cp.startswith("/api/groups/") and cp.endswith("/clear-fallback"):
        # 手动「立即切回」（UI 点击 ↩main 锁定徽标 / ⏸待回切 徽标）：清回切锁 + 清该组
        # 端点的冷却/冻结/失败态 → 下一个请求真正落回本组。
        gname = unquote(cp[len("/api/groups/"):-len("/clear-fallback")]).strip("/")
        if not gname:
            return 400, {"error": "需要分组名"}, False
        if gname not in pool._all_group_names():
            return 404, {"error": f"组 '{gname}' 不存在"}, False
        result = pool.clear_group_fallback(gname)
        if result["lock_cleared"] or result["endpoints_reset"]:
            # 显式落盘（清锁 + 清冷却都是内存态）：崩溃重启（无 SIGTERM 快照）不会把已解除的
            # 锁/冷却从磁盘复活。合并模式保留 groups，cooldowns/group_fallback 精确覆盖。
            save_runtime_state_groups(
                {}, cooldowns=_collect_cooldown_state(), fallback=_collect_fallback_state(),
            )
        return 200, {"ok": True, **result}, False

    # ================= 聚合池管理 =================
    if method == "GET" and cp == "/api/pool":
        return 200, [ep for ep in pool.list_endpoints() if ep.get("in_pool")], False
    if method == "POST" and cp == "/api/pool/switch":
        # 组感知切换接口必须先于通用 /api/pool/ 路由匹配。
        # 插件只提交结构化参数，组归属与状态持久化由 API Pool 统一负责。
        group = str(body.get("group", "") or "").strip()
        ep_id = str(body.get("endpoint_id", "") or "").strip()
        if not group or not ep_id:
            return 400, {"ok": False, "error": "需要 group 和 endpoint_id"}, False
        ep = pool.get_endpoint(ep_id)
        if ep is None:
            return 404, {"ok": False, "error": "端点不存在"}, False
        if group not in pool._all_group_names():
            return 404, {"ok": False, "error": "分组不存在"}, False
        if group not in pool._ep_groups(ep):
            return 409, {"ok": False, "error": "端点不属于目标分组"}, False
        if not pool.switch_to_endpoint(ep_id, group=group):
            return 409, {"ok": False, "error": "端点未启用、未入池或不可切换"}, False
        state = load_runtime_state() or {}
        loaded = state.get("groups") if isinstance(state.get("groups"), dict) else None
        groups_state: dict = dict(loaded) if loaded is not None else {}
        groups_state[group] = ep_id
        if save_runtime_state_groups(groups_state, cooldowns=_collect_cooldown_state()):
            pool._set_persisted(group, ep_id)
        return 200, {
            "ok": True,
            "group": group,
            "endpoint_id": ep_id,
            "endpoint_name": ep.name,
            "model": ep.model,
            "current": True,
        }, False
    if method == "POST" and cp.startswith("/api/pool/") and cp.endswith("/select-model"):
        source_ep_id = unquote(cp[len("/api/pool/"):-len("/select-model")]).strip("/")
        group = str(body.get("group", "") or "").strip()
        model = str(body.get("model", "") or "").strip()
        if not source_ep_id or not group or not model:
            sys_log(f"模型切换失败(400): 参数缺失 group='{group}' model='{model}' source='{source_ep_id}'", "ERROR")
            return 400, {"error": "需要端点、group 和 model"}, False
        try:
            replacement, created, pointer_moved = pool.replace_group_model(group, source_ep_id, model)
            _sync_to_config()
            if pointer_moved:
                state = load_runtime_state() or {}
                loaded = state.get("groups") if isinstance(state.get("groups"), dict) else None
                groups_state: dict = dict(loaded) if loaded is not None else {}
                groups_state[group] = replacement.id
                if not save_runtime_state_groups(groups_state):
                    return 500, {"error": "运行态指针持久化失败"}, False
            sys_log(f"模型切换成功: 组 '{group}' 源端点 {source_ep_id} → 模型 '{model}' → 端点 '{replacement.name}'（{'克隆' if created else '复用'}）", "INFO")
            return 200, {
                "ok": True,
                "action": "cloned" if created else "reused",
                "endpoint_id": replacement.id,
                "endpoint_name": replacement.name,
            }, False
        except KeyError as exc:
            sys_log(f"模型切换失败(404): 组 '{group}' 源端点 {source_ep_id} 模型 '{model}': {exc}", "ERROR")
            return 404, {"error": str(exc.args[0])}, False
        except ValueError as exc:
            sys_log(f"模型切换失败(400): 组 '{group}' 源端点 {source_ep_id} 模型 '{model}': {exc}", "ERROR")
            return 400, {"error": str(exc)}, False
    if method == "POST" and cp.startswith("/api/pool/"):
        ep_id = unquote(cp.split("/")[-1])
        # 分组池：?groups=main,bg 入池同时指定组（缺省保持原组不变；兼容旧调用）
        qs_groups = None
        if parsed.query:
            for kv in parsed.query.split("&"):
                if kv.startswith("groups="):
                    qs_groups = [g for g in unquote(kv[7:]).split(",") if g.strip()]
                    break
        pool.set_pool(ep_id, True, groups=qs_groups); _sync_to_config()
        return 200, {"ok": True}, False
    if method == "DELETE" and cp.startswith("/api/pool/"):
        ep_id = unquote(cp.split("/")[-1])
        # 分组池：?group=xxx 仅从该组移出（其他组不动）；缺省=整体出池
        qs_group = None
        if parsed.query:
            for kv in parsed.query.split("&"):
                if kv.startswith("group="):
                    qs_group = unquote(kv[6:])
                    break
        if qs_group:
            pool.remove_from_group(ep_id, qs_group)
        else:
            pool.set_pool(ep_id, False)
        _sync_to_config()
        return 200, {"ok": True}, False
    if method == "POST" and cp.startswith("/api/switch-endpoint/"):
        ep_id = unquote(cp.split("/")[-1])
        # 分组池：支持 ?group= 查询参数指定目标组（默认 main）
        qs_group = None
        if parsed.query:
            for kv in parsed.query.split("&"):
                if kv.startswith("group="):
                    qs_group = unquote(kv[6:])
                    break
        ok = pool.switch_to_endpoint(ep_id, group=qs_group)
        if ok:
            grp = qs_group or pool.MAIN_GROUP
            state = load_runtime_state() or {}
            loaded = state.get("groups") if isinstance(state.get("groups"), dict) else None
            groups_state: dict = dict(loaded) if loaded is not None else {}
            groups_state[grp] = ep_id
            # 手动切换清除了该端点的冷却/冻结内存态，冷却快照必须同步落盘。
            # 合并模式会保留文件里的旧 cooldowns，非 SIGTERM 崩溃重启会把已解除的
            # 冻结从磁盘复活（启动恢复读 cooldowns 键）。
            if save_runtime_state_groups(groups_state, cooldowns=_collect_cooldown_state()):
                pool._set_persisted(grp, ep_id)
        return 200, {"ok": ok}, False
    # ================= 客户端伪装 profile 管理（2026-09-10） =================
    if method == "GET" and cp == "/api/client-profiles":
        return 200, {
            "profiles": pool.list_client_profiles(),
            # 池自身出站（探活/测试/拉模型）的默认身份 profile（2026-09-12）
            "probe_client_profile": getattr(pool, "_probe_client_profile", ""),
            "has_client_baseline": bool(_current_client_headers()),
        }, False
    if method == "POST" and cp == "/api/client-profiles":
        try:
            name = pool.save_client_profile(body.get("name", ""), body.get("headers", {}))
        except ValueError as e:
            return 400, {"error": str(e)}, False
        _sync_to_config()
        return 201, {"ok": True, "name": name}, False
    if method == "POST" and cp == "/api/probe-client-profile":
        # 池自身出站身份（探活/测试/拉模型；2026-09-12）：空串=用最近一次真实客户端指纹
        probe_name = str(body.get("name", "") or "").strip()
        if probe_name and not any(p["name"] == probe_name for p in pool.list_client_profiles()):
            return 400, {"error": f"profile '{probe_name}' 不存在"}, False
        with pool._lock:
            pool._probe_client_profile = probe_name
        _sync_to_config()
        sys_log(f"池自身出站身份 probe_client_profile = '{probe_name or '未设置（用最近一次真实客户端指纹）'}'", "INFO")
        return 200, {"ok": True, "probe_client_profile": probe_name}, False
    if method == "PUT" and cp.startswith("/api/client-profiles/"):
        name = unquote(cp[len("/api/client-profiles/"):]).strip("/")
        try:
            pool.save_client_profile(name, body.get("headers", {}))
        except ValueError as e:
            return 400, {"error": str(e)}, False
        _sync_to_config()
        return 200, {"ok": True, "name": name}, False
    if method == "DELETE" and cp.startswith("/api/client-profiles/"):
        name = unquote(cp[len("/api/client-profiles/"):]).strip("/")
        try:
            pool.delete_client_profile(name)
        except ValueError as e:
            return 400, {"error": str(e)}, False
        _sync_to_config()
        return 200, {"ok": True}, False

    # ================= 敏感词过滤配置管理（2026-09-11） =================
    if method == "GET" and cp == "/api/content-filter":
        return 200, {
            "content_filter": content_filter._raw.get("content_filter", {}),
            "known_targets": list(ContentFilter.KNOWN_TARGETS),
        }, False
    if method == "PUT" and cp == "/api/content-filter":
        section = body.get("content_filter")
        if not isinstance(section, dict):
            return 400, {"error": "content_filter 配置段缺失或类型错误"}, False
        try:
            payload = save_content_filter_config(section)
        except ValueError as e:
            return 400, {"error": str(e)}, False
        return 200, payload, False
    if method == "POST" and cp == "/api/content-filter/test":
        text = body.get("text")
        if not isinstance(text, str):
            return 400, {"error": "text 必须是字符串"}, False
        result, matched = content_filter._match_and_replace(text)
        return 200, {
            "matched": matched,
            "result": result,
            "enabled": content_filter._enabled,
            "dictionary_version": content_filter._dictionary_version,
        }, False

    if method == "POST" and cp == "/api/endpoints":
        pool.add_endpoint(body); _sync_to_config(); return 201, {"ok": True}, False
    if method == "POST" and cp == "/api/endpoints/batch":
        items = body.get("endpoints", []); base = body.get("base", {}); added = 0; start_priority = base.get("start_priority", 1)
        for i, item in enumerate(items):
            ep = {
                "name": item.get("name", base.get("name", f"ep_{i}")), "base_url": item.get("base_url", base.get("base_url", "")),
                "site_name": item.get("site_name", base.get("site_name", "")),
                "api_key": item.get("api_key", base.get("api_key", "")), "model": item.get("model", ""),
                "priority": item.get("priority", start_priority + i), "timeout": item.get("timeout", base.get("timeout", 60)),
                "max_retries": item.get("max_retries", base.get("max_retries", 1)), "cooldown_minutes": item.get("cooldown_minutes", base.get("cooldown_minutes", 5)),
                               "use_proxy": item.get("use_proxy", base.get("use_proxy", False)),
                "protocol": item.get("protocol", base.get("protocol", "openai")),
                "client_profile": item.get("client_profile", base.get("client_profile", "")),
                "default_headers": item.get("default_headers", base.get("default_headers", {"User-Agent": item.get("user_agent", base.get("user_agent", ""))} if item.get("user_agent", base.get("user_agent", "")) else {})),
                "health_mode": item.get("health_mode", base.get("health_mode", "chat")),
                "billing_mode": item.get("billing_mode", base.get("billing_mode", "subscription")),
                "is_vision": item.get("is_vision", base.get("is_vision", False)),
                "stream_first_packet_timeout": item.get("stream_first_packet_timeout", base.get("stream_first_packet_timeout", 120)),
                "stream_stall_timeout": item.get("stream_stall_timeout", base.get("stream_stall_timeout", 60)),
                "stream_max_duration": item.get("stream_max_duration", base.get("stream_max_duration", 0)),
                "in_pool": item.get("in_pool", base.get("in_pool", False)),
                "enabled": item.get("enabled", True),
                "pool_groups": item.get("pool_groups", base.get("pool_groups", ["main"])),
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
        _sync_to_config()
        return 200, {"ok": True}, False
    if method == "POST" and cp == "/api/health-check": return 200, {"ok": True, "results": pool.check_all_health()}, False
    if method == "POST" and cp == "/api/fetch-models":
        base_url = body.get("base_url", ""); api_key = body.get("api_key", "")
        if not base_url or not api_key: return 400, {"error": "需要 base_url 和 api_key"}, False
        try:
            models = pool.fetch_models(
                base_url, api_key, use_proxy=body.get("use_proxy", True),
                protocol=body.get("protocol", "openai"),
                default_headers=body.get("default_headers", {"User-Agent": body["user_agent"]} if body.get("user_agent") else {}),
                client_profile=body.get("client_profile", ""),
            )
            return 200, {"ok": True, "models": models, "count": len(models)}, False
        except urllib.error.HTTPError as e:
            err_body = ""
            try: err_body = _DecodedResponse(e).read().decode("utf-8", errors="ignore")[:200]
            except Exception: pass
            return 200, {"ok": False, "error": f"HTTP {e.code}: {err_body}"}, False
        except Exception as e: return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/test-model": return 200, pool.test_model_latency(body.get("base_url", ""), body.get("api_key", ""), body.get("model", ""), timeout=body.get("timeout", 60), use_proxy=body.get("use_proxy", True), protocol=body.get("protocol", "openai"), user_agent=body.get("user_agent", ""), client_profile=body.get("client_profile", "")), False
    if method == "POST" and cp == "/api/test-vision": return 200, pool.test_vision(body.get("base_url", ""), body.get("api_key", ""), body.get("model", ""), timeout=body.get("timeout", 60), use_proxy=body.get("use_proxy", True), protocol=body.get("protocol", "openai"), user_agent=body.get("user_agent", ""), client_profile=body.get("client_profile", "")), False
    if method == "POST" and cp == "/api/test":
        ep_id = body.get("id", "")
        test_msg = body.get("message", "你好")
        target_ep = pool.get_endpoint(ep_id)
        if target_ep is None:
            return 404, {"error": "端点不存在"}, False
        group = body.get("group") or None
        try:
            res_dict, error = pool.test_endpoint(
                target_ep, message=test_msg, image=body.get("image"), group=group,
            )
            cooldowns = _collect_cooldown_state()
            if save_runtime_state_groups({}, cooldowns=cooldowns):
                pass
            if error:
                return 200, {"ok": False, "error": error}, False
            res_str = res_dict.get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(res_dict, dict) else res_dict
            return 200, {"ok": True, "result": res_str, "served_by": f"{target_ep.name} ({target_ep.model})"}, False
        except Exception as e:
            return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/test-pool":
        test_msg = body.get("message", "你好")
        img = body.get("image")
        if img:
            test_msg = [{"type": "text", "text": test_msg}, {"type": "image_url", "image_url": {"url": img}}]
        try:
            # 池自身发起的请求（2026-09-12 伤害隔离）：失败只写观测态，不打生产路由
            _POOL_INITIATED_CTX.active = True
            try:
                res_dict, served_ep = pool.chat([{"role": "user", "content": test_msg}], return_endpoint=True)
            finally:
                _POOL_INITIATED_CTX.active = False
            res_str = res_dict.get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(res_dict, dict) else res_dict
            return 200, {"ok": True, "result": res_str, "served_by": f"{served_ep.name} ({served_ep.model})"}, False
        except AllEndpointsFailed as e: return 200, {"ok": False, "errors": e.errors}, False
        except Exception as e: return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/reset": pool.reset(); return 200, {"ok": True}, False
    if method == "POST" and cp.startswith("/api/priority/"):
        # 分组隔离（2026-08-29）：组内优先级调整 ?group=xxx&priority=N（缺省组=main）
        ep_id = unquote(cp.split("/")[-1])
        qs_group, qs_priority = None, None
        if parsed.query:
            for kv in parsed.query.split("&"):
                if kv.startswith("group="):
                    qs_group = unquote(kv[6:])
                elif kv.startswith("priority="):
                    try:
                        qs_priority = int(kv[9:])
                    except ValueError:
                        pass
        if qs_priority is None:
            return 400, {"error": "需要 priority 参数"}, False
        pool.set_group_priority(ep_id, qs_group or pool.MAIN_GROUP, qs_priority)
        _sync_to_config()
        return 200, {"ok": True}, False

    return 404, {"error": "Not found"}, False

def _handle_responses(body):
    try:
        messages, extra_payload = _responses_to_chat_request(body)
    except ValueError as e:
        return 400, {"error": {"message": str(e), "type": "invalid_request_error", "param": None, "code": None}}, False

    previous_response_id = body.get("previous_response_id")
    if previous_response_id:
        stored = _responses_store_get(previous_response_id)
        if not stored:
            return 400, {"error": {
                "message": f"previous_response_id '{previous_response_id}' not found",
                "type": "invalid_request_error", "param": "previous_response_id", "code": None
            }}, False
        messages = (stored.get("messages") or []) + messages

    is_stream = bool(body.get("stream", False))
    extra_payload["stream"] = is_stream
    if is_stream:
        extra_payload.setdefault("stream_options", {"include_usage": True})
        if isinstance(extra_payload.get("stream_options"), dict):
            extra_payload["stream_options"]["include_usage"] = True

    response_id = f"resp_{int(time.time() * 1000)}"
    item_id = f"msg_{int(time.time() * 1000)}"

    def maybe_store(response_obj):
        if body.get("store"):
            _responses_store_put(response_obj.get("id") or response_id, {
                "messages": messages, "response": response_obj
            })

    try:
        result, served_ep = pool.chat(
            messages, model=body.get("model"), extra_payload=extra_payload,
            return_endpoint=True
        )
        if is_stream:
            return 200, _responses_stream_generator(
                result, body, meta={"protocol": "responses"}, model=served_ep.model, response_id=response_id,
                item_id=item_id, on_complete=maybe_store
            ), True

        chat_body = result if isinstance(result, dict) else {"choices": [{"message": {"role": "assistant", "content": str(result or "")}}]}
        choices = chat_body.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        text, _, _ = _responses_output_to_chat_message([{
            "type": "message", "content": [{"type": "output_text", "text": message.get("content") or ""}]
        }])
        response_obj = _responses_response_object(
            body, text, usage=_responses_usage_from_chat_usage(chat_body.get("usage")),
            model=served_ep.model, message=message, response_id=response_id, item_id=item_id
        )
        maybe_store(response_obj)
        return 200, response_obj, False
    except AllEndpointsFailed as e:
        return 500, {"error": {"message": f"所有端点均已失败: {e.errors}", "type": "server_error", "param": None, "code": None}}, False
    except Exception as e:
        return 500, {"error": {"message": str(e), "type": "server_error", "param": None, "code": None}}, False


def _sync_to_config():
    # 组管理（2026-08-30）：组实体定义随配置持久化（dict → list 有序形态，main 恒在首位）
    defs_list = [{"name": pool.MAIN_GROUP, **pool._group_defs[pool.MAIN_GROUP]}]
    for gname, gd in pool._group_defs.items():
        if gname != pool.MAIN_GROUP:
            entry = {"name": gname, "type": gd.get("type", "mixed"), "model": gd.get("model", gname)}
            if gd.get("context_tokens"):
                entry["context_tokens"] = gd["context_tokens"]
            defs_list.append(entry)
    save_config([{"id": ep.get("id"), "name": ep["name"], "site_name": ep.get("site_name", ""), "site_id": ep.get("site_id", ""), "base_url": ep["base_url"], "api_key": ep.get("api_key_full", ep.get("api_key", "")), "model": ep["model"], "priority": ep["priority"], "priority_by_group": ep.get("priority_by_group", {}), "timeout": ep["timeout"], "max_retries": ep["max_retries"], "enabled": ep["enabled"], "cooldown_minutes": ep["cooldown_minutes"], "use_proxy": ep.get("use_proxy", True), "protocol": ep.get("protocol", "openai"), "extra_headers": ep.get("extra_headers", {}), "default_headers": ep.get("default_headers", {}), "client_profile": ep.get("client_profile", ""), "health_mode": ep.get("health_mode", "chat"), "billing_mode": ep.get("billing_mode", "subscription"), "manual_unlock_required": ep.get("manual_unlock_required", False), "is_vision": ep.get("is_vision", True),
            "in_pool": ep.get("in_pool", False), "check_fake_success": ep.get("check_fake_success", False), "tool_call_id_prefix": ep.get("tool_call_id_prefix", ""), "strict400_retries": ep.get("strict400_retries", 2), "reasoning_policy": ep.get("reasoning_policy", "auto"), "preserved_thinking": ep.get("preserved_thinking", False), "deferrable": ep.get("deferrable", True), "max_context_k": ep.get("max_context_k", 0), "stream_first_packet_timeout": ep.get("stream_first_packet_timeout", 120), "stream_stall_timeout": ep.get("stream_stall_timeout", 60), "stream_max_duration": ep.get("stream_max_duration", 120), "pool_groups": ep.get("pool_groups", ["main"])} for ep in pool.list_endpoints()], group_defs=defs_list, client_profiles=pool._client_profiles,
        probe_client_profile=getattr(pool, "_probe_client_profile", ""))


GUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
GUI_FILE = os.path.join(GUI_DIR, "index.html")
_gui_html_cache = None
_gui_html_mtime = 0

# 请求特征 DEBUG 日志开关（2026-08-15）：默认关闭。
# 开启方式：
#   1. 启动时：环境变量 API_POOL_DEBUG=1
#   2. 运行时：POST /api/debug {"enabled": true}（免重启）
# 开启后在收到请求/请求成功时打印请求特征（大小/stream/max_tokens/thinking/tools 等）与耗时，
# 便于排查"某请求为何慢/卡"类问题。排查完记得关闭。
_DEBUG_LOGGING = os.environ.get("API_POOL_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

def _set_debug_logging(enabled: bool) -> bool:
    global _DEBUG_LOGGING
    _DEBUG_LOGGING = bool(enabled)
    return _DEBUG_LOGGING


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
            # no-cache：允许浏览器缓存但每次回源校验（配合 mtime 热更新，前端改动普通刷新即见）
            self.send_header("Cache-Control", "no-cache")
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
        elif self.path.startswith("/api/") or self.path.startswith("/v1/") or self.path.startswith("/responses/") or self.path == "/models":
            res = api_handler("GET", self.path, {})
            if len(res) == 3 and res[2] is True:
                code, stream_gen = res[0], res[1]
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in stream_gen:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    self.close_connection = True
                except ConnectionError:
                    pass
            else:
                self._send_json(res[0], res[1])
        else:
            self._send_json(404, {"error": "Not found"})

    _PROXY_PATHS = ("/v1/chat/completions", "/chat/completions", "/v1/responses", "/responses")

    def _is_proxy_path(self):
        return urlparse(self.path).path in self._PROXY_PATHS

    def do_POST(self):
        body = self._read_body()
        # 出站客户端透传：仅代理路径把客户端原始头带入本请求线程，并更新客户端基线供
        # 探活/测试/拉模型复用。流式生成器在本方法内同线程消费，故清理放在 finally。
        proxy_request = self._is_proxy_path()
        if proxy_request:
            set_client_headers(self.headers)
        try:
            res = api_handler("POST", self.path, body)

            if len(res) == 3 and res[2] is True:
                code, stream_gen = res[0], res[1]
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()

                    for chunk in stream_gen:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    self.close_connection = True
                except ConnectionError:
                    pass
            else:
                self._send_json(res[0], res[1])
        finally:
            if proxy_request:
                clear_client_headers()

    def do_PUT(self):
        body = self._read_body()
        res = api_handler("PUT", self.path, body)
        self._send_json(res[0], res[1])

    def do_DELETE(self):
        res = api_handler("DELETE", self.path, {})
        self._send_json(res[0], res[1])


def _handle_sigterm(signum, frame):
    # 方案 A（2026-09-01）：正常停止/重启前全量快照当前端点指针态，
    # 保证下次启动无缝恢复；随后立即退出（不等待非 daemon 工作线程）。
    snapshot_runtime_state()
    os._exit(0)


def main():
    import sys
    if sys.stdout.encoding.lower() != 'utf-8':
        try: sys.stdout.reconfigure(encoding='utf-8')
        except: pass
    instance_name = os.environ.get("API_POOL_INSTANCE", "API Pool").strip() or "API Pool"
    try:
        port = int(os.environ.get("API_POOL_PORT", "5100"))
    except ValueError:
        raise SystemExit("API_POOL_PORT must be an integer")
    if not 1 <= port <= 65535:
        raise SystemExit("API_POOL_PORT must be between 1 and 65535")
    signal.signal(signal.SIGTERM, _handle_sigterm)
    # 注：滚动清理由 ChatLogger.__init__ 的守护线程负责（启动即执行首次清理），
    #     不在 main() 同步执行——大表 DELETE 会阻塞 server 启动（2026-08-15 实测 63s）
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"\n  ⚡ {instance_name} 管理面板已启动")
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
