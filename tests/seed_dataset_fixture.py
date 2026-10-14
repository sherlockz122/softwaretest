"""Seed only a disposable fresh environment; never normal public assets."""

import argparse

from packages.mining import public_assets as assets
from tests.dataset_support import fixture_asset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    args = parser.parse_args()
    store = assets.safe_path(args.store)
    if not store.is_relative_to(assets.ROOT / "runtime/fresh"):
        raise RuntimeError("Engineering seed must remain in the disposable environment")
    directory, _ = fixture_asset(store.parent / "dataset-fixture", store=store)
    print("Seeded inert 40-row research fixture", directory.name)


if __name__ == "__main__":
    main()
