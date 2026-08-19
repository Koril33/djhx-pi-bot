# AGENTS.md

本文件为 AI 编码代理（如 pi）提供项目约定，随项目演进持续更新。

## 当前状态

项目初始化中，尚未安装依赖。

## 技术栈（规划）

- Python >= 3.14，包管理使用 uv
- Telegram Bot：python-telegram-bot（参考 djhx-bot 的既有选型）
- 与 pi 交互：待定（print 模式一次性调用 / RPC 模式常驻 / SDK）

## 约定（待补充）

- 配置走环境变量 + `.env`（见 `.env.example`）
- 运行时数据不提交 git（见 .gitignore）

<!-- 后续迭代补充：目录结构、代码风格、测试要求、部署方式 -->
