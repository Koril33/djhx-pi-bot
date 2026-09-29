# 用户体验改进实施记录

用于跨次工作恢复。范围：2026-09-28 代码审查的 37 项。状态依次为：待做、进行中、已部署验证、受阻。每完成一项，记录提交、针对性测试和服务器验证；文档不保存凭据或真实用户消息。

## 基线与部署规则

- 本地与服务器 main 均为 81afa12；服务器工作区干净，djhx-pi-bot active，/healthz 为 ok=true、degraded=false、失败更新 0。
- 本地基线：113 项 unittest 通过。
- 工作流：本地针对性测试、完整测试、uv lock、python -m py_compile *.py → commit → push gitea main → 等待服务器 origin/main 到达提交 → 核对服务与 /healthz → 在服务器运行针对性测试并验证真实服务行为。
- 数据修改使用可清理的测试条目；邮件仅发往明确授权的测试地址。部署、验证结果逐项登记。失败先修复，不把未验证项标记完成。

## 实施清单（按建议顺序）

| ID | 功能及验收 | 状态 | 提交、线上验证 |
|---|---|---|---|
| UX-01 | 忙碌时明确选择插入当前任务或排队下一轮；回执标识目标 | 已部署验证 | 1d8a373、10df4b9：连续消息为两个完整回合；运行中 /steer 写入当前回合 steer_text，无新回合；健康正常 |
| UX-02 | 启动或切换期间新消息可靠排队，并可取消 | 已部署验证 | 1d8a373：服务器 10 项针对性测试通过；真实 webhook 连续消息后 /queue clear，第二条无会话回合、更新均 done、健康正常 |
| UX-03 | 未支持的 Telegram 消息类型得到明确反馈 | 已部署验证 | a45b2c3：语音、贴纸真实 webhook 返回 200 且更新 done；服务器针对性 2 项测试通过；健康正常 |
| UX-04 | 普通文件与图片分流；支持受限文本、代码和日志附件 | 已部署验证 | e058064：UTF-8 附件在线下载并形成完整非图片回合；PDF 被拒绝且无回合；健康正常 |
| UX-05 | 相册按组处理，明确数量与限制 | 已部署验证 | 05ef9a6：双图片同组 webhook 均 done，仅一个 complete 图片回合；5 张超限均 done、无回合；健康正常 |
| UX-06 | 用户模型选择持久化，重启后保持 | 已部署验证 | b271f02、918379b：/model use 更新 done；重启后 SQLite 记录与新 AgentSession 模型一致；健康正常 |
| UX-07 | /status 显示实际模型、任务耗时和队列 | 已部署验证 | 6edfe7e：/status 真实 webhook 更新 done；服务器 2 项格式测试通过；健康正常 |
| UX-08 | 聊天工具进度不泄露命令和敏感参数 | 已部署验证 | 9513f17：服务器 2 项隐私测试通过；真实工具请求回合 complete；健康正常 |
| UX-09 | 邮件发送前预览收件人、主题、正文并确认 | 进行中 | b88cfd5：服务器 4 项测试通过；在线草稿创建、/mail 预览与取消均成功；等待授权测试邮箱后验证真实 SMTP 确认 |
| UX-10 | 失败更新与用户、会话关联，安全告知结果 | 已部署验证 | 9cc5159：服务器 19 项针对性测试通过；线上故障注入记录用户与会话、通知发送成功；测试失败记录已确认处理，健康正常 |
| UX-11 | /stop 可取消待启动任务并显示状态 | 已部署验证 | b6786b8：服务器 6 项队列测试通过；真实 webhook 排队消息被 /stop 取消、无会话回合，健康正常 |
| UX-12 | /help 首屏精简，分类查看命令 | 已部署验证 | 04bd425：服务器 2 项测试通过；真实 /help 与 /help memory 更新 done，健康正常 |
| UX-13 | 单环境时避免 /env use 的误导性“已切换” | 已部署验证 | 4649ca7：服务器 3 项帮助/环境测试通过；真实 /env use server 更新 done，健康正常 |
| UX-14 | 模型列表显示当前项、提供商、能力与估算价格；选择无歧义 | 已部署验证 | 79a115f：服务器 5 项测试通过；真实 /model list 与完整 ID 切换均 done，模型偏好与健康正常 |
| UX-15 | 图片模型不兼容时便捷切换并提示重发 | 已部署验证 | a239310：服务器 28 项图片测试通过；真实非图片模型+图片更新 done、无会话回合；已恢复原模型，健康正常 |
| UX-16 | Telegram 会话列表分页、搜索、重命名 | 已部署验证 | 18135a3：服务器 2 项导航测试通过；真实重命名、搜索、分页更新 done；临时会话已清理，健康正常 |
| UX-17 | 默认会话自动命名过滤敏感内容 | 已部署验证 | baa7c5a：服务器 3 项测试及临时库功能验证通过；敏感文本隐藏、普通标题保留，健康正常 |
| UX-18 | Telegram 人格列表可点击切换 | 已部署验证 | dc7e8cf：服务器 9 项人格测试通过；真实 /persona list 更新 done，回调路由测试通过，健康正常 |
| UX-19 | 人格切换说明历史影响，并支持同时新建会话 | 已部署验证 | 809dcfe：服务器 10 项人格测试通过；真实新会话创建、原会话恢复及测试会话清理成功，健康正常 |
| UX-20 | Telegram 删除记忆、清空摘要二次确认 | 已部署验证 | 3c2310e：服务器 15 项记忆测试通过；真实预览不删除、确认后删除/清空，临时数据已清理，健康正常 |
| UX-21 | 每用户控制费用自动报告，支持紧凑展示 | 已部署验证 | 6dd5734：服务器 8 项费用测试通过；真实命令与测试回合 done、用量完整；临时偏好恢复，健康正常 |
| UX-22 | 长时间无事件时显示阶段和耗时 | 已部署验证 | 866adec：服务器 11 项流式测试通过；真实 Telegram 消息显示等待阶段与耗时，完成后替换为最终回复；健康正常 |
| UX-23 | 工具成功但无文字回复时显示明确完成状态 | 已部署验证 | b26ec8c：服务器 34 项针对性测试通过；真实 Telegram 空文本工具完成消息发送成功；健康正常 |
| UX-24 | 长回复、链接和代码格式可靠发送 | 已部署验证 | 9d0200b：服务器 17 项流式测试通过；真实长回复分 3 段发送，链接和代码实体均经 Telegram 确认；健康正常 |
| UX-25 | Telegram 记忆列表翻页按钮、来源和更新时间 | 已部署验证 | 46f4ef9：服务器分页回调测试通过；真实 /memory list 和第 2 页 webhook 均 done；健康正常 |
| UX-26 | 可查询一轮实际注入的记忆 | 已部署验证 | 1ae7754：服务器 19 项记忆测试通过；真实回合完成并记录 4 条注入记忆；/memory used 与 list 均 done；健康正常 |
| UX-27 | 改善中文记忆检索并显示命中依据 | 已部署验证 | c8a0d9e：服务器 21 项记忆测试通过；真实检索与回合均 done，回合 6 条注入记忆均有命中依据；/memory used done；健康正常 |
| UX-28 | 用户可暂停和恢复自动记忆 | 已部署验证 | 06e3971：服务器 23 项记忆测试通过；真实 off/on/default/status 均 done，偏好恢复原值；健康正常 |
| UX-29 | 展示摘要待处理轮次、上次成功和失败状态 | 已部署验证 | 12ee35a：服务器成功/失败状态测试通过；真实 /summary status done；健康正常 |
| UX-30 | 会话恢复前预览可恢复范围，区分空白恢复 | 已部署验证 | 2eeb5d0：服务器 3 项恢复测试通过；真实部分/空白预览不修改会话，确认空白恢复后新文件绑定成功；原会话已恢复，健康正常 |
| UX-31 | 费用页按会话、日期和模型筛选，单列未计价调用 | 已部署验证 | 5a42129：服务器筛选测试通过；真实后台 HTTP 验证会话/日期/模型筛选、未计价原因及无效会话拒绝；临时数据已清理，健康正常 |
| UX-32 | 后台概览显示刷新时间，支持刷新 | 已部署验证 | 2d2dd7f：服务器刷新测试通过；真实后台两次请求时间戳变化，用户选择保留；健康正常 |
| UX-33 | 失败更新显示安全元数据和副作用风险 | 待做 | — |
| UX-34 | 后台长列表筛选与分页 | 待做 | — |
| UX-35 | 记忆设置可恢复 .env 默认值 | 待做 | — |
| UX-36 | 耗时表单显示提交中、防重复提交、保留错误输入 | 待做 | — |
| UX-37 | 手机端导航提示可横向滚动或提供折叠菜单 | 待做 | — |

