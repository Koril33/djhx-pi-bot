"""配置加载：环境变量 + .env，不硬编码任何密钥。"""
from functools import lru_cache
import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str = ""
    # 允许使用 bot 的 Telegram user id，逗号分隔；空 = 拒绝所有
    allowed_user_ids: str = ""

    # Webhook 路径密钥（随机字符串，防他人调 webhook）
    webhook_secret: str = ""
    webhook_url: str = ""  # 形如 https://pi.djhx.site/webhook/<secret>

    # Gitea 部署 webhook 密钥（Push 事件触发自动部署）
    deploy_secret: str = ""

    # pi
    pi_cmd: str = "pi"                     # pi 可执行文件路径
    pi_provider: str = "deepseek"          # 默认 provider（透传给 pi）
    pi_model: str = "deepseek-flash"       # 默认模型（DeepSeek V4.1 Flash）
    pi_session_dir: str = "data/pi-sessions"  # pi 会话文件目录

    # 存储
    db_path: str = "data/bot.db"
    pricing_file: str = "pricing.json"  # 费率、峰谷时段与节假日，按官方变动更新
    cost_tracking_enabled: bool = True
    cost_auto_report: bool = True

    # 单管理员后台；留空时后台登录关闭，密码仅存于 .env
    admin_username: str = ""
    admin_password: str = ""

    # SQLite 长期记忆与会话摘要；全部预算可通过环境变量调整
    memory_enabled: bool = True
    memory_auto_extract: bool = True
    memory_batch_turns: int = 5
    memory_summary_retry_seconds: int = 3600
    memory_turn_retention: int = 20
    memory_turn_user_chars: int = 4000
    memory_turn_assistant_chars: int = 8000
    memory_summary_input_max_bytes: int = 16000
    memory_summary_response_max_bytes: int = 8192
    memory_active_limit: int = 100
    memory_item_max_chars: int = 240
    memory_inject_max_items: int = 6
    memory_inject_max_bytes: int = 4096
    memory_summary_max_bytes: int = 1536
    memory_items_max_bytes: int = 2048
    memory_context_dir: str = "data/memory-context"

    # 服务
    host: str = "127.0.0.1"
    port: int = 8128
    webhook_queue_capacity: int = 1000
    webhook_worker_concurrency: int = 8
    webhook_steer_concurrency: int = 4
    webhook_max_bytes: int = 2 * 1024 * 1024
    shutdown_drain_seconds: float = 30.0
    telegram_retry_initial_seconds: float = 3.0
    telegram_retry_max_seconds: float = 60.0
    telegram_probe_interval_seconds: float = 60.0
    update_worker_retry_seconds: float = 3.0
    backup_enabled: bool = True
    backup_root: str = "data/backups"
    backup_interval_seconds: float = 86400.0
    backup_idle_timeout_seconds: float = 120.0
    voice_enabled: bool = True
    voice_model_dir: str = "data/voice-model"
    voice_max_seconds: int = 60
    voice_max_bytes: int = 10 * 1024 * 1024
    voice_transcribe_timeout_seconds: float = 120.0
    pdf_max_bytes: int = 5 * 1024 * 1024
    pdf_max_pages: int = 30
    pdf_max_chars: int = 30000
    pdf_parse_timeout_seconds: float = 20.0
    deploy_require_signature: bool = False  # 旧 Gitea hook 仅配置 URL 密钥时保持兼容
    deploy_repo_full_name: str = ""         # 可设为 koril/djhx-pi-bot，校验 payload 来源仓库

    # 日志
    log_level: str = "INFO"
    log_dir: str = "logs"

    # 流式回复：首批文本先短暂聚合，后续按固定频率编辑 Telegram 消息
    stream_edit_interval: float = 0.6
    stream_initial_delay: float = 0.25
    stream_initial_chars: int = 16

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_user_ids.split(",") if x.strip().isdigit()}

    def validate_runtime(self) -> None:
        """启动时尽早暴露会令所有请求失效的配置错误。"""
        missing = [
            name for name, value in (
                ("BOT_TOKEN", self.bot_token),
                ("WEBHOOK_SECRET", self.webhook_secret),
            ) if not value
        ]
        if missing:
            raise ValueError(f"缺少必要配置: {', '.join(missing)}")
        ids = [part.strip() for part in self.allowed_user_ids.split(",") if part.strip()]
        if not ids or any(not part.isdigit() for part in ids):
            raise ValueError("ALLOWED_USER_IDS 必须是非空、逗号分隔的 Telegram 用户 ID")
        if (
            self.webhook_queue_capacity < 1
            or self.webhook_worker_concurrency < 1
            or self.webhook_steer_concurrency < 1
            or self.webhook_max_bytes < 1024
        ):
            raise ValueError("Webhook 队列容量或请求大小限制无效")
        if self.shutdown_drain_seconds <= 0:
            raise ValueError("SHUTDOWN_DRAIN_SECONDS 必须大于 0")
        if any(value <= 0 for value in (
            self.telegram_retry_initial_seconds, self.telegram_retry_max_seconds,
            self.telegram_probe_interval_seconds, self.update_worker_retry_seconds,
            self.backup_interval_seconds, self.backup_idle_timeout_seconds,
            self.voice_max_seconds, self.voice_max_bytes, self.voice_transcribe_timeout_seconds,
            self.pdf_max_bytes, self.pdf_max_pages, self.pdf_max_chars, self.pdf_parse_timeout_seconds,
        )):
            raise ValueError("重试、探活和备份间隔必须大于 0")
        if self.telegram_retry_max_seconds < self.telegram_retry_initial_seconds:
            raise ValueError("TELEGRAM_RETRY_MAX_SECONDS 不能小于初始重试间隔")
        if bool(self.admin_username) != bool(self.admin_password):
            raise ValueError("ADMIN_USERNAME 与 ADMIN_PASSWORD 必须同时配置")
        if self.admin_username:
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", self.admin_username):
                raise ValueError("ADMIN_USERNAME 必须是 3-32 位字母、数字、下划线或连字符")
            if not 12 <= len(self.admin_password) <= 128:
                raise ValueError("ADMIN_PASSWORD 必须是 12-128 个字符")
        if self.memory_enabled:
            limits = (
                self.memory_batch_turns, self.memory_turn_retention,
                self.memory_summary_retry_seconds,
                self.memory_turn_user_chars, self.memory_turn_assistant_chars,
                self.memory_summary_input_max_bytes, self.memory_active_limit,
                self.memory_summary_response_max_bytes,
                self.memory_item_max_chars, self.memory_inject_max_items,
                self.memory_inject_max_bytes, self.memory_summary_max_bytes,
                self.memory_items_max_bytes,
            )
            if any(value < 1 for value in limits):
                raise ValueError("MEMORY_* 限额必须大于 0")
            if self.memory_summary_max_bytes + self.memory_items_max_bytes > self.memory_inject_max_bytes:
                raise ValueError("摘要和记忆注入预算之和不能超过 MEMORY_INJECT_MAX_BYTES")


@lru_cache
def get_settings() -> Settings:
    return Settings()
