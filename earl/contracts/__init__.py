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
    EscalationReason,
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
    TargetKind,
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
    "TargetKind",
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
    "EscalationReason",
    "MemberResult",
    "MemberStatus",
    "SanityChecks",
    "SkyCivReport",
    "CrossCheck",
]
