# 子组空闲借用 main 工作端点

## 借用资格

普通子组与 main 共享当前工作端点时，main 在该端点空闲满窗口后，子组可将它当正常候选使用；这条资格与整组候选耗尽后的 fallback 分开。

窗口为 main 组 `idle_seconds`，0/缺省表示关闭。`_ep_idle_ready(ep)` 只比较端点 `_last_activity_by_group[main]` 与窗口；main 未使用过该端点视为一直空闲。子组自身与其他子组活动保持独立，借到后可连续使用，main 再活动后重新避让窗口。

main 在途由 `_is_ep_inflight_elsewhere` 单独静默剔除。空闲时间比较与在途约束分别执行；main / dedicated 豁免遵循既有单向约束，子组之间各自可用性按其路由判断。

## 挂载与释放

`_is_ep_sticky_elsewhere` / `_group_sticky_candidates` 命中 main 工作端点避让时，只有 `group in _groups_pointing_at(ep.id)` 才挂本组借用 defer。其他组当前目标在别处时，仅剔除共享候选；本组原本使用共享端点且被接管，才存在回迁资格。窗口关闭或该组配置主动轮换时沿相应入口跳过借用回切挂载。

`_defer_until_by_group` 与 `_defer_borrow_by_group` 按组保存等待。首次迁移打挂载事件，等待期重复请求保持静默。

`_reconcile_deferred` 区分：

- 借用型：接手端点为本组当前指针解析到的另一端点，须有成功时间且自身停走满回迁窗口，当前默认 300 秒。指针为空或仍指向被避让端点时尚未交接，续期等待；接手端点的 deferrable 与此等待无关。
- 缓存保护型：沿当前工作端点保护开关和全池活跃时间判定，保持独立条件。

释放时按有效恢复名次回写组指针；借入子组名次解析见 [子组契约](subgroup-join-main-contract.md)。窗口期内手动切换清本组所有等待态和恢复登记，该次回迁作废，见 [手动契约](manual-switch-cooldown-noop-2026-09-06.md)。

## 验证契约

窗口用状态注入构造黄金样本，分别验证 main 真活动、满窗口、本组并发、他组并发、main 在途、无真实交接、接手端点未开缓存保护和手动清除。实际回迁核对释放后下一次业务目标与在途归还，日志行单独作为事件证据。

窗口实测先识别验证者自身流量是否持续刷新 main，隔离流量或选自然样本。正式入口 `test/test_group_idle_borrow.py`、`test/test_group_idle_input.js`、`test/test_defer_group_isolation.py`、`test/test_retry_recovery_scope.py`，命令见 [testing](../testing.md)。生产窗口值与现场配方由维护技能承载。
