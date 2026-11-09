# 手动切换与故障恢复归属

## 单次用户断言

手动切换把该组当前指针写到指定目标，表示用户断言现在使用它。`switch_to_endpoint` 调 `_reset_endpoint_assertion_state` 清冷却、失败计数、上次错误和人工解冻标记，健康变为 unknown，由随后真实请求重新分类。配置声明 `enabled` / `in_pool` 与既发用量约束保持；本组停用标记在显式切换时解除。

`_clear_group_waiting_state(group)` 清该组在所有端点上的延迟回切/借用 defer、组级回切锁、待回切标记和恢复登记。当前机制只存组指针，用户动作作废本组旧等待意图，后续自动轮换与故障处理继续生效。

普通组切专用子组整档同构，`switch_main_to_subgroup` / `switch_group_to_subgroup` 清父组等待态、写子组名指针并重置子组内端点观测态；子组成员、优先级和子组自身指针保持。全冷却子组可按显式用户断言切入。

## 接口与持久性

优先使用 `POST /api/pool/switch`，body 为 `{group, endpoint_id}`。缺参数 400，组或端点不存在 404，成员资格或可切换约束冲突 409；成功返回目标组、端点与模型。

兼容 `POST /api/switch-endpoint/<id>?group=<组>` 的组参数决定作用域，省略按 main 解释。给其他组切换时显式传目标组。

指针落 `api_runtime_state.json` 的 `groups.<组>`，冷却快照随切换同步保存。持久化成功、状态接口目标与后续业务实际目标分别核对；HTTP 成功本身不能证明下一次请求选中了该目标。调整组优先级改变无有效当前指针时的选站次序。

## 故障恢复

恢复回切限定在实际故障交接的组。`_rotate` 与 `_set_current` 登记 `_recovery_groups` 和 `_recovery_slots`，后台恢复按登记消费；未登记时仅恢复可用性。共享端点故障归 main 时，其他未交接组保持原指针。

手动切换清本组各端点登记，写指针使用 `record_recovery=False`；清除和恢复消费由同一把锁保护，迟到请求受 route epoch 约束。登记随未过期冷却快照保存，改名/退组与名次变化按有效成员关系维护。父组指针为子组名时，恢复资格和实际目标见 [子组契约](subgroup-join-main-contract.md)。

## 定位与测试

判断指针变化先对照目标组、操作时间、恢复登记与后续业务目标，再区分手动切换、延迟释放和故障回切。日志声明只证明执行到该行，业务请求目标才证明选站。

正式入口：`test/test_manual_switch_health_reset.py`、`test/test_persistent_current_endpoint.py`、`test/test_retry_recovery_scope.py`、`test/test_defer_group_isolation.py`、`test/test_group_join_main.py`。命令与证据分层见 [testing](../testing.md)。
