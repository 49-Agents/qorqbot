"""Closed value types for exported columns and structured-field projections.

Every exported value is checked against one of these types. They are deliberately small and closed: a scalar is a
plain ``str``/``int``/``float``/``bool`` (or ``None`` when nullable), an ``Obj`` has exactly its declared properties
and an ``Arr`` is a bounded list of one declared item type. Bytes, unknown containers and undeclared properties are
never accepted, so an opaque payload cannot be exported merely because it happens to be a string or JSON column.
``check`` returns the normalized value and raises ``FieldValueError`` naming only the path and the Python type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
from types import MappingProxyType
from typing import Any

from .errors import HistoryCompositionError

NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAXIMUM_DEPTH = 4
MAXIMUM_ITEMS = 10_000


class FieldValueError(ValueError):
    """A value does not satisfy its declared type. ``path`` names the field; the message never contains the value."""

    def __init__(self, path: str, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path, self.reason = path, reason


def _fail(path: str, reason: str, value: Any = None) -> FieldValueError:
    return FieldValueError(path, f"{reason} ({type(value).__name__})" if value is not None else reason)


@dataclass(frozen=True, kw_only=True)
class FieldType:
    """A declared closed value type. ``nullable`` allows ``None``."""

    nullable: bool = False

    kind = "field"

    def check(self, value: Any, path: str) -> Any:
        if value is None:
            if self.nullable:
                return None
            raise FieldValueError(path, "null is not allowed")
        return self._check(value, path)

    def _check(self, value: Any, path: str) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"type": self.kind, "nullable": self.nullable, **self._describe()}

    def _describe(self) -> dict[str, Any]:
        return {}

    def _validate_declaration(self, depth: int) -> None:
        if type(self.nullable) is not bool:
            raise HistoryCompositionError("nullable must be a boolean")


@dataclass(frozen=True, kw_only=True)
class Text(FieldType):
    choices: tuple[str, ...] = ()
    max_length: int | None = None

    kind = "text"

    def __post_init__(self):
        object.__setattr__(self, "choices", tuple(self.choices))
        self._validate_declaration(0)
        if any(type(choice) is not str for choice in self.choices):
            raise HistoryCompositionError("Text choices must be strings")
        if self.max_length is not None and (type(self.max_length) is not int or self.max_length < 1):
            raise HistoryCompositionError("Text max_length must be a positive integer")

    def _check(self, value, path):
        if type(value) is not str:
            raise _fail(path, "expected text", value)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise FieldValueError(path, "text is not valid Unicode") from None
        if self.choices and value not in self.choices:
            raise FieldValueError(path, "text is not one of the declared choices")
        if self.max_length is not None and len(value) > self.max_length:
            raise FieldValueError(path, "text exceeds its declared length")
        return value

    def _describe(self):
        return {key: value for key, value in (("choices", list(self.choices) or None), ("max_length", self.max_length))
                if value is not None}


@dataclass(frozen=True, kw_only=True)
class Integer(FieldType):
    choices: tuple[int, ...] = ()

    kind = "integer"

    def __post_init__(self):
        object.__setattr__(self, "choices", tuple(self.choices))
        self._validate_declaration(0)
        if any(type(choice) is not int for choice in self.choices):
            raise HistoryCompositionError("Integer choices must be integers")

    def _check(self, value, path):
        if type(value) is not int:
            raise _fail(path, "expected integer", value)
        if self.choices and value not in self.choices:
            raise FieldValueError(path, "integer is not one of the declared choices")
        return value

    def _describe(self):
        return {"choices": list(self.choices)} if self.choices else {}


@dataclass(frozen=True, kw_only=True)
class Real(FieldType):
    kind = "real"

    def __post_init__(self):
        self._validate_declaration(0)

    def _check(self, value, path):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise _fail(path, "expected a finite number", value)
        return float(value)


@dataclass(frozen=True, kw_only=True)
class Boolean(FieldType):
    """A flag. SQLite stores it as 0/1; both that and a real boolean are accepted and exported as a boolean."""

    kind = "boolean"

    def __post_init__(self):
        self._validate_declaration(0)

    def _check(self, value, path):
        if type(value) is bool:
            return value
        if type(value) is int and value in (0, 1):
            return bool(value)
        raise _fail(path, "expected a boolean", value)


@dataclass(frozen=True, kw_only=True)
class Obj(FieldType):
    """An object with exactly these properties: a missing or an undeclared one is rejected."""

    fields: Mapping[str, FieldType]

    kind = "object"

    def __post_init__(self):
        object.__setattr__(self, "fields", _frozen_fields(self.fields))
        self._validate_declaration(0)

    def _validate_declaration(self, depth):
        super()._validate_declaration(depth)
        if depth >= MAXIMUM_DEPTH:
            raise HistoryCompositionError("Structured schemas are limited to four levels")
        if not self.fields:
            raise HistoryCompositionError("An object schema declares at least one property")
        for name, child in self.fields.items():
            if not NAME.fullmatch(name):
                raise HistoryCompositionError("Object property names are lowercase identifiers")
            if not isinstance(child, FieldType):
                raise HistoryCompositionError("Object properties are FieldType declarations")
            child._validate_declaration(depth + 1)

    def _check(self, value, path):
        if not isinstance(value, Mapping):
            raise _fail(path, "expected an object", value)
        extra = sorted(str(key) for key in value if key not in self.fields)
        if extra:
            raise FieldValueError(path, f"undeclared properties: {', '.join(extra[:5])}")
        missing = sorted(name for name in self.fields if name not in value)
        if missing:
            raise FieldValueError(path, f"missing properties: {', '.join(missing[:5])}")
        return {name: child.check(value[name], f"{path}.{name}") for name, child in self.fields.items()}

    def _describe(self):
        return {"fields": {name: child.describe() for name, child in sorted(self.fields.items())}}


@dataclass(frozen=True, kw_only=True)
class Arr(FieldType):
    """A bounded list of one declared item type."""

    item: FieldType
    max_items: int

    kind = "array"

    def __post_init__(self):
        self._validate_declaration(0)

    def _validate_declaration(self, depth):
        super()._validate_declaration(depth)
        if depth >= MAXIMUM_DEPTH:
            raise HistoryCompositionError("Structured schemas are limited to four levels")
        if not isinstance(self.item, FieldType):
            raise HistoryCompositionError("An array declares its item type")
        if type(self.max_items) is not int or not 1 <= self.max_items <= MAXIMUM_ITEMS:
            raise HistoryCompositionError(f"An array declares a bound of 1 to {MAXIMUM_ITEMS} items")
        self.item._validate_declaration(depth + 1)

    def _check(self, value, path):
        if not isinstance(value, (list, tuple)):
            raise _fail(path, "expected a list", value)
        if len(value) > self.max_items:
            raise FieldValueError(path, "list exceeds its declared bound")
        return [self.item.check(item, f"{path}[{index}]") for index, item in enumerate(value)]

    def _describe(self):
        return {"item": self.item.describe(), "max_items": self.max_items}


def _frozen_fields(fields: Any) -> Mapping[str, FieldType]:
    if not isinstance(fields, Mapping):
        raise HistoryCompositionError("Object properties are a mapping of names to FieldType declarations")
    return MappingProxyType(dict(fields))


SCALARS = (Text, Integer, Real, Boolean)
