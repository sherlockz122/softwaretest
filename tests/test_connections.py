import pytest
from sqlalchemy import text

from packages.platform.config import load_settings
from packages.platform.connections import Connections


@pytest.mark.integration
def test_real_services_and_volume_marker():
    settings = load_settings()
    connections = Connections(settings)
    try:
        assert connections.ready()
        with connections.engine.connect() as connection:
            assert connection.execute(text("SELECT DATABASE()")).scalar() == settings.mysql_database
            # Dedicated platform probe, not application migration or business data.
            connection.execute(
                text("CREATE TABLE IF NOT EXISTS platform_probe (id INT PRIMARY KEY)")
            )
            connection.execute(text("INSERT IGNORE INTO platform_probe (id) VALUES (1)"))
            connection.commit()
            assert connection.execute(text("SELECT COUNT(*) FROM platform_probe")).scalar() == 1
        connections.redis.set("defectguard:platform-probe", "stage2", ex=86400)
        assert connections.redis.get("defectguard:platform-probe") == b"stage2"
    finally:
        connections.close()
