"""显式预置账号（bootstrap-application 2.1）：按部署配置 upsert 预置用户。

用法：python -m packages.persistence.seed
幂等：同用户名重复执行不产生重复记录；配置变更时同步密码哈希、角色与启用状态。
"""

from sqlalchemy import select

from packages.domain.models import User
from packages.persistence.db import create_session_factory, transaction
from packages.platform.config import load_settings
from packages.security.passwords import hash_password


def provision_user(settings) -> User:
    session_factory = create_session_factory(settings)
    with session_factory() as session:
        with transaction(session):
            existing = session.execute(
                select(User).where(User.username == settings.provision_username)
            ).scalar_one_or_none()
            if existing is None:
                user = User(
                    username=settings.provision_username,
                    password_hash=hash_password(settings.provision_password.get_secret_value()),
                    role=settings.provision_role,
                    is_active=True,
                )
                session.add(user)
                return user
            existing.password_hash = hash_password(settings.provision_password.get_secret_value())
            existing.role = settings.provision_role
            existing.is_active = True
            return existing


def main() -> int:
    settings = load_settings()
    user = provision_user(settings)
    print(f"provisioned user {user.username!r} role={user.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
