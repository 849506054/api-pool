# 上游借鉴改进设计 v1（抖动退避 / 客户端错误分类 / 配置原子写）

状态：已批准（2026-08-27）——三项并入 PROJECT.md 待办，按优先级排期实施；
改动二采用「轮转但不记账」方案（A′）。实施前如上游代码已变动，以最新代码重新核对锚点行号。
来源：ai-switch（ijry/ai-switch）流量层机制借鉴，逐条对照本仓库现行实现。

---

## 改动一：确定性抖动冷却时长

### 现状
- `_set_cooldown()`（api_pool_server.py:1488–1494）：固定 `max(cooldown_minutes, 1)` 分钟。
- 已有幂等保护（1491 行）：冷却中不刷新窗口，抖动只需作用于首次冻结时的时长计算。
- 池内 P1–P3 多个同供应商、同模型端点（生产 27 个端点，多个 deepseek-v4-flash 源），
  同批故障→同批冻结→同批解冻→瞬间再打挂上游，即惊群形状。

### 设计
- 仅在 `_set_cooldown()` 计算窗口时乘以确定性系数：
  ```
  jitter_pct = 80 + int(sha256(f"{ep.id}:{int(ep._fail_count)}").hexdigest(), 16) % 41   # 80..120
  cd_seconds = max(cooldown_minutes, 1) * 60 * jitter_pct / 100
  ```
- 种子用 `ep.id + 当前 fail_count`：不同端点解冻时间必然错开；同一端点同一档位可复现，便于排查与测试。
- 用 hashlib 而非 random：跨重启可复现，无全局随机态。

### 明确不做
- **配额/余额通道不参与抖动**（`_set_capacity_cooldown`，1469–1486）：quota 冷却对齐上游声明的
  reset 时间，是精确窗口；balance 是手动解锁语义。二者不受影响。
- 探活短冷却路径不动（保持现有 probe 语义，1072bd8 刚收口过）。

### 测试
- 单测：同一 fail_count 下多次调用时长一致（确定性）；不同端点同时冻结时 `_cooldown_until` 两两不同；
  幂等性质保持（冷却中重复失败不延长）；capacity 通道窗口不含抖动。

---

## 改动二：客户端类错误分类（不损伤端点健康）

### 现状
- 非 2xx 且非 5xx 的响应（如 400/404），`_try_endpoint` 的 HTTPError 分支在排除 5xx 后直接
  `return None, msg`（2847 行），进入 `chat()` 失败处理 → 冻结 + 轮转 + fail_count += 1
  （`_rotate` 内 1733 行）。一个畸形请求可以给好端点记一次健康失败并触发冷却。
- 历史上两次真实伤害均已用特例补丁修复：auto-strip temperature/top_p（8c58aef，_try_endpoint 内部
  重试，2826–2837）、tool_call_id_prefix 重写（9303572，发送前改写）。缺一条兜底通用规则。

### 分类规则
```
client_class := HTTP 状态码 ∈ {400, 404, 413, 422}
                且 error 不含已由内部重试处理的 workaround 标记
                且 响应体不含已知瞬态标记（如 rate/quota/balance 字样）
```
- 温度剥离、tool_call_id 两个既有特例完全不动（它们在到达分类器之前已各自消化）。
- 鉴别为瞬态字样的 4xx 维持现有瞬时失败处理不变。

### 处置（推荐方案 A′）
- client_class 失败：**不冻结、fail_count 不增、不触发探活**；
- 同请求内仍继续轮转其余候选（成本极低——400 秒拒，每候选毫秒级），保留跨端点模型回切能力；
- 全部候选同类失败时按现状返回最后的错误给客户端。
- 理由：池是多供应商同模型链，少数场景下某端点对该模型报 400 而其他端点可服务（模型名差异），
  完全不轮转（ai-switch 原方案 A）会牺牲这种可用性；而轮转的健康损伤正是本改动要消除的。

### 实现位置
- 新增 `_classify_client_error(error_msg) -> bool`；
- 在 `chat()` 失败分支（2091–2120 区域）判断后走 `_rotate(..., skip_cooldown=True)` 并跳过
  `_rotate` 内的 fail_count 累加（为 `_rotate` 加 `health_impact=True` 默认参数，
  False 时不增计数、不打日志级别 ERROR 改 WARN）。
- DEBUG trace 增加 `kind: "client_error"` 事件，遵循 P0 边界（只在 debug_trace 存在时记录）。

### 测试
- 单测：构造 mock 上游恒返 400 → 该端点 `_cooldown_until` 不变、fail_count 不变、请求最终返回错误；
  混合池（一正常一 400）时正常端点接管成功；既有 temperature/auto-strip 回归测试全绿。

---

## 改动三：save_config 原子写

### 现状
- `save_config()`（2974–2976）裸 `open("w")` 截断覆盖；进程中途被杀会留下半截 JSON，
  下次启动 `load_config()` 异常静默回落空列表 → 全部端点丢失视角。
- 同文件 `save_runtime_state()`（2949–2966）已是 tmp+fsync+os.replace 正确范式。

### 设计
- save_config 改为与 save_runtime_state 同构：写临时文件 → flush+fsync → os.replace 原子替换。
- 临时文件放同目录（保证同一文件系统 replace 原子性），命名 `.api_config.json.tmp`。
- 异常时不吞错：写失败向上抛（调用方在 API handler 内已有异常边界）。

### 测试
- 单测：正常写入后配置完整可读；模拟 replace 前中断不留半截目标文件；并发写不产生损坏文件。

---

## 交付协议

1. 开发机 worktree 实现，逐项独立提交（三笔 commit，互不纠缠）；
2. ruff check → py_compile → 定向单测 → 既有测试套件回归；
3. mock 上游 E2E：验证改动二混合池行为与流式首包超时路径未受影响；
4. 通过后经用户确认再部署宿主机 5200（推文件+重启+hash 确认，不做生产探测，
   实测由用户在真实工作中进行）；
5. PROJECT.md 对应里程碑置 [x] 并登记变更。

## 影响面
- 改动一、二触碰路由核心路径的边缘分支，不改请求成功路径语义、不改流式事务层；
- 改动三独立于业务逻辑；
- 三项均不触碰敏感词过滤、P0 观测边界与 sticky 端点选择逻辑。
