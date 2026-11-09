# 普通组引用专用子组

## 引用与名次

专用组 `type=dedicated` 可作为完整条目加入 main 或其他普通 mixed 组。一层引用形态为普通组 → 专用组 → 端点，vision 保持内置独立组。同一专用组可加入多个普通组；各父组分别保存名次与当前指针，共享子组内部的当前端点。

成员关系存组定义的 `priority_by_group`；main 的历史 `main_priority` 按 main 名次兼容读入。加入默认追加末位，名次为原生端点与子组聚合条目共同的 1..N 连续轴，子组按一个成员计数。子组内端点沿用自身组内优先级与参数。

## 父轴名次作为路由单位

`_group_axis_slots(group)` 定义有序名次列表，原生端点各一档，引用子组整档一档。`_parent_slot_routes(group, active, slot_id, failed_ep, exclude)` 从失败名次继续向下，走完再回绕。

名次内优先走子组当前成员；失败后先环试同组兄弟，子组耗尽才进入父轴下一档。无引用子组的组保持原全局 ring。`_group_sticky_candidates` 计算物理可用并集，名次身份由路由参数承载；同一端点按名次顺序仅尝试一次，兼容一个端点同时属于多个子组。

`_guard_empty_stream` 向 `_rotate` 传 `route_slot`，空流回调与迟到失败按相同名次处置。

## 指针、恢复与并发

父组当前指针可以是原生成员端点 ID、已加入子组名，或兼容读入的子组成员端点 ID。后者恢复为有效子组名；下钻使用 `_resolve_subgroup_current`。正常成功保留父组整组指针并更新子组成功成员，后续请求复用该成员。

`Endpoint._recovery_groups` 登记实际故障交接的组，`_recovery_slots[父组]` 登记原名次；两者在交接、组改属、移出、defer 清除、手动切换与冷却快照恢复中同生命周期。登记覆盖父组指针为子组名的形态，恢复反查有效挂载的父组，回切到原名次。

加入、调序、退出与子组切换推进 route epoch。共享子组切换作废所有依赖父组的在途路由，包含父组首次请求、指针尚空的窗口。`_on_success` 回写前检查父组 epoch 与成员资格；迟到结果仍交付响应和成功记账，路由与轮换回写按有效性决定。

手动切到子组由 `switch_main_to_subgroup(..., group=...)` 处理：清目标父组等待态与恢复登记、写整组指针、重置子组成员观测态，成员、优先级和子组自身指针保持。子组全冷却时同样可通过用户断言切入。改名、删除和移出经组键清理维护父组引用与指针。

## 状态与前端

`_subgroup_raw_available` 计算子组物理可用成员，整组冻结判断使用这一出口。`all_frozen_remaining` 取成员最短剩余冷却；全冷却名次自动退出候选，聚合条目呈现冻结状态。

`_resolve_subgroup_current` 是下钻统一出口；`_display_pointer_id` 为端点列表、组摘要和聚合链提供同源当前成员。main 的服务子组由 `_main_serving_subgroup` 根据父组有效目标判断，`/api/chain` 的 `groups.main.current_subgroup` 驱动前端当前子组标记。候选存在时有效指针可回落，候选为空时才显示空态。

合成聚合条目的 `pool_groups` 只包含所属父组，保持父组视图与子组直接成员视图隔离。池列表条目复用 `ep-item`，组名大写紫色，状态位置与原生端点一致；优先级、切换、编辑和移出在成员卡片，聚合链负责展示。整组冻结时条目在 ⏳ 右侧给出 ⏰，逐个对冻结成员调用端点 `clear-error`（组冻结是成员状态的派生值，不设组级接口）。

## 整组在本父组停用

`toggle_subgroup_disabled(子组, 父组)` 与端点组级停用同构：停用记录在子组定义的 `disabled_groups`，成员关系与父轴名次保持，只退出该父组的候选、归属与显示指针。`_sub_disabled_in` 是唯一判据，`_axis_subgroup`、`_group_sticky_candidates`、`_parent_slot_routes`、指针与展示解析按它跳过该档；子组自身与其他父组不受影响。停用态禁止手动切到该子组整体。

停用时会清掉正在指向该子组（或其名下成员）的父组指针，下个请求重新解析。移出父组、改名与删除按 `_retire_group_key` 清理该键。池卡片以 `⏸` / `▶` 切换，停用时置灰并显示「本组停用」，聚合链合成条目带 `disabled_in_group`。

组编辑的加入控件每次选择一个普通目标组；已加入目标置灰，空白选择保持普通编辑，移出后恢复可选。专用组筛选标记按 `type=dedicated` 判定。

组的 `context_tokens` 保持各自显式声明，异构成员实际容量由上下文检查和客户端处理，父组声明保持配置原值。

## 响应侧推理证据

`_reasoning_detected(reasoning_text, reasoning_tokens)` 统一判定文本或正计数证据。`_chunk_reasoning_text` 提取 `reasoning_content`、`reasoning` 和 `reasoning_details[].text`，同时用于累积、`_is_business_chunk` 首包 gate 与 `_business_activity` 停滞时钟。采集失败或正文全空的无效证据用 NULL；有效采集才区分 0/1。

真实工具续轮与仅携带 tools 的普通问答分别验证。上游仅返回计数、或工具调用时剥离推理内容，按真实响应证据记账。

## 接口与测试

组 API 的 `join-group` / `leave-group` / `switch-group` / `toggle-group-disabled` 动作带目标组参数；main 的 `join-main` / `switch-main` 是兼容入口。端点手动切换与持久化见 [手动契约](manual-switch-cooldown-noop-2026-09-06.md)。

正式入口：`test/test_parent_slot_failover.py`、`test/test_group_join_main.py`、`test/test_group_join_parent.py`、`test/test_group_management.py`、`test/test_retry_recovery_scope.py`、`test/test_reasoning_detected.py`、`test/test_business_stall_detection.py`、`test/test_group_membership_ui.js`、`test/test_subgroup_disabled_ui.js`、`test/test_subgroup_unfreeze_ui.js`。其中并发反例走真实 `chat()` 回调路径；具体选择见 [testing](../testing.md)。
