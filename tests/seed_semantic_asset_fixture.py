"""Seed synthetic structural data in a disposable acceptance deployment only."""

import argparse
from tempfile import TemporaryDirectory

from packages.semantic import asset_catalog as catalog
from packages.semantic.asset_reader import safe
from tests.semantic_asset_support import source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    args = parser.parse_args()
    store = safe(args.store)
    store.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=store.parent, prefix=".semantic-fixture-") as directory:
        original, m = source(directory)
        installed = catalog.install(original, m["content_hash"], store=store)
        catalog.verify(installed)
    print("Synthetic semantic asset installed for isolated UI acceptance: " + installed.name)


if __name__ == "__main__":
    main()
