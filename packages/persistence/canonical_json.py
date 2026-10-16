"""Lossless JSON text for binary floating-point research values."""

import json

from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.types import TypeDecorator


class CanonicalJSON(TypeDecorator):
    impl = MEDIUMTEXT
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(
            value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("DATA_NUMBER_INVALID"))
        )
