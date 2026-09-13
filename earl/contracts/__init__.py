"""Shared contracts between Track A and Track B.

This package is the agreed boundary from Sprint 0. It is owned by neither
track: change it only by agreement, and bump CONTRACT_VERSION when you do.

    Track A --[DependencyGraph]--> Track B
    Track A <--[Decision]--------- Track B
"""

from .common import CONTRACT_VERSION, SI_UNITS, Serializable, Units, UnitSystem
from .decision import (
    CrossCheck,
    Decision,
    MemberResult,
    MemberStatus,
    Outcome,
    SanityChecks,
    SkyCivReport,
)
from .graph import (
    ChangeEvent,
    ChangeKind,
    DependencyGraph,
    Edge,
    EdgeKind,
    LoadCase,
    Material,
    Member,
    Node,
    OnshapeRef,
    PointLoad,
    Section,
    SupportType,
)

__all__ = [
    "CONTRACT_VERSION",
    "SI_UNITS",
    "Serializable",
    "Units",
    "UnitSystem",
    # graph (Track A -> Track B)
    "DependencyGraph",
    "ChangeEvent",
    "ChangeKind",
    "Edge",
    "EdgeKind",
    "LoadCase",
    "Material",
    "Member",
    "Node",
    "OnshapeRef",
    "PointLoad",
    "Section",
    "SupportType",
    # decision (Track B -> Track A)
    "Decision",
    "Outcome",
    "MemberResult",
    "MemberStatus",
    "SanityChecks",
    "SkyCivReport",
    "CrossCheck",
]
