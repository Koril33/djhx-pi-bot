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

    # 日志
    log_level: str = "INFO"
    log_dir: str = "logs"

    # 流式回复节流（秒）
    stream_edit_interval: float = 1.0

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_user_ids.split(",") if x.strip().isdigit()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
