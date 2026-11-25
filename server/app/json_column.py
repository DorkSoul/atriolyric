"""JSON values kept in plain text columns.

The ``*_json`` columns hold JSON text. ``JSONText`` encodes with
``json.dumps`` and decodes with ``json.loads`` in one place: the column is
TEXT (or VARCHAR, with ``varchar=True``), and each column passes its own
``ensure_ascii``/``sort_keys``/``separators``, so the text written is stable.
That matters where stored text is compared, e.g.
``PlaySourceHistory.payload_json`` is looked up by equality with a freshly
encoded payload, so stored rows keep matching.

Rules:

- Writing: ``None`` stores NULL; anything else is encoded. A ``str`` is
  refused (TypeError): every JSONText column holds a list or an object, so
  a string means a call site encodes by hand and would store the JSON
  twice.
- Reading: NULL, an empty string, text that is not JSON and a JSON
  ``null`` all read as the column's empty value - ``None``, or a fresh
  ``[]``/``{}`` from ``empty=list``/``empty=dict`` - instead of raising.
  Anything else is the parsed value; call sites still check its shape.
- Changes are made by assignment, never in place: the type is not a
  mutable one (no ``MutableList``/``MutableDict``), so SQLAlchemy sees a new
  value only when the attribute is assigned. A list or dict read from a
  column is treated as read-only; to change it, build a new one and assign
  it. Assigning a value equal to the loaded one writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class JSONText(TypeDecorator[Any]):
    impl = Text
    cache_ok = True

    def __init__(
        self,
        *,
        empty: Callable[[], Any] | None = None,
        ensure_ascii: bool = True,
        sort_keys: bool = False,
        separators: tuple[str, str] | None = None,
        varchar: bool = False,
    ) -> None:
        super().__init__()
        # The column's declared SQL type (both have TEXT affinity in
        # SQLite; it only changes what CREATE TABLE says).
        if varchar:
            self.impl = String()
        self.empty = empty
        self.ensure_ascii = ensure_ascii
        self.sort_keys = sort_keys
        self.separators = separators
        self.varchar = varchar

    def _empty_value(self) -> Any:
        return self.empty() if self.empty is not None else None

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            raise TypeError(
                "JSONText columns take the value itself, not JSON text "
                f"(got {type(value).__name__})"
            )
        return json.dumps(
            value,
            ensure_ascii=self.ensure_ascii,
            sort_keys=self.sort_keys,
            separators=self.separators,
        )

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if not value:
            return self._empty_value()
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return self._empty_value()
        return self._empty_value() if parsed is None else parsed

    def copy(self, **kw: Any) -> JSONText:
        return JSONText(
            empty=self.empty,
            ensure_ascii=self.ensure_ascii,
            sort_keys=self.sort_keys,
            separators=self.separators,
            varchar=self.varchar,
        )
