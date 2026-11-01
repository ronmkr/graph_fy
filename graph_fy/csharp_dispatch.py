"""Member-level interface dispatch for C# (#3003).

A C# call through a constructor-injected dependency lands on the interface's
method node, because that is what the call site names: `_report.Build()` where
`_report` is an `IReport` resolves to `IReport.Build()`. The implementing
`Report.Build()` is a separate node, and nothing joins the two, so a directed
walk stops at the interface and every chain through an injected dependency is
cut at that point. On a Scrutor-scanned .NET service where every dependency is
an interface, that is most chains.

The mechanism — single-owner, per-method `dispatches_to` edges at INFERRED,
fanning out to every implementer — lives in `graph_fy.interface_dispatch` so
that another language can add a gate rather than a copy; this module fixes
the C# gate.
"""
from __future__ import annotations

from graph_fy.interface_dispatch import (  # noqa: F401
    DISPATCH_RELATION,
    method_label as _method_label,
    resolve_interface_dispatch,
)

_CSHARP_SUFFIXES = (".cs",)


def resolve_csharp_interface_dispatch(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Link each C# interface method to every implementation that owns it unambiguously."""
    resolve_interface_dispatch(per_file, all_nodes, all_edges, suffixes=_CSHARP_SUFFIXES)
