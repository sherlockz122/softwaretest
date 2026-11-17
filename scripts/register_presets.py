"""Register versioned preset baselines into the model pool (idempotent).

预置基线用内置 apachejit 资产按默认配置训练（LR/RF 各一份），按「名称 + 代码
版本」幂等注册：同一版本重复执行不重训，版本升级后按新版本名注册新条目、
旧条目保留但不再通过新请求的契约校验。预置条目没有所有者，只能由管理员移除。
"""

import argparse
from pathlib import Path

from packages.mining import research_dataset as research
from packages.modeling import presets
from packages.platform.config import load_settings
from packages.platform.connections import Connections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=research.STORE)
    args = parser.parse_args()
    settings = load_settings()
    connections = Connections(settings)
    try:
        registered = presets.ensure_presets(connections.engine, store=args.store)
        if registered:
            print("registered:", ", ".join(registered))
        else:
            print("nothing new: all presets for this code version already registered")
    finally:
        connections.close()


if __name__ == "__main__":
    main()
