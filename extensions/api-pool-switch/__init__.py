"""API Pool endpoint switch plugin — switch endpoints via /endpoint slash command.

API: communicates with API Pool 2.0 on port 5200 (host 192.168.5.6).

Usage:
  /endpoint                       — two-level picker: pool group → endpoint
  /endpoint switch <name>         — switch by name (uses its pool group)
  /endpoint check                 — trigger health check on all pool endpoints
  /endpoint health                — show detailed health info

Pool-group aware (2026-09-05): the menu now shows pool groups first, then the
endpoints *inside the chosen group* sorted by the group's priority
(priority_by_group, fallback endpoint priority). Platforms without a native menu
(anything but Telegram / Feishu) get a grouped text overview.

Feishu (2026-10-06): /endpoint renders interactive cards throughout — bare
command = menu card (one button per group, then per endpoint), switch / check /
health = result cards (title colour follows the outcome). The adapter turns a
button click into a synthetic ``/card button {...}`` command, which this plugin
claims in ``pre_gateway_dispatch`` by the ``apipool`` action key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

API_POOL_BASE = "http://192.168.5.6:5200"

logger = logging.getLogger(__name__)
_CARD_TASKS: set[asyncio.Task] = set()


def _spawn(coro: Any) -> None:
    """后台执行卡片发送/切换（hook 必须立即返回）；异常由 coro 自身兜住。"""
    task = asyncio.get_running_loop().create_task(coro)
    _CARD_TASKS.add(task)
    task.add_done_callback(_CARD_TASKS.discard)


# ── HTTP helpers ──────────────────────────────────────────────────────


def _api_get(path: str) -> Any:
    url = f"{API_POOL_BASE}{path}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _api_post(path: str, body: dict | None = None) -> Any:
    url = f"{API_POOL_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, method="POST", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


# ── Group / endpoint data shaping ────────────────────────────────────


def _ep_group_priority(ep: dict, group: str) -> int:
    """端点在组内的排序键：组内优先级 > 全局 priority。"""
    pbg = ep.get("priority_by_group") or {}
    if isinstance(pbg, dict) and group in pbg:
        try:
            return int(pbg[group])
        except (TypeError, ValueError):
            pass
    return int(ep.get("priority", 1))


def _group_members(endpoints: list[dict], group: str) -> list[dict]:
    """组内端点（in_pool 且声明该组），按组内优先级升序。"""
    eps = [
        e for e in endpoints
        if e.get("in_pool") and group in (e.get("pool_groups") or [])
    ]
    eps.sort(key=lambda e: (_ep_group_priority(e, group), str(e.get("name", ""))))
    return eps


def _all_group_names(endpoints: list[dict], groups: list[dict]) -> list[str]:
    """全部池组名：/api/groups 为准，端点声明兜底；main 恒排第一。"""
    names = []
    for g in groups:
        n = g.get("name")
        if n and n not in names:
            names.append(n)
    for e in endpoints:
        for g in e.get("pool_groups") or []:
            if g and g not in names:
                names.append(g)
    names.sort(key=lambda n: (n != "main", str(n)))
    return names


def _group_is_current(ep: dict, group: str) -> bool:
    return group in (ep.get("current_groups") or [])


def _health_badge(ep: dict) -> str:
    """冻结标记：disabled / cooldown / health fail-bad → 🔴。"""
    if not ep.get("enabled", True):
        return " 🔴"
    if ep.get("in_cooldown"):
        return " 🔴"
    if ep.get("health") in ("fail", "bad"):
        return " 🔴"
    return ""


def _fmt_group_overview(groups: list[dict], endpoints: list[dict]) -> str:
    """纯文本分组概览（非 picker 平台 / 回退）。"""
    names = _all_group_names(endpoints, groups)
    if not names:
        return "⚠️ 无可用池组"
    lines = ["**API Pool 池组**\n"]
    for gname in names:
        members = _group_members(endpoints, gname)
        if not members:
            continue
        cur = next((e for e in members if _group_is_current(e, gname)), None)
        cur_line = f"（当前 {cur['name']}）" if cur else "（未选）"
        lines.append(f"🗂 **{gname}** {cur_line}")
        for ep in members:
            mark = "👈 " if _group_is_current(ep, gname) else ""
            prio = _ep_group_priority(ep, gname)
            lines.append(f"  {mark}`#{prio}` {ep['name']} — {ep.get('model', '?')}{_health_badge(ep)}")
        lines.append("")
    lines.append("> 发送 `/endpoint switch <端点名>` 可直接切换")
    return "\n".join(lines)


def _fmt_health_detail(endpoints: list[dict]) -> str:
    """Detailed health info for pool endpoints only."""
    pool_eps = [e for e in endpoints if e.get("in_pool")]
    if not pool_eps:
        return "⚠️  No endpoints in pool."

    lines = ["**Health Details (pool endpoints)**\n"]
    now = time.time()

    for ep in pool_eps:
        name = ep["name"]
        h = ep.get("health", "unknown")
        lat = ep.get("health_latency_ms", -1)
        err = ep.get("health_error", "") or ""
        last_check = ep.get("health_last_check", 0)
        fail_cnt = ep.get("fail_count", 0)

        if h in ("ok", "pass"):
            icon = "🟢"
        elif h == "slow":
            icon = "🟡"
        elif h in ("fail", "bad"):
            icon = "🔴"
        else:
            icon = "⚪"

        lat_str = f"{lat}ms" if lat >= 0 else "—"
        ago = f"{int(now - last_check)}s ago" if last_check else "never"

        lines.append(f"- **{name}** {icon}")
        lines.append(f"  ├─ Health: `{h}`  Latency: {lat_str}  Last check: {ago}")
        if err:
            lines.append(f"  ├─ Error: `{err[:120]}`")
        lines.append(f"  └─ Fail count: {fail_cnt}  |  Enabled: {'✅' if ep.get('enabled') else '❌'}")

    return "\n".join(lines)


# ── 菜单文本构建 ─────────────────────────────────────────────────────


def _group_menu_text(endpoints: list[dict], names: list[str]) -> str:
    lines = ["**端点切换 — 选择池组**"]
    for gname in names:
        members = _group_members(endpoints, gname)
        if not members:
            continue
        cur = next((e for e in members if _group_is_current(e, gname)), None)
        cur_part = f" · 当前 {cur['name']}" if cur else " · 未选"
        lines.append(f"{gname}{cur_part} · {len(members)} 端点")
    return "\n".join(lines)


def _endpoint_menu_text(group: str, endpoints: list[dict]) -> str:
    members = _group_members(endpoints, group)
    cur = next((e for e in members if _group_is_current(e, group)), None)
    lines = [
        f"**端点切换 · {group}**",
        (f"当前: **{cur['name']}** — {cur.get('model', '?')}" if cur else "⚠️ 未选择工作端点"),
        f"{len(members)} 个端点（按组内优先级）",
    ]
    return "\n".join(lines)


def _group_choices(endpoints: list[dict], names: list[str]) -> list[dict]:
    choices = []
    for gname in names:
        members = _group_members(endpoints, gname)
        if not members:
            continue
        cur = next((e for e in members if _group_is_current(e, gname)), None)
        cur_part = f"· {cur['name']}" if cur else ""
        label = f"{gname} {cur_part}".strip()
        choices.append({
            "label": label,
            "callback_value": f"group:{gname}",
            "is_current": False,
            "full_width": True,
        })
    return choices


def _endpoint_choices(group: str, endpoints: list[dict]) -> list[dict]:
    members = _group_members(endpoints, group)
    items = []
    for ep in members:
        name = ep["name"]
        is_current = _group_is_current(ep, group)
        frozen = _health_badge(ep) != ""
        label = name + (" 🔴" if frozen and not is_current else "")

        item = {
            "label": label,
            "callback_value": f"ep:{group}:{ep['id']}",
            "is_current": bool(is_current),
            "full_width": True,
        }
        items.append(item)
    return items


# ── 会话上下文暂存（hook/命令路径 → picker 回调共享）────────────────

# Telegram 回调只回传 chat_id；发送 picker 时把后续要用的 context 暂存起来。
# 单用户家庭网关：单槽即够；用 chat_id 兜底避免群/多端相互覆盖。
_PICKER_CTX: dict[str, dict] = {}


def _remember_picker_context(source: Any, adapter: Any, context: dict) -> str:
    chat_id = str(getattr(source, "chat_id", ""))
    _PICKER_CTX[chat_id] = {
        "adapter": adapter,
        "chat_id": chat_id,
        "source": source,
        "session_id": str(context.get("session_id") or ""),
        "metadata": context.get("metadata"),
    }
    return chat_id


def _forget_picker_context(chat_id: str) -> None:
    _PICKER_CTX.pop(chat_id, None)


# ── Picker 发送 ──────────────────────────────────────────────────────


async def _send_picker(
    context: dict,
    title: str,
    choices: list[dict],
    *,
    source: Any = None,
    session_key: str | None = None,
) -> str | None:
    """发送单级 choice picker；返回 None 成功 / 错误串。

    回调数据由本模块 `_on_choice_selected` 统一处理；发送前把 context
    记入 _PICKER_CTX，回调据此拿到 adapter 发下一级菜单。
    """
    adapter = context.get("adapter")
    source = source or context.get("source")
    send_picker = getattr(adapter, "send_choice_picker", None)
    if not callable(send_picker) or source is None:
        return "picker-unavailable"

    chat_id = _remember_picker_context(source, adapter, context)

    picker_choices = []
    for item in choices:
        picker_choices.append({
            "value": str(item.get("callback_value") or ""),
            "label": str(item.get("label") or ""),
            "is_current": bool(item.get("is_current")),
            "full_width": bool(item.get("full_width")),
        })
    if not any(c.get("value") == "cancel" for c in picker_choices):
        picker_choices.append({
            "value": "cancel",
            "label": "取消",
            "is_current": False,
            "full_width": True,
        })

    result = await send_picker(
        chat_id=source.chat_id,
        title=title,
        choices=picker_choices,
        session_key=session_key or str(context.get("session_id") or ""),
        on_choice_selected=_on_choice_selected,
        metadata=context.get("metadata"),
    )
    if result.success:
        return None
    _forget_picker_context(chat_id)
    return f"❌ 菜单发送失败: {result.error or 'unknown error'}"


async def _open_group_menu(context: dict, endpoints: list[dict], names: list[str]) -> str | None:
    """第一级：选池组。"""
    return await _send_picker(
        context,
        _group_menu_text(endpoints, names),
        _group_choices(endpoints, names),
    )


async def _open_endpoint_menu(context: dict, group: str) -> str | None:
    """第二级：选组内端点（按组内优先级）。"""
    try:
        endpoints = await asyncio.to_thread(_api_get, "/api/endpoints")
    except urllib.error.URLError as e:
        return f"❌ Cannot reach API Pool: {e.reason}"
    except Exception as e:
        return f"❌ Error: {e}"

    members = _group_members(endpoints, group)
    if not members:
        return f"⚠️ 池组 `{group}` 没有可切换端点"
    return await _send_picker(
        context,
        _endpoint_menu_text(group, endpoints),
        _endpoint_choices(group, endpoints),
    )


async def _fetch_pool_state() -> tuple[list[dict], list[dict]]:
    """实时拉取端点与池组定义。"""
    endpoints = await asyncio.to_thread(_api_get, "/api/endpoints")
    groups_data = await asyncio.to_thread(_api_get, "/api/groups")
    groups = groups_data.get("groups", []) if isinstance(groups_data, dict) else (groups_data or [])
    return endpoints, groups


def _active_group_names(endpoints: list[dict], groups: list[dict]) -> list[str]:
    """有成员的池组名（空组不呈现）。"""
    return [g for g in _all_group_names(endpoints, groups) if _group_members(endpoints, g)]


async def _run_endpoint_menu(context: dict) -> str | None:
    """拉取实时状态并呈现两级菜单；单组直接进端点选择。"""
    try:
        endpoints, groups = await _fetch_pool_state()
    except urllib.error.URLError as e:
        return f"❌ Cannot reach API Pool: {e.reason}"
    except Exception as e:
        return f"❌ Error: {e}"

    names = _active_group_names(endpoints, groups)
    if not names:
        return "⚠️ 无可用池组（没有在池内的端点）"

    if len(names) == 1:
        return await _open_endpoint_menu(context, names[0])
    return await _open_group_menu(context, endpoints, names)


# ── 端点切换（picker / 飞书卡片共用）────────────────────────────────


async def _do_switch(group: str, ep_id: str) -> str:
    """切换组内端点，返回结果文案。"""
    try:
        result = await asyncio.to_thread(
            _api_post,
            "/api/pool/switch",
            {"group": group, "endpoint_id": ep_id},
        )
        if not result.get("ok"):
            return f"❌ 切换失败: `{result}`"
        return (
            f"✅ 已切换组 `{result.get('group', group)}` → "
            f"**{result.get('endpoint_name', ep_id)}** "
            f"(`{result.get('model', '?')}`)"
        )
    except urllib.error.URLError as exc:
        return f"❌ 无法连接 API Pool: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - plugin boundary
        return f"❌ 切换失败: {exc}"


# ── 飞书卡片菜单 ─────────────────────────────────────────────────────

# 飞书适配器没有 send_choice_picker：卡片按钮点击由适配器转成
# `/card button {"apipool": "<callback_value>"}` 合成命令，本模块在
# pre_gateway_dispatch 里按 _FEISHU_ACTION_KEY 认领并处理。
_FEISHU_ACTION_KEY = "apipool"


def _feishu_card(
    title: str,
    markdown: str,
    choices: list[dict] | None = None,
    *,
    template: str = "blue",
) -> dict:
    """交互卡片：标题 + 说明 +（可选）每个选项一个按钮（一行一个，长标签不挤）。"""
    elements: list[dict] = [{"tag": "markdown", "content": markdown}]
    for item in choices or []:
        label = ("✓ " if item.get("is_current") else "") + str(item.get("label") or "")
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary" if item.get("is_current") else "default",
                "value": {_FEISHU_ACTION_KEY: str(item.get("callback_value") or "")},
            }],
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }


def _feishu_group_card(endpoints: list[dict], names: list[str]) -> dict:
    """一级：选池组。"""
    lines = []
    for gname in names:
        members = _group_members(endpoints, gname)
        cur = next((e for e in members if _group_is_current(e, gname)), None)
        cur_part = f"当前 {cur['name']}" if cur else "未选"
        lines.append(f"**{gname}** · {cur_part} · {len(members)} 端点")
    return _feishu_card(
        "🗂 端点切换 · 选择池组",
        "\n".join(lines),
        _group_choices(endpoints, names),
    )


def _feishu_endpoint_card(group: str, endpoints: list[dict]) -> dict:
    """二级：选组内端点（按组内优先级）。"""
    members = _group_members(endpoints, group)
    cur = next((e for e in members if _group_is_current(e, group)), None)
    cur_part = f"当前: **{cur['name']}** — {cur.get('model', '?')}" if cur else "⚠️ 未选择工作端点"
    return _feishu_card(
        f"端点切换 · {group}",
        f"{cur_part}\n{len(members)} 个端点（按组内优先级）",
        _endpoint_choices(group, endpoints),
    )


# /endpoint <sub> 的结果卡片：标题与配色由子命令 + 结果首行决定
_FEISHU_RESULT_TITLES = {
    "switch": "🔀 端点切换",
    "check": "🩺 健康检查结果",
    "health": "🩺 端点健康详情",
}


def _feishu_result_title(args: str) -> str:
    sub = args.strip().split(maxsplit=1)[0].lower() if args.strip() else ""
    return _FEISHU_RESULT_TITLES.get(sub, "端点命令")


def _feishu_result_card(title: str, markdown: str) -> dict:
    """文本结果 → 卡片：结果首字符的 ✅/❌/⚠️/❓ 决定标题配色；重复的英文小标题去掉。"""
    lines = markdown.strip().splitlines()
    if len(lines) > 1 and lines[0].strip().startswith("**") and lines[0].strip().endswith("**"):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    template = {"✅": "green", "❌": "red", "⚠": "orange", "❓": "orange"}.get(body.lstrip()[:1], "blue")
    return _feishu_card(title, body, template=template)


async def _run_feishu_command(args: str, adapter: Any, chat_id: str) -> None:
    """带参数的 /endpoint（switch / check / health）→ 结果卡片。"""
    try:
        text = await handle_endpoint(args)
    except Exception as exc:  # noqa: BLE001 - plugin boundary
        text = f"❌ 命令执行失败: {exc}"
    error = await _feishu_send_card(
        adapter, chat_id, _feishu_result_card(_feishu_result_title(args), text or "✅ 完成"),
    )
    if error:
        # 卡片通道失败时至少把内容以文本投递
        await _feishu_reply(adapter, chat_id, f"{error}\n\n{text}")


async def _feishu_send_card(adapter: Any, chat_id: str, card: dict) -> str | None:
    """发送交互卡片；返回 None 成功 / 错误串。走适配器自己的发送通道（重试 + token 缓存）。"""
    try:
        response = await adapter._feishu_send_with_retry(
            chat_id=chat_id,
            msg_type="interactive",
            payload=json.dumps(card, ensure_ascii=False),
            reply_to=None,
            metadata=None,
        )
        result = adapter._finalize_send_result(response, "send card failed")
        if not result.success:
            return f"❌ 卡片发送失败: {result.error or 'unknown error'}"
        return None
    except Exception as exc:  # noqa: BLE001 - plugin boundary
        return f"❌ 卡片发送失败: {exc}"


async def _feishu_reply(adapter: Any, chat_id: str, text: str) -> None:
    try:
        await adapter.send(chat_id, text)
    except Exception:
        logger.warning("failed to deliver feishu reply", exc_info=True)


async def _run_feishu_menu(context: dict, adapter: Any) -> str | None:
    """拉取实时状态 → 发池组卡片；单组直接发端点卡片。"""
    try:
        endpoints, groups = await _fetch_pool_state()
    except urllib.error.URLError as e:
        return f"❌ Cannot reach API Pool: {e.reason}"
    except Exception as e:  # noqa: BLE001 - plugin boundary
        return f"❌ Error: {e}"

    names = _active_group_names(endpoints, groups)
    if not names:
        return "⚠️ 无可用池组（没有在池内的端点）"

    card = (
        _feishu_endpoint_card(names[0], endpoints)
        if len(names) == 1
        else _feishu_group_card(endpoints, names)
    )
    return await _feishu_send_card(adapter, context["chat_id"], card)


async def _run_feishu_menu_and_report(context: dict, adapter: Any) -> None:
    try:
        error = await _run_feishu_menu(context, adapter)
    except Exception as exc:  # noqa: BLE001 - plugin boundary must fail closed
        logger.warning("feishu endpoint menu failed: %s", exc)
        error = f"❌ 端点菜单加载失败: {exc}"
    if error:
        await _feishu_reply(adapter, context["chat_id"], error)


async def _feishu_card_action(value: str, adapter: Any, chat_id: str) -> None:
    """按钮回调：group: → 发二级卡片；ep: → 切换并回报结果。"""
    if value.startswith("group:"):
        group = value[len("group:"):]
        try:
            endpoints = await asyncio.to_thread(_api_get, "/api/endpoints")
        except Exception as exc:  # noqa: BLE001 - plugin boundary
            await _feishu_reply(adapter, chat_id, f"❌ 无法连接 API Pool: {exc}")
            return
        if not _group_members(endpoints, group):
            await _feishu_reply(adapter, chat_id, f"⚠️ 池组 `{group}` 没有可切换端点")
            return
        error = await _feishu_send_card(adapter, chat_id, _feishu_endpoint_card(group, endpoints))
        if error:
            await _feishu_reply(adapter, chat_id, error)
        return

    if value.startswith("ep:"):
        parts = value.split(":", 2)
        if len(parts) != 3:
            await _feishu_reply(adapter, chat_id, "❌ 无效选择")
            return
        await _feishu_reply(adapter, chat_id, await _do_switch(parts[1], parts[2]))
        return

    await _feishu_reply(adapter, chat_id, "❌ 无效选择")


# ── Choice picker 回调（两级共用）────────────────────────────────────

# 状态机：菜单处于一级（选组中）时二级发送由回调异步安排，适配器会先清理
# 一级状态；新 picker 是新消息，因此不会互相覆盖。


async def _on_choice_selected(chat_id: str, value: str) -> str:
    ctx = _PICKER_CTX.get(str(chat_id))
    if value == "cancel":
        _forget_picker_context(str(chat_id))
        return "已取消端点选择"

    if not ctx:
        return "❌ 菜单会话已失效，请重新发送 /endpoint"

    if value.startswith("group:"):
        group = value[len("group:"):]
        # 记录本次一级 picker 的状态对象；适配器会在回调返回后 pop 它。
        picker_state = getattr(ctx.get("adapter"), "_choice_picker_state", None)
        old_state = (picker_state or {}).get(str(chat_id))
        # 不在回调中同步发送二级 picker：等待旧状态真正释放，避免随后
        # 的一级回调清理把新 picker 一并删除。
        async def _send_second_level():
            deadline = asyncio.get_running_loop().time() + 5.0
            while (
                old_state is not None
                and picker_state is not None
                and (picker_state.get(str(chat_id)) is old_state)
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.05)
            try:
                await _open_endpoint_menu(ctx, group)
            except Exception as exc:  # noqa: BLE001 - plugin boundary
                logger.warning("second-level endpoint menu failed: %s", exc)

        _spawn(_send_second_level())
        return f"🗂 已选择池组 `{group}`，请选择端点"

    if value.startswith("ep:"):
        _forget_picker_context(str(chat_id))
        _parts = value.split(":", 2)
        if len(_parts) != 3:
            return "❌ 无效选择"
        return await _do_switch(_parts[1], _parts[2])

    _forget_picker_context(str(chat_id))
    return "❌ 无效选择"


# ── Command handler ───────────────────────────────────────────────────


async def handle_endpoint(raw_args: str, context: Any = None) -> str | None:
    args = raw_args.strip().split()
    cmd = args[0].lower() if args else None

    # ── bare /endpoint — two-level picker: pool group → endpoint ──
    if not cmd:
        # gateway 分发不传 context 时（非 Telegram hook 路径），构造纯文本回退
        try:
            endpoints = await asyncio.to_thread(_api_get, "/api/endpoints")
            groups_data = await asyncio.to_thread(_api_get, "/api/groups")
            groups = groups_data.get("groups", []) if isinstance(groups_data, dict) else (groups_data or [])
        except urllib.error.URLError as e:
            return f"❌ Cannot reach API Pool: {e.reason}"
        except Exception as e:
            return f"❌ Error: {e}"
        return _fmt_group_overview(groups, endpoints)

    # ── /endpoint switch <name> ──
    if cmd == "switch" and len(args) >= 2:
        target_name = " ".join(args[1:]).strip()
        try:
            all_eps = _api_get("/api/endpoints")
            target = next((e for e in all_eps if e["name"].lower() == target_name.lower()), None)
            if not target:
                target = next(
                    (e for e in all_eps if target_name.lower() in e["name"].lower()),
                    None,
                )
            if not target:
                names = ", ".join(e["name"] for e in all_eps)
                return f"❌ Endpoint `{target_name}` not found.\nAvailable: {names}"
            if not target.get("in_pool"):
                return f"❌ `{target['name']}` 不在聚合池内（仅保存端点不可切换）"

            # 分组池：切到端点所属的池组（多组时优先 main）
            groups = target.get("pool_groups") or []
            group = "main" if "main" in groups else (groups[0] if groups else "main")
            ep_id = target["id"]
            result = _api_post(f"/api/switch-endpoint/{ep_id}?group={quote(group, safe='')}")

            if result.get("ok"):
                model = target.get("model", "?")
                return f"✅ Switched → **{target['name']}** (`{model}`) 组: `{group}`"
            else:
                return f"❌ Switch returned: `{result}`"

        except urllib.error.URLError as e:
            return f"❌ Cannot reach API Pool: {e.reason}"
        except Exception as e:
            return f"❌ Error: {e}"

    if cmd == "switch":
        return "❓ Usage: `/endpoint switch <name>`"

    # ── /endpoint check ──
    if cmd == "check":
        try:
            result = _api_post("/api/health-check")
            results = result.get("results", [])

            all_eps = _api_get("/api/endpoints")
            id_to_name = {e["id"]: e["name"] for e in all_eps}

            lines = ["**Health Check Results**\n"]
            for r in results:
                r_id = r.get("id", "")
                name = id_to_name.get(r_id, r_id[:8])
                h = r.get("health", "?")
                lat = r.get("latency_ms", -1)
                err = r.get("error", "")

                if h == "ok":
                    icon = "🟢"
                elif h == "slow":
                    icon = "🟡"
                elif h == "fail":
                    icon = "🔴"
                else:
                    icon = "⚪"

                lat_str = f"{lat}ms" if lat >= 0 else "—"
                lines.append(f"- {icon} **{name}**  `{h}`  {lat_str}")
                if err:
                    lines.append(f"  └─ {err}")

            return "\n".join(lines)

        except urllib.error.URLError as e:
            return f"❌ Cannot reach API Pool: {e.reason}"
        except Exception as e:
            return f"❌ Health check failed: {e}"

    # ── /endpoint health ──
    if cmd == "health":
        try:
            data = _api_get("/api/endpoints")
            return _fmt_health_detail(data)
        except urllib.error.URLError as e:
            return f"❌ Cannot reach API Pool: {e.reason}"
        except Exception as e:
            return f"❌ Error: {e}"

    # ── unknown subcommand ──
    return (
        f"❓ Unknown subcommand: `{cmd}`\n\n"
        "Available:\n"
        "• `/endpoint` — pick pool group then endpoint\n"
        "• `/endpoint switch <name>` — switch active endpoint\n"
        "• `/endpoint check` — health check all pool endpoints\n"
        "• `/endpoint health` — detailed health info"
    )


# ── 菜单入口（hook）：Telegram inline keyboard / 飞书交互卡片 ─────────


def _menu_context(gateway: Any, source: Any, event: Any, platform: str) -> dict:
    return {
        "adapter": gateway._adapter_for_source(source),
        "platform": platform,
        "source": source,
        "chat_id": str(getattr(source, "chat_id", "")),
        "session_id": gateway._session_key_for_source(source),
        "metadata": gateway._thread_metadata_for_source(
            source, getattr(event, "message_id", None)
        ),
    }


def _feishu_action_value(args: str) -> str:
    """从 `/card button {"apipool": "..."}` 合成命令里取出本插件的动作值。"""
    text = args.strip()
    brace = text.find("{")
    if not text.startswith("button") or brace < 0:
        return ""
    try:
        payload = json.loads(text[brace:])
    except ValueError:
        return ""
    value = payload.get(_FEISHU_ACTION_KEY) if isinstance(payload, dict) else None
    return str(value) if value else ""


def _feishu_operator_open_id(event: Any, source: Any) -> str:
    """卡片点击操作人的 open_id：优先取回调原始事件，兜底取适配器写进 user_id 的 open_id。"""
    raw = getattr(event, "raw_message", None)
    operator = getattr(getattr(raw, "event", None), "operator", None)
    open_id = str(getattr(operator, "open_id", "") or "")
    return open_id or str(getattr(source, "user_id", "") or "")


def _feishu_operator_authorized(adapter: Any, event: Any, source: Any) -> bool:
    """卡片点击授权 —— 用适配器自家卡片按钮的口径（_is_interactive_operator_authorized）。

    卡片点击是适配器合成的 COMMAND 事件，其 source 的 chat_type 被标成 group，
    gateway 的授权判定对这类事件一律判否（真实 DM 的授权不适用），所以不能用
    ``gateway._is_user_authorized``；适配器对 approval / update-prompt 卡片点击
    用的就是这个操作人口径。适配器缺该接口时一律拒绝（fail closed）。
    """
    check = getattr(adapter, "_is_interactive_operator_authorized", None)
    if not callable(check):
        return False
    return bool(check(_feishu_operator_open_id(event, source)))


def _feishu_dispatch(event: Any, gateway: Any, source: Any) -> dict | None:
    """飞书入口：/endpoint 发卡片（菜单 / 结果）；/card 认领本插件的按钮点击。"""
    command = event.get_command()

    if command == "card":
        args = event.get_command_args()
        value = _feishu_action_value(args)
        # 不是本插件的按钮（或按钮 value 未回传）→ 让原路径处理。
        if not value:
            logger.info("api-pool: card action not claimed args=%r", args)
            return None
        adapter = gateway._adapter_for_source(source)
        if adapter is None:
            return None
        if not _feishu_operator_authorized(adapter, event, source):
            logger.warning("api-pool card click rejected: operator not authorized")
            return {"action": "skip", "reason": "api-pool feishu card click unauthorized"}
        _spawn(_feishu_card_action(value, adapter, str(source.chat_id)))
        return {"action": "skip", "reason": "api-pool feishu card action handled"}

    if command != "endpoint":
        return None
    # This hook runs before the Gateway authorization stage. Do not consume an
    # unauthorized event; let the normal dispatch path handle pairing/denial.
    if not gateway._is_user_authorized(source):
        return None
    adapter = gateway._adapter_for_source(source)
    if adapter is None or not callable(getattr(adapter, "_feishu_send_with_retry", None)):
        return None  # 适配器不支持交互卡片 → 走 handler 的纯文本输出
    chat_id = str(source.chat_id)
    args = event.get_command_args().strip()
    if args:  # switch / check / health → 结果卡片
        _spawn(_run_feishu_command(args, adapter, chat_id))
        return {"action": "skip", "reason": "api-pool feishu command card sent"}
    context = _menu_context(gateway, source, event, "feishu")
    _spawn(_run_feishu_menu_and_report(context, adapter))
    return {"action": "skip", "reason": "api-pool feishu card menu sent"}


def _pre_gateway_dispatch(event: Any, gateway: Any, **_kwargs: Any) -> dict | None:
    """裸 /endpoint → 原生菜单；飞书卡片按钮点击 → 本插件的动作处理。"""
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "")

    if platform == "feishu":
        return _feishu_dispatch(event, gateway, source)

    if (
        platform != "telegram"
        or event.get_command() != "endpoint"
        or event.get_command_args().strip()  # 仅裸命令走 picker；带参数走 handler
    ):
        return None

    # This hook runs before the Gateway authorization stage. Do not consume an
    # unauthorized event; let the normal dispatch path handle pairing/denial.
    if not gateway._is_user_authorized(source):
        return None

    adapter = gateway._adapter_for_source(source)
    if adapter is None:
        return None

    context = _menu_context(gateway, source, event, "telegram")

    async def _run_and_report_errors():
        """菜单发送失败 / 拉取失败时把错误文本发回会话（picker 已自行回执成功态）。"""
        try:
            error = await _run_endpoint_menu(context)
        except Exception as exc:  # noqa: BLE001 - plugin boundary must fail closed
            logger.warning("endpoint menu failed: %s", exc)
            error = f"❌ 端点菜单加载失败: {exc}"
        if error:
            try:
                await adapter.send(
                    source.chat_id,
                    error,
                    metadata=context["metadata"],
                )
            except Exception:
                logger.warning("failed to deliver endpoint menu error", exc_info=True)

    _spawn(_run_and_report_errors())
    return {"action": "skip", "reason": "api-pool endpoint menu sent"}


# ── Plugin registration ──────────────────────────────────────────────


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_command(
        "endpoint",
        handler=handle_endpoint,
        description="List or switch API Pool endpoints by pool group",
        args_hint="[switch <name>|check|health]",
    )


__all__ = ["register"]
