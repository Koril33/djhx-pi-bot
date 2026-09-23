"""配置加载：环境变量 + .env，不硬编码任何密钥。"""
from functools import lru_cache

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
    pi_model: str = "deepseek-v4-flash"    # 默认模型
    pi_session_dir: str = "data/pi-sessions"  # pi 会话文件目录

    # 存储
    db_path: str = "data/bot.db"

    # 服务
    host: str = "127.0.0.1"
    port: int = 8128
    webhook_queue_capacity: int = 1000
    webhook_worker_concurrency: int = 8
    webhook_steer_concurrency: int = 4
    webhook_max_bytes: int = 2 * 1024 * 1024
    shutdown_drain_seconds: float = 30.0
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