## 交付记录

| 北京时间 | 功能 | 本地验证 | 服务器提交与在线验证 | 结果及下一步 |
|---|---|---|---|---|
| 2026-09-28 | 基线 | 113 项测试通过 | 81afa12，服务 active，/healthz 正常 | 开始 UX-01 |
| 2026-09-28 | UX-01、UX-02 首次部署 | 117 项测试通过；uv lock、py_compile、diff --check 通过 | 1d8a373；服务 active、/healthz 正常；服务器 10 项针对性测试通过；真实 webhook 的独立回合与取消均成功 | UX-02 完成；UX-01 补测启动期 /steer |
| 2026-09-28 | UX-01 完成 | 118 项测试通过；uv lock、py_compile、diff --check 通过 | 10df4b9；服务器 4 项针对性测试通过；真实 webhook 在运行中 /steer，更新均 done，仅原消息形成完整回合且 steer_text 有值；/healthz 正常 | UX-01 完成，下一项 UX-03 |
| 2026-09-28 | UX-03 完成 | 120 项测试通过；uv lock、py_compile、diff --check 通过 | a45b2c3；服务 active、/healthz 正常；服务器 2 项针对性测试通过；语音与贴纸 webhook 更新均 done | 未支持类型有明确回复；下一项 UX-04 |
| 2026-09-28 | UX-04 完成 | 124 项测试通过；uv lock、py_compile、diff --check 通过 | e058064；服务 active、/healthz 正常；服务器 26 项针对性测试通过；真实 Telegram 测试附件经 webhook 下载，回合 complete、has_image=0 且包含附件内容；PDF webhook done、无回合 | 文本附件可用；下一项 UX-05 |
| 2026-09-28 | UX-05 完成 | 126 项测试通过；uv lock、py_compile、diff --check 通过 | 05ef9a6；服务 active、/healthz 正常；服务器 27 项图片测试通过；两张真实图片文件同组 webhook 均 done，仅一个 complete 图片回合；5 张超限均 done、无回合 | 相册聚合与限制可用；下一项 UX-06 |
| 2026-09-28 | UX-06 部署中 | 129 项测试通过；uv lock、py_compile、diff --check 通过 | b271f02；服务器 3 项针对性测试通过；真实 /model use webhook done，用户模型记录为 deepseek/deepseek-flash | 待部署重启后检查记录与启动模型 |
| 2026-09-28 | UX-06 完成 | 无新代码 | 918379b 触发服务重启；/healthz 正常，数据库保存的模型与重建 AgentSession 的 provider/model 相同 | 选择持久化生效；下一项 UX-07 |
| 2026-09-28 | UX-07 完成 | 131 项测试通过；uv lock、py_compile、diff --check 通过 | 6edfe7e；服务 active、/healthz 正常；服务器 2 项状态格式测试通过；真实 /status webhook 更新 done | 状态页增加实时模型、任务耗时、队列；下一项 UX-08 |
| 2026-09-29 | UX-08 完成 | 133 项测试通过；uv lock、py_compile、diff --check 通过 | 9513f17；服务 active、/healthz 正常；服务器 2 项隐私测试通过；真实工具请求 webhook done、回合 complete | 聊天活动只展示固定类别；下一项 UX-09 |
| 2026-09-29 | UX-09 阶段验证 | 138 项测试通过；uv lock、py_compile、diff --check 通过 | b88cfd5；服务 active、/healthz 正常；服务器 4 项邮件确认测试通过；mail_cli 草稿创建成功，/mail 预览更新 done 且 previewed_at 有值，取消更新 done 且 state=cancelled | 真实 SMTP 发送仅在收到授权测试邮箱后验证；并行继续 UX-10 |

