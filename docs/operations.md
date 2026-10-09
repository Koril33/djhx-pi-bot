# 运行、备份与故障恢复

本页描述当前代码的运维行为。某次部署是否通过线上验证，以部署日志、服务 revision 和实际测试记录为准；实现合并不等于已经部署。

## 运行状态

`/healthz` 同时报告当前就绪状态、历史故障记录、后台活动与备份状态。就绪要求 Telegram 已启动且最近探测正常、更新 worker 正常、待处理队列没有超过阈值的阻塞；未就绪返回 HTTP 503。历史未处理事故单列在 `incidents.unacknowledged`，核对后可在后台标记已处理，保留更新 ID 去重，不重放原消息。

Telegram 初始化、`getMe`、启动和命令菜单遇到网络异常时按退避重试，默认从 3 秒增加到最多 60 秒；运行后每 60 秒探测一次。无效 Token 会明确失败。更新 worker 异常退出后默认 3 秒再启动。配置见 [.env.example](../.env.example)。

模型执行与 Telegram 送达分开记录。网络导致最终发送失败时，已经生成的文字仍保存在 SQLite；`/resend` 默认查找最近待补发结果，没有待补发结果时可取回最近结果，也可指定结果 ID。补发发送保存的文字，不再次调用模型或工具。有消息 ID 的段落优先编辑，发送超时且没有消息 ID 的段落要确认可能重复后再补发。重启遗留的生成过程标为中断，部分文字会明确标为部分结果。

模型接口失败与 Telegram 发送失败分别排查。已经执行过文件修改、发信等工具的回合，不应靠重新投递旧更新来恢复；先检查持久化结果和副作用状态，再用新的中性消息验证连接。

## 完整备份

```bash
uv run python -m scripts.backup_bundle --reason manual
uv run python -m scripts.restore_backup data/backups/daily/<归档文件>.tar.gz --verify-only
```

归档包含一致 SQLite 快照与 `PI_SESSION_DIR` 下所有完整 JSONL 文件。生成时检查数据库完整性、解析 JSONL，并记录每个文件的大小和 SHA256。POSIX 上目录与归档权限分别为 0700、0600。数据库含用户消息、记忆、摘要、邮件草稿和送达记录，应按私人数据保护；旧版本备份还可能含 SMTP 明文密码。

服务默认在首次没有备份或最近成功备份满 24 小时后尝试备份。备份期间暂停新普通消息的处理，控制消息仍可调度；等待更新处理、Pi 回合与记忆维护空闲，最多 120 秒。忙碌或失败时延后 60 秒再尝试。部署前也生成完整备份。手工运行备份命令前，需要自行确认没有正在写入的任务。

保留策略是最近 7 份 daily 快照，以及最近 4 个 ISO 周各自首份 weekly 快照；不是按“超过 7 天”删除。后台概览与 `/healthz` 显示最近成功时间、大小及错误。`scripts.backup_db` 只备份 SQLite，可用于数据库维护，但不能逐字恢复完整 Pi 会话。

本机归档不包括 `.env`、Pi 私有模型凭据、项目工作文件，或没有写入 JSONL 的外部附件。迁移时单独保护和配置这些内容。异机备份需要另行安排；同一磁盘上的归档无法应对服务器整盘丢失。

## 恢复到新目录

```bash
uv run python -m scripts.restore_backup <归档文件>.tar.gz --destination /home/pi/restore/pi-bot
```

恢复先验证归档路径、manifest、大小、SHA256、SQLite 与 JSONL，随后一次性公布新的目标目录；目标必须不存在，不覆盖线上文件。新 manifest 保存绝对 `source_session_dir`。只有严格位于原会话目录且确实在归档中的 JSONL 路径，才会在恢复数据库的 `sessions` 和 `session_recovery` 中重绑定到目标目录的 `pi-sessions/`。

命令返回的 `restore.manual_rebind=true` 表示仍有路径需人工核对，例如旧归档未记录原目录、引用了外部文件或归档缺少对应文件。此时不能直接把恢复结果视为完整迁移。`manifest.json` 保留原归档内容；`restore-metadata.json` 另存恢复前后数据库 SHA256、重绑定数量及人工核对标志。`--verify-only` 不修改数据库。

核对恢复结果后，把 `DB_PATH` 指向目标目录的 `bot.db`，把 `PI_SESSION_DIR` 指向目标目录的 `pi-sessions`，通过既有 `deploy/deploy.sh` 流程重启，再检查健康、会话与一条中性消息。重绑定后不要直接移动恢复目录，否则需要重新核对路径。启用前保留当前部署的数据快照，便于回退。

## 邮件与交互确认

生产 SMTP 密码位于项目 `.env` 的 `SMTP_PASSWORD`，SQLite 只存环境引用，启动会迁移旧明文并清理数据库页。后台保存保留其他环境配置，POSIX 权限为 `0600`；留空保留当前密码。迁移服务器时单独配置此值，完整归档不包含 `.env`。旧备份中原有明文不能由这次迁移自动清除。

Pi 的发信工具只创建持久化草稿。用户预览并确认后才连接 SMTP；草稿跨重启保留，24 小时后不可发送。`/mail history` 和 `/mail status <ID>` 区分成功提交、明确拒绝和结果不确定；重启遗留的 `sending` 归为不确定，不自动重发。SMTP 接受不能证明最终入箱，真实投递需查看授权收件箱。

语音先在本机转写并预览，确认才提交当前会话；音频说明文字会一起提交。首次使用运行 `uv run python -m scripts.prepare_voice` 下载并校验固定模型；平时转写使用本地文件。默认限制 60 秒、10 MiB。PDF 默认限制 5 MiB、30 页，提取最多 30000 字符，扫描件不做 OCR。媒体解析在受限子进程中执行，超时或 `/stop` 会结束准备过程。

Pi 扩展的确认、选择、输入与编辑框通过按钮或 `/answer <ID> <内容>` 回答，默认两分钟有效。语音和图片继续处理入口按用户、原会话及一次性确认校验；会话切换或入口过期时，应重新预览或重新提交。

## 部署与检查

遵守 [AGENTS.md](../AGENTS.md) 的 Git 与部署约定。服务器部署目录的改动必须提交并推送；脏工作区会阻止自动部署。部署脚本在独立进程中执行同步、自检、完整备份、重启和就绪检查，保存 `data/deploy-state/` 状态；失败目标不会反复触发启动追赶。需要系统操作时使用部署脚本。

```bash
uv lock
uv run python -m unittest discover -s tests -v
uv run python -m py_compile *.py
```

部署后核对服务 active、`/healthz` 当前就绪、revision 与目标提交、后台读取和真实中性回合。邮件等外部副作用只使用明确授权的测试对象，记录 SMTP 接受与最终入箱两项结果。
