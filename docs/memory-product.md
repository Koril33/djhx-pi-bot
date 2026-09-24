# 长期记忆与会话摘要：初版产品设计

## 目标与边界

- 个人 Telegram bot：自动提取跨会话稳定事实和偏好，保留 `/memory add` 手动添加。
- 项目进度、临时任务只进入所属会话摘要；长期记忆按 Telegram 用户跨会话共享。
- 仅依赖现有 Python 服务、pi 和 SQLite。维护调用固定为 `deepseek/deepseek-flash`，不随聊天模型切换；摘要调用单独记入费用账本。旧 `.env` 若仍含 `MEMORY_MODEL`，该项会被忽略，可删除。
- 每次模型请求的记忆文本受可配置的条目数、摘要字节数、记忆字节数和总字节数限制。实际 token 由 pi 用量事件记录；这些上限是输入大小上限，不承诺精确 token 数。
- SQLite 备份可恢复记忆和摘要；没有旧 JSONL 时不能逐字还原历史、图片和工具输出。

## 数据流

1. 每轮完成后，SQLite 保存受长度限制的用户文字、插入指令及最终可见回复。启动时将上个进程遗留的 `running` 回合标记为 `incomplete`，保留一次性提醒；不完整回合不参与自动摘要。下一条普通消息会收到提醒，发送成功后才清除提醒。
2. 同一会话积累到配置的完整回合数后，用独立的 `pi --mode rpc --no-session --no-tools` 请求 DeepSeek Flash，将旧摘要与新回合合并，输出新摘要及跨会话记忆候选。只在解析和校验通过后，事务性提交摘要、候选记忆和覆盖进度；失败留待后续重试。
3. 对当前消息从最多 `MEMORY_ACTIVE_LIMIT` 条有效记忆中挑选相关条目。手动置顶优先，随后按文字相关度、最近更新排序。会话恢复时额外放入该会话摘要。
4. Python 将受预算约束的上下文写入只供对应 pi 子进程读取的临时文件；项目内小型 pi `context` 扩展在每次模型请求前临时加入该文本。原始 JSONL 不会因每轮注入而重复保存记忆。
5. `/memory delete` 物理删除记忆并刷新临时上下文；已在旧 JSONL、旧回复或摘要中出现的同一事实不会自动消失。用户可另行重建会话上下文。

## SQLite 初版

| 表 | 主要字段 | 用途 |
|---|---|---|
| `conversation_turns` | id, user_id, session_id, telegram_update_id, user_text, steer_text, assistant_text, has_image, status, interruption_notice_pending, created_at | 摘要前的耐久回合；中断提醒持久化；成功摘要后只保留可配置的近期轮数 |
| `session_summaries` | session_id, user_id, summary_text, covered_turn_id, updated_at | 每个会话一份增量摘要和覆盖进度 |
| `memory_items` | id, user_id, text, source, pinned, content_hash, created_at, updated_at | 跨会话有效记忆；稳定 ID 为未来 FTS5 留接口 |
| `session_recovery` | session_id, pi_session_file, context_text, updated_at | 缺失 JSONL 后当前新文件使用的固定恢复上下文，避免把新回合重复注入 |

## 缺失 JSONL 与迁移

- 加载或切换会话时检查 `pi_session_file` 是否存在。不存在就从 SQLite 摘要及未覆盖的近期回合形成恢复上下文，创建新的 pi 会话文件，原 app 会话 ID 不变，并更新路径及 `session_recovery`。
- 有旧 JSONL 时继续原会话，恢复上下文为空。Pi 自带压缩继续工作，但 SQLite 摘要独立保存。
- 在线备份用 `sqlite3.Connection.backup()` 产生一个一致的 `.db` 文件。迁移记忆只需该数据库；运行新服务器仍需程序、Telegram 和模型凭证。
- 旧 DB 没有任何摘要时，只能新开空的 pi 历史，明确告知用户，不能假称恢复了旧对话。
- 被 kill 的回合可能已调用工具，执行结果不确定，因此不自动重放。`running → incomplete` 只纠正状态并提示用户，不会补出丢失的回复；旧 JSONL 若还在，Pi 仍可能保留部分历史，仅迁移 SQLite 时则无法恢复该回合。

## 管理与费用

- `/memory list [页码]`、`add <内容>`、`edit <id> <内容>`、`delete <id>`、`pin <id>`、`unpin <id>`；`/summary show`、`/summary clear` 管理当前会话摘要。
- 自动候选达到有效记忆上限后停止新增，不自动逐出旧记忆。显式添加在已满时要求先删除或编辑。
- 自动提取跳过明显的密码、API token 和一次性验证码；原始近期回合仍可能含敏感文字，DB 文件维持仅属主可读。
- 摘要模型调用使用独立的 `usage_runs` 来源字段，`/cost` 分列聊天和记忆维护费用。`memory_context_bytes` 是每个聊天回合准备的记忆上下文字节数快照，并非逐次模型调用的累计注入量；一轮含多次模型调用时同一内容可能重复注入。整轮 token 和费用仍按 Pi 用量事件统计，不能从该字节数单独推算记忆费用。
- Telegram 中的 Markdown 表格转换为逐项列表，避免手机窄屏上的等宽块折行；明确的围栏代码块仍按代码显示。

## 可配置默认值

`MEMORY_BATCH_TURNS=5`、`MEMORY_TURN_RETENTION=20`、`MEMORY_ACTIVE_LIMIT=100`、`MEMORY_ITEM_MAX_CHARS=240`、`MEMORY_INJECT_MAX_ITEMS=6`、`MEMORY_INJECT_MAX_BYTES=4096`、`MEMORY_SUMMARY_MAX_BYTES=1536`、`MEMORY_ITEMS_MAX_BYTES=2048`。用户和回复的入库长度及摘要输入长度也由配置控制。默认值只是初始运行参数，均不写入计算逻辑常量。

## 限制与验收

- 摘要是有损的；没有 JSONL 时无法恢复完整文本、图片或工具调用。
- 删除记忆使后续注入停止；正在进行的请求及旧历史可能已读到该事实。需要更强的遗忘时重建相关会话并清理旧文件。
- 原始回合入库前先对已识别的密钥写法脱敏，再按配置长度截断；规则是尽力防护，不能识别所有敏感内容，应保护数据库备份。
- `/summary clear` 清空 SQLite 摘要，但不回退覆盖进度。若旧回合已按保留策略清理，只靠 DB 不能重建被清空的摘要。
- FTS5 初版不启用。有效记忆数量有上限，可先在 Python 中扫描；未来可按 `memory_items.id/text/user_id` 增设索引，中文语义相近查询仍需另行处理。
- 验收覆盖：峰值注入上限、自动提取与幂等重试、删除后不再注入、独立维护费用、只有 DB 的跨服务器恢复、无摘要时的诚实提示，以及旧 SQLite 自动升级。