| 2026-09-29 | UX-10 完成 | 142 项测试通过；uv lock、py_compile、diff --check 通过 | 9cc5159；服务器 19 项针对性测试通过；伪造过期回调触发真实失败路径，关联用户与会话，通知最终送达；测试失败记录已确认处理，/healthz 正常 | 下一项 UX-11；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-11 完成 | 144 项测试通过；uv lock、py_compile、diff --check 通过 | b6786b8；服务器 6 项队列测试通过；真实 webhook 第一条启动、第二条排队后 /stop，3 条更新 done，第二条无会话回合，/healthz 正常 | 下一项 UX-12；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-12 完成 | 146 项测试通过；uv lock、py_compile、diff --check 通过 | 04bd425；服务器 2 项帮助测试通过；真实 /help 与 /help memory 更新 done，/healthz 正常 | 下一项 UX-13；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-13 完成 | 147 项测试通过（首次完整测试出现偶发失败，重跑通过）；uv lock、py_compile、diff --check 通过 | 4649ca7；服务器 3 项帮助/环境测试通过；真实 /env use server 更新 done，/healthz 正常 | 下一项 UX-14；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-14 完成 | 149 项测试通过；uv lock、py_compile、diff --check 通过 | 79a115f；服务器 5 项模型测试通过；RPC 元数据确有价格与能力；真实 /model list、/model use deepseek/deepseek-flash 更新 done，用户偏好保持，/healthz 正常 | 下一项 UX-15；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-15 完成 | 149 项测试通过；uv lock、py_compile、diff --check 通过 | a239310；服务器 28 项图片测试通过；先切到 deepseek-v4-pro，再投递真实 Telegram 图片，更新 done、无会话回合；切回 deepseek-flash，/healthz 正常 | 下一项 UX-16；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-16 完成 | 151 项测试通过；uv lock、py_compile、diff --check 通过 | 18135a3；服务器 2 项会话导航测试通过；临时会话经真实 webhook 重命名、搜索、列表分页均 done，名称保存正确；测试会话删除，/healthz 正常 | 下一项 UX-17；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-17 完成 | 152 项测试通过；uv lock、py_compile、diff --check 通过 | baa7c5a；服务器 3 项会话测试通过；服务器临时库验证凭据文本自动命名为中性标题、普通标题保留；/healthz 正常 | 下一项 UX-18；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-18 完成 | 153 项测试通过；uv lock、py_compile、diff --check 通过 | dc7e8cf；服务器 9 项人格测试通过（含按钮回调切换）；真实 /persona list 更新 done，/healthz 正常 | 下一项 UX-19；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-19 完成 | 154 项测试通过；uv lock、py_compile、diff --check 通过 | 809dcfe；服务器 10 项人格测试通过；真实 /persona use <当前人格> new 创建独立会话，原人格保持；/session use 恢复原会话，测试会话清理，/healthz 正常 | 下一项 UX-20；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-20 完成 | 156 项测试通过；uv lock、py_compile、diff --check 通过 | 3c2310e；服务器 15 项记忆测试通过；真实 /memory delete 与 /summary clear 均先预览不改动、确认后执行；临时记忆/会话清理、原会话与上下文恢复，/healthz 正常 | 下一项 UX-21；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-21 完成 | 158 项测试通过；uv lock、py_compile、diff --check 通过 | 6dd5734；服务器 8 项费用测试通过；真实 /cost auto compact、/cost auto、/cost last compact 均 done；测试回合 done、完整用量记录和 1 条计价事件；未见摘要发送失败，临时偏好恢复为空，/healthz 正常 | 下一项 UX-22；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-22 完成 | 160 项测试通过；uv lock、py_compile、diff --check 通过 | 866adec；服务器 11 项流式测试通过；真实 Telegram 消息先显示等待阶段与耗时，随后正确替换为最终回复；/healthz 正常 | 下一项 UX-23；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-23 完成 | 164 项测试通过；uv lock、py_compile、diff --check、20 次空闲边界重复测试通过 | b26ec8c；服务器 34 项针对性测试通过；真实 Telegram 成功工具空文本回合明确显示完成；/healthz 正常 | 下一项 UX-24；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-24 完成 | 167 项测试通过；uv lock、py_compile、diff --check 通过 | 9d0200b；服务器 17 项流式测试通过；真实长回复分 3 段成功发送，短样本返回 text_link 与 pre 实体；/healthz 正常 | 下一项 UX-25；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-25 完成 | 168 项测试通过；uv lock、py_compile、diff --check 通过 | 46f4ef9；服务器分页按钮、来源与更新时间测试通过；真实 /memory list 与 /memory list 2 更新均 done；/healthz 正常 | 下一项 UX-26；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-26 完成 | 171 项测试通过；uv lock、py_compile、diff --check 通过 | 1ae7754；服务器 19 项记忆测试通过；真实回合 complete 且记录 4 条完整注入记忆；/memory used 与 list 更新均 done；/healthz 正常 | 下一项 UX-27；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-27 完成 | 173 项测试通过；uv lock、py_compile、diff --check 通过 | c8a0d9e；服务器 21 项记忆测试通过；真实 /memory search 中文、测试回合和 /memory used 均 done；回合 complete、6 条注入记忆均有命中依据；/healthz 正常 | 下一项 UX-28；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-28 完成 | 175 项测试通过；uv lock、py_compile、diff --check 通过 | 06e3971；服务器 23 项记忆测试通过；真实 /memory auto off/on/default/status 更新均 done，数据库偏好随命令变化并恢复原值；/healthz 正常 | 下一项 UX-29；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-29 完成 | 177 项测试通过；uv lock、py_compile、diff --check 通过 | 12ee35a；服务器 2 项状态测试通过；真实 /summary status 更新 done；/healthz 正常 | 下一项 UX-30；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-30 完成 | 179 项测试通过；uv lock、py_compile、diff --check 通过；覆盖空白/部分恢复、过期确认、后台表单 | 2eeb5d0；服务器 3 项针对性测试通过；真实 webhook 的部分/空白预览均 done 且不修改会话，确认空白恢复后绑定新文件，切回原会话并清理临时记录；/healthz 正常 | 下一项 UX-31；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-31 完成 | 180 项测试通过；uv lock、py_compile、diff --check 通过；覆盖会话、日期、模型筛选和未计价列表 | 5a42129；服务器针对性测试通过；真实后台登录后，临时用量验证会话/日期排除、模型筛选、未计价原因及无效会话拒绝；数据已清理；/healthz 正常 | 下一项 UX-32；UX-09 真实 SMTP 仍等待授权测试邮箱 |

| 2026-09-29 | UX-32 完成 | 181 项测试通过；uv lock、py_compile、diff --check 通过；概览刷新表单保留用户选择，显示北京时间读取时刻 | 2d2dd7f；服务器针对性测试通过；真实后台登录后两次请求，读取时间戳变化且保留用户选择；/healthz 正常 | 下一项 UX-33；UX-09 真实 SMTP 仍等待授权测试邮箱 |

## 恢复工作时先做

1. 读取本文件、AGENTS.md 和 git status --short --branch，确认上次交付点。
2. 找到首个未完成项；不要重复执行已验证的外部副作用测试。
3. 核对服务器 git status、git log -1、systemctl is-active djhx-pi-bot 和 /healthz，再继续。
