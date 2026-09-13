"""Shared primitives for the Track A <-> Track B contracts.

Everything here is common to both the dependency graph and the decision object:
schema versioning, the unit system, and dependency-free JSON serialization.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, Type, TypeVar

# Bump on ANY breaking change to either contract, and tell the other track.
# Minor version = additive/backward-compatible. Major = breaking.
CONTRACT_VERSION = "0.3.0"

T = TypeVar("T", bound="Serializable")


class UnitSystem(str, Enum):
    """Unit systems the contract can be expressed in.

    SI is the default and the only one the pipeline is validated against.
    IMPERIAL exists so an Onshape document authored in inches/lbf can be
    labelled honestly rather than silently misread -- conversion is the
    producer's job, not the consumer's.
    """

    SI = "SI"              # length m, force N, stress Pa, area m^2, E Pa
    IMPERIAL = "IMPERIAL"  # length in, force lbf, stress psi, area in^2, E psi


@dataclass(frozen=True)
class Units:
    """Explicit unit declaration carried on every payload that crosses tracks.

    This is deliberately not optional. Two independently developed halves that
    disagree about whether a stress is Pa or MPa produce a plausible-looking
    number that is wrong by 10^6 -- precisely the silent failure the project
    exists to catch. Consumers MUST check this rather than assume.
    """

    system: UnitSystem = UnitSystem.SI
    length: str = "m"
    force: str = "N"
    stress: str = "Pa"
    area: str = "m^2"

    def assert_si(self) -> None:
        if (self.system is not UnitSystem.SI or
                (self.length, self.force, self.stress, self.area) !=
                ("m", "N", "Pa", "m^2")):
            raise ValueError(
                f"expected SI units, payload declares {self.system.value}; "
                "convert at the producer before handing the payload across tracks"
            )


SI_UNITS = Units()


def _encode(value: Any) -> Any:
    """JSON-safe encoding: enums become their values, dataclasses become dicts."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _encode(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(cls: Type[Any], value: Any) -> Any:
    """Rebuild a dataclass (or enum, or list thereof) from plain JSON types."""
    import types
    import typing

    origin = getattr(cls, "__origin__", None)
    # Optional[X] / X | None -- decode against the non-None branch.
    if origin is typing.Union or isinstance(cls, types.UnionType):
        if value is None:
            return None
        inner = [a for a in typing.get_args(cls) if a is not type(None)]
        return _decode(inner[0], value) if len(inner) == 1 else value
    if origin in (list, tuple):
        (inner,) = cls.__args__
        return [_decode(inner, v) for v in value]
    if isinstance(cls, type) and issubclass(cls, Enum):
        return cls(value)
    if is_dataclass(cls) and isinstance(value, dict):
        # Resolve annotations here rather than trusting f.type: under
        # `from __future__ import annotations` every f.type is a string, so
        # nested generics like list[PointLoad] would silently stay as dicts.
        hints = typing.get_type_hints(cls)
        kwargs = {}
        for f in fields(cls):
            if f.name in value:
                kwargs[f.name] = _decode(hints.get(f.name, f.type), value[f.name])
        return cls(**kwargs)
    return value


class Serializable:
    """Mixin giving every contract object a symmetric JSON round-trip.

    The two tracks may end up in separate processes (or separate machines at
    demo time), so every contract object must survive a trip through JSON
    without losing type information.
    """

    def to_dict(self) -> dict[str, Any]:
        validator = getattr(self, "validate", None)
        if validator:
            validator()
        return _encode(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, allow_nan=False)

    @classmethod
    def from_dict(cls: Type[T], data: dict[str, Any]) -> T:
        # Resolve string annotations produced by `from __future__ import annotations`.
        import typing

        hints = typing.get_type_hints(cls)
        kwargs = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name in data:
                kwargs[f.name] = _decode(hints[f.name], data[f.name])
        obj = cls(**kwargs)
        validator = getattr(obj, "validate", None)
        if validator:
            validator()
        return obj

    @classmethod
    def from_json(cls: Type[T], raw: str) -> T:
        return cls.from_dict(json.loads(raw))
