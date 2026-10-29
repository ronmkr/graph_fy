"""Pipeline package for graph_fy extraction: discovery, workers, resolvers, assembly."""
from __future__ import annotations

from graph_fy.pipeline.assembly import assemble_graph
from graph_fy.pipeline.discovery import collect_files, infer_root_and_cache
from graph_fy.pipeline.resolvers import run_resolution_phase
from graph_fy.pipeline.workers import run_worker_phase

__all__ = [
    "assemble_graph",
    "collect_files",
    "infer_root_and_cache",
    "run_resolution_phase",
    "run_worker_phase",
]
