"""Retired provision entry: refuse account changes and point to explicit bootstrap."""


def main() -> int:
    print(
        "LEGACY_PROVISION_UNSUPPORTED: use python -m packages.auth.bootstrap "
        "with explicit DG_BOOTSTRAP_USERNAME and DG_BOOTSTRAP_PASSWORD; "
        "existing account credentials are preserved."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
