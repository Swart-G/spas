"""Immutable JSON views backed by mapping proxies and tuples."""

from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Any, NoReturn

from pydantic import AfterValidator, BeforeValidator, JsonValue, PlainSerializer


def _reject_mutation(*args: Any, **kwargs: Any) -> NoReturn:
    raise TypeError("JSON snapshots are immutable; replace the snapshot instead")


class _FrozenDict(Mapping[str, Any]):
    __slots__ = ("_data",)

    def __new__(cls, values: Mapping[str, Any]):
        snapshot = super().__new__(cls)
        object.__setattr__(snapshot, "_data", MappingProxyType(dict(values)))
        return snapshot

    def __init__(self, values: Mapping[str, Any]):
        # Populate only during allocation; reinitialization must not mutate data.
        pass

    __setattr__ = __delattr__ = _reject_mutation
    __setitem__ = __delitem__ = _reject_mutation
    clear = pop = popitem = setdefault = update = _reject_mutation
    __ior__ = _reject_mutation

    def __getitem__(self, key: str):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        return repr(dict(self))

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


class _FrozenList(Sequence[Any]):
    __slots__ = ("_items",)

    def __new__(cls, values: Iterable[Any]):
        snapshot = super().__new__(cls)
        object.__setattr__(snapshot, "_items", tuple(values))
        return snapshot

    def __init__(self, values: Iterable[Any]):
        pass

    __setattr__ = __delattr__ = _reject_mutation
    __setitem__ = __delitem__ = _reject_mutation
    append = clear = extend = insert = pop = remove = reverse = sort = _reject_mutation
    __iadd__ = __imul__ = _reject_mutation

    def __getitem__(self, index):
        value = self._items[index]
        return _FrozenList(value) if isinstance(index, slice) else value

    def __len__(self):
        return len(self._items)

    def __eq__(self, other):
        if isinstance(other, (_FrozenList, list)):
            return self._items == tuple(other)
        return NotImplemented

    def __repr__(self):
        return repr(list(self))

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def _freeze_json(value: JsonValue) -> Any:
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("JSON snapshots require finite numbers")
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze_json(item) for item in value)
    return value


def _freeze_data(value: Mapping[str, JsonValue]) -> _FrozenDict:
    return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})


def _thaw_json(value: Any, active: set[int]) -> Any:
    # Normalize immutable views for validation/serialization and reject cycles
    # without rejecting shared, non-cyclic subtrees.
    if not isinstance(value, (Mapping, list, _FrozenList)):
        return value
    identity = id(value)
    if identity in active:
        raise ValueError("JSON snapshots cannot contain cyclic data")
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            return {key: _thaw_json(item, active) for key, item in value.items()}
        return [_thaw_json(item, active) for item in value]
    finally:
        active.remove(identity)


def _thaw_data(value: Any) -> Any:
    try:
        return _thaw_json(value, set())
    except RecursionError as exc:
        raise ValueError("JSON snapshot nesting is too deep") from exc


# Validation still enforces JSON values. Serialization returns detached ordinary
# dictionaries/lists; the read-only runtime views do not inherit their mutators.
SnapshotData = Annotated[
    Mapping[str, JsonValue],
    BeforeValidator(_thaw_data),
    AfterValidator(_freeze_data),
    PlainSerializer(_thaw_data, return_type=dict[str, JsonValue]),
]
