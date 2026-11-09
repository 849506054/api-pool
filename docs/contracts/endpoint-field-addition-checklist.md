# Endpoint 配置字段接线与持久化

## 后端契约

新增可配置字段先进入 `Endpoint` 数据类，定义默认值、类型和 0/空语义；`_ep_to_dict` 输出供编辑回填与 `/api/endpoints` 读取。`_endpoint_config_record` 从 `Endpoint.__dataclass_fields__` 推导落盘，排除下划线运行态字段，api_key 使用完整值；`_sync_to_config` 使用该记录。

add/update 的通用赋值与 HTTP body 透传沿现有路径。新增顶层配置另核 `save_config` 从空 dict 重建的输出集合，顶层键各自接入。配置写入是同目录临时文件、flush/fsync、os.replace 的原子替换。

运行态值、PUT 成功或 GET 回读只证明相应层次；持久化验收须覆盖写入 → 磁盘 JSON → 重新加载/隔离重启回读。正式入口是 `test/test_endpoint_config_persistence.py`、`test/test_endpoint_thinking_persist.py` 与 `test/test_atomic_config_write.py`。

## 前端契约

1. 表单 HTML 按字段职责放对应行空位，行满另开行，控件 id 唯一。
2. `openAddModal` 设置默认值。
3. `editEndpoint` 按语义回填，显式 0/false 与缺省分别处理。
4. `saveEndpoint` / 草稿读取收集类型化值，编辑模式只提交字段差异。

引用全部按 id，改布局只调整 HTML。结构自检确认承载行、各回填/收集位置和移动断点；复杂 JSON 映射用结构化下拉行编辑器，宽度足以阅读，set→collect 往返保持值。关闭/枚举字段用中文选项的原生 select。批量添加 base 当前只透传通用字段，所需新字段按批准范围单独接线。

## 取值约束变更

修改范围时核对唯一归一化函数及 add/update/加载三条路径，保留类型纠错、下限与默认语义；前端 min/max 与实际后端约束一致。建议文案与硬约束分别表达。测试更新采用后的断言，覆盖非法值、边界和加载往返；说明新范围与重试预算等下游约束的交互。

## 验证与来源状态

新增行为持有原版黄金样本，目标字段真实驱动相应逻辑；单测、表单 DOM、接口、落盘和隔离重启各证明各自一层。按 [testing](../testing.md) 选择命令。

字段审计工具由维护技能提供：`api-pool-management/scripts/audit_endpoint_config_fields.py`；维护技能的 `references/endpoint-field-addition-checklist.md` 给出本机执行路径与通过信号。工具只核数据类推导和 API 输出覆盖；落盘、重载与行为由上述正式测试证明。
