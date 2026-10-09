"""部署复制到 data/deploy-state；回滚后阻止旧版启动补偿再次部署失败目标。"""
import sys
from pathlib import Path

from dotenv import dotenv_values


def install_legacy_smtp_reader(storage_class, root: Path) -> None:
    """旧版本回滚也读取 .env 引用，数据库中的密码不需要重新转回明文。"""
    original_get = storage_class.get_smtp_settings

    def get_smtp_settings(self):
        values = original_get(self)
        if values is None or values["password"] != "env:SMTP_PASSWORD":
            return values
        values = dict(values)
        values["password"] = dotenv_values(root / ".env", interpolate=False).get("SMTP_PASSWORD") or ""
        return values

    storage_class.get_smtp_settings = get_smtp_settings


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    import app
    import uvicorn

    install_legacy_smtp_reader(app.Storage, root)
    if (root / "data/deploy-state/failed-target").exists():
        original_spawn = app._spawn_deploy

        def spawn_deploy(*, catch_up=False):
            if catch_up:
                return None
            return original_spawn(catch_up=catch_up)

        app._spawn_deploy = spawn_deploy
    uvicorn.run(app.app, host=app.settings.host, port=app.settings.port, log_config=None)


if __name__ == "__main__":
    main()
