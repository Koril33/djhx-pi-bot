# 长期记忆与会话摘要

## 目标与边界

- 个人 Telegram bot：自动提取跨会话稳定事实和偏好，保留 `/memory add` 手动添加。
- 项目进度、临时任务只进入所属会话摘要；长期记忆按 Telegram 用户跨会话共享。
- 仅依赖现有 Python 服务、pi 和 SQLite。维护调用固定为 `deepseek/deepseek-flash`，不随聊天模型切换；摘要调用单独记入费用账本。旧 `.env` 若仍含 `MEMORY_MODEL`，该项会被忽略，可删除。
- 每次模型请求的记忆文本受可配置的条目数、摘要字节数、记忆字节数和总字节数限制。实际 token 由 pi 用量事件记录；这些上限是输入大小上限，不承诺精确 token 数。
- SQLite 备份可恢复记忆、摘要、草稿与已保存回复；完整 Pi 历史另需 JSONL。服务的完整备份工具同时归档两者，不包含 `.env`、模型凭据或项目文件。

## 数据流

1. 每轮完成后，SQLite 保存受长度限制的用户文字、插入指令及已生成回复；生成完成与 Telegram 送达分别跟踪，不能把入库文字当作已送达。启动时将遗留 `running` 回合标为 `incomplete` 并保留一次提醒；不完整回合不参与自动摘要。下一条普通消息提醒成功后才清除提醒。已有完整或部分回复可独立补发，不重新执行原任务。
2. 同一会话积累到配置的完整回合数后，将批次完整文字按 UTF-8 字节分片。每片用新的独立、无会话、无工具、无扩展 Pi 请求 DeepSeek Flash，将上片中间摘要与新片段合并，优先保留最新决定。每次输入与回复都受预算约束，避免 RPC 前轮历史绕过预算。只有全部片段解析成功后，才事务性提交最终摘要、候选和完整回合覆盖进度；任一片失败不推进进度、不提交部分记忆，后续仍可读取完整待处理回合。
3. 对当前消息从有界有效记忆中挑选相关条目。手动置顶优先，随后按中文短语/英文关键词匹配及更新时间排序；零匹配的非置顶条目不注入。会话恢复时额外放入受预算限制的摘要与近期问答。
4. Python 将受预算约束的上下文写入只供对应 pi 子进程读取的临时文件；项目内小型 pi `context` 扩展在每次模型请求前临时加入该文本。原始 JSONL 不会因每轮注入而重复保存记忆。
5. `/memory delete` 物理删除记忆并刷新临时上下文；已在旧 JSONL、旧回复或摘要中出现的同一事实不会自动消失。用户可另行重建会话上下文。

## SQLite

| 表 | 主要字段 | 用途 |
|---|---|---|
| `conversation_turns` | id, user_id, session_id, telegram_update_id, user_text, steer_text, assistant_text, has_image, status, interruption_notice_pending, created_at | 摘要前的耐久回合；中断提醒持久化；成功摘要后只保留可配置的近期轮数 |
| `session_summaries` | session_id, user_id, summary_text, covered_turn_id, updated_at | 每个会话一份增量摘要和覆盖进度 |
| `memory_items` | id, user_id, text, source, pinned, content_hash, created_at, updated_at | 跨会话有效记忆；稳定 ID 为未来 FTS5 留接口 |
| `memory_maintenance_status` | user_id, added, refreshed, skipped_full, updated_at | 最近自动新增、精确重复刷新与满额跳过状态 |
| `summary_job_status` | session_id, user_id, last_success_at, last_failure_at, last_failure_kind | 摘要维护结果，仅保存安全故障类型 |
| `session_recovery` | session_id, pi_session_file, context_text, updated_at | 缺失 JSONL 后当前新文件使用的固定恢复上下文，避免把新回合重复注入 |

## 缺失 JSONL 与迁移

- 加载或切换会话时检查 `pi_session_file` 是否存在。缺失时先展示部分/空白恢复范围，不改动会话；用户确认且预览版本未变化后才新建 Pi 会话文件、更新路径与 `session_recovery`，原 app 会话 ID 不变。
- 恢复同时存在旧摘要和新回合时，最新回合至少有一半恢复预算。按最新回合优先选择，问题、插入指令与回复保留头尾，省略部分明确标记；长旧摘要不能挤掉全部最新决定。
- 有旧 JSONL 时继续原会话，恢复上下文为空。Pi 自带压缩继续工作，但 SQLite 摘要独立保存。
- 只迁移记忆可用 `scripts.backup_db` 的一致 `.db` 文件。完整迁移使用 `scripts.backup_bundle` 归档 SQLite 和 JSONL，并用 `scripts.restore_backup` 校验后恢复到新离线目录。新格式按原会话根目录重绑定实际归档路径；未归档/外部路径及旧格式明确要求人工核对，详见 [备份运维说明](operations.md)。新服务器仍需另行配置程序、Telegram、管理员与模型凭据。
- 旧 DB 没有任何摘要时，只能新开空的 pi 历史，明确告知用户，不能假称恢复了旧对话。
- 被 kill 的回合可能已调用工具，执行结果不确定，因此不自动重放。`running → incomplete` 只纠正状态并提示用户，不会补出丢失的回复；旧 JSONL 若还在，Pi 仍可能保留部分历史，仅迁移 SQLite 时则无法恢复该回合。

## 管理与费用

- `/memory list [页码]`、`search <关键词>`、`add`、`edit`、`delete`、`pin`、`unpin` 管理记忆；列表有来源与更新时间。`used [list|回合ID]` 查询实际注入文本和依据，`auto [status|on|off|default]` 调整个人自动提取偏好；暂停新增记忆后，会话摘要仍更新。
- `/memory capacity`、`manage`（或 `tidy`）显示容量与候选；`merge <ID1> <ID2> [整理后的内容]` 先展示两条原文及保留内容，点击确认才执行，预览十分钟有效且校验条目版本。后台也提供候选与可编辑保留内容。整理保留任一置顶状态、标记人工来源，只删除明确选中的旧条目；旧 JSONL 不被改写。
- 候选基于措辞归一、近重复及否定/数值/风格差异，可能误判，不等同于语义真值判定。所有纠错与合并均由用户核对；不自动将“较新”当作正确事实。
- 自动候选达到上限后不新增、不逐出。精确重复仍刷新日期并保留人工文本、来源与置顶。容量与最近因满额跳过的数量明确显示；显式添加在已满时要求先整理、删除或编辑。
- `/summary show`、`status`、`clear` 查看摘要、待处理完整回合、上次成功/失败与安全故障类型。清空须确认；旧预览版本变化后拒绝执行。
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
- 当前不引入 FTS5 或向量数据库，两人规模使用有界本地扫描和人工整理。短语匹配不是完整语义检索，不相关但同词的条目仍可能命中，可用 `used` 核对。
- 单元验收覆盖：长中文回合全量分片、片段失败不推进覆盖、旧摘要满额仍保留最新决定、零匹配不注入、满额精确重复刷新但不逐出、人工确认合并/置顶保留/版本与用户隔离、独立维护费用和 SQLite 迁移。实际部署与外部调用验证另记实施记录。
