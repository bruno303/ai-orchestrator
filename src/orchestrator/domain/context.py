"""Serializable, namespaced context shared across provider boundaries."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
import json
from types import MappingProxyType
from typing import Any, Protocol


def _validated(data: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for namespace, values in (data or {}).items():
        if not isinstance(namespace, str):
            raise TypeError("context namespaces must be strings")
        if not isinstance(values, Mapping):
            raise TypeError(f"context namespace {namespace!r} must be a mapping")
        result[namespace] = deepcopy(dict(values))
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError("context contains values that are not JSON-serializable") from exc
    return result


class Context(Mapping[str, Mapping[str, Any]]):
    """Immutable-by-convention JSON data grouped by provider-owned namespace.

    Namespace merges are shallow and right-biased. Whole-context merges merge
    matching namespaces and preserve every unrelated namespace.
    """

    def __init__(self, data: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._data = _validated(data)

    def __getitem__(self, name: str) -> Mapping[str, Any]:
        if name not in self._data:
            raise KeyError(name)
        return MappingProxyType(deepcopy(self._data[name]))

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Context):
            return self._data == other._data
        if isinstance(other, Mapping):
            return self._data == dict(other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"Context({self._data!r})"

    def namespace(self, name: str) -> Mapping[str, Any]:
        if not isinstance(name, str):
            raise TypeError("context namespace name must be a string")
        return MappingProxyType(deepcopy(self._data.get(name, {})))

    def with_namespace(self, name: str, values: Mapping[str, Any]) -> "Context":
        updated = self.to_dict()
        updated[name] = dict(values)
        return Context(updated)

    def merge_namespace(self, name: str, values: Mapping[str, Any]) -> "Context":
        return self.with_namespace(name, {**self._data.get(name, {}), **dict(values)})

    def merged(self, other: "Context | Mapping[str, Mapping[str, Any]] | None" = None) -> "Context":
        result = Context(self._data)
        candidate = other if isinstance(other, Context) else Context(other)
        for name, values in candidate._data.items():
            result = result.merge_namespace(name, values)
        return result

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._data)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Context":
        return cls(value)  # type: ignore[arg-type]


class ContextPresenter(Protocol):
    def logging_fields(self, context: Context) -> Mapping[str, Any]: ...


class NoopContextPresenter:
    def logging_fields(self, context: Context) -> Mapping[str, Any]:
        return {}
