"""query_engine.py — Unified Query, Traversal, and Retrieval Kernel.

Unifies seed resolution, FTS5 + RapidFuzz scoring, Personalized PageRank (PPR),
reverse-BFS blast radius, and 3-tier AST skeletonization between CLI and MCP.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import networkx as nx

from graph_fy.affected import (
    DEFAULT_AFFECTED_RELATIONS,
    AffectedHit,
    affected_nodes as _affected_nodes,
    resolve_seed as _resolve_seed,
)
from graph_fy.impact import (
    IMPACT_RELATIONS,
    compute_impact as _compute_impact,
    format_impact_report as _format_impact_report,
    resolve_targets as _resolve_targets,
)
from graph_fy.retrieval_context import (
    diff_context as _diff_context,
    refine_query as _refine_query,
    suggest_close_symbols as _suggest_close_symbols,
    task_context as _task_context,
)
from graph_fy.retrieval_format import (
    format_compact_path as _format_compact_path,
    format_query_results as _format_query_results,
    format_rag_prompt as _format_rag_prompt,
    retrieve as _retrieve,
)
from graph_fy.retrieval_index import (
    build_index as _build_index,
    compute_pagerank as _compute_pagerank,
)
from graph_fy.skeleton import (
    compact_signature as _compact_signature,
    get_symbol_code_from_file as _get_symbol_code_from_file,
    skeletonize_code as _skeletonize_code,
    skeletonize_file as _skeletonize_file,
)


class GraphQueryEngine:
    """Stateless unified query kernel for graph_fy CLI and MCP server."""

    # --- Seed Resolution ---
    @staticmethod
    def resolve_seed(graph: nx.Graph, query: str, root: Path | None = None) -> str | None:
        """Resolve query string to single best seed node ID."""
        return _resolve_seed(graph, query, root=root)

    @staticmethod
    def resolve_targets(graph: nx.Graph, target: str, root: Path | None = None) -> list[str]:
        """Resolve query target to all matching node IDs."""
        return _resolve_targets(graph, target, root=root)

    # --- Inbound & Outbound Calling ---
    @staticmethod
    def get_callers_and_callees(G: nx.Graph, nid: Any) -> tuple[list[str], list[str]]:
        """Find immediate callers and callees for a node."""
        from graph_fy.retrieval import _find_callers_and_callees
        return _find_callers_and_callees(G, nid)

    # --- Traversal & Impact Analysis ---
    @staticmethod
    def find_affected(
        graph: nx.Graph,
        seed: str,
        *,
        relations: Iterable[str] = DEFAULT_AFFECTED_RELATIONS,
        depth: int = 2,
    ) -> list[AffectedHit]:
        """Traverse upstream reverse-BFS to identify affected dependent nodes."""
        return _affected_nodes(graph, seed, relations=relations, depth=depth)

    @staticmethod
    def compute_impact(
        graph: nx.Graph,
        target: str,
        max_depth: int = 10,
        root: Path | None = None,
    ) -> dict[str, Any]:
        """Calculate holistic blast radius risk, affected tests, APIs, and IaC resources."""
        return _compute_impact(graph, target, max_depth=max_depth, root=root)

    @staticmethod
    def format_impact_report(impact_data: dict[str, Any], terse: bool = False) -> str:
        """Render formatted human-readable impact analysis report."""
        return _format_impact_report(impact_data, terse=terse)

    # --- Zero-Deserialization SQLite Relational Queries (#22) ---
    @staticmethod
    def get_sqlite_store(db_path: Path | str | None = None) -> Any:
        """Open SqliteGraphStore if database exists."""
        from graph_fy.paths import default_graph_db
        from graph_fy.storage_sqlite import SqliteGraphStore
        p = Path(db_path) if db_path is not None else Path(default_graph_db())
        if p.exists():
            return SqliteGraphStore(p)
        return None

    @staticmethod
    def find_affected_sqlite(
        seed: str,
        db_path: Path | str | None = None,
        *,
        relations: Iterable[str] = DEFAULT_AFFECTED_RELATIONS,
        depth: int = 2,
    ) -> list[dict[str, Any]]:
        """Zero-deserialization reverse-BFS blast radius traversal via SQLite recursive CTE (<2ms)."""
        from graph_fy.paths import default_graph_db
        from graph_fy.storage_sqlite import SqliteGraphStore
        p = Path(db_path) if db_path is not None else Path(default_graph_db())
        with SqliteGraphStore(p) as store:
            return store.reverse_bfs(seed, relations=relations, max_depth=depth)

    @staticmethod
    def k_hop_ego_sqlite(
        seed: str,
        db_path: Path | str | None = None,
        *,
        max_depth: int = 1,
        directed: bool = True,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Zero-deserialization k-hop ego sub-graph retrieval via SQLite recursive CTE (<2ms)."""
        from graph_fy.paths import default_graph_db
        from graph_fy.storage_sqlite import SqliteGraphStore
        p = Path(db_path) if db_path is not None else Path(default_graph_db())
        with SqliteGraphStore(p) as store:
            return store.k_hop_ego_graph(seed, max_depth=max_depth, directed=directed)

    # --- GraphRAG Querying ---
    @staticmethod
    def query(
        query_text: str,
        graph: nx.Graph | None = None,
        db_path: Path | str | None = None,
        *,
        limit: int = 10,
        hops: int = 2,
        graph_path: Path | str | None = None,
        synonyms: dict[str, str] | None = None,
        fuzzy: bool = True,
        fuzzy_min_results: int = 1,
        terse: bool = False,
        caveman: bool = False,
        skeleton: bool = False,
        boundary_pruning: bool = True,
        root: Path | str | None = None,
    ) -> list[dict[str, Any]] | str:
        """Execute 4-step GraphRAG retrieval (Seed -> Expand -> Re-rank -> Return)."""
        from graph_fy.retrieval import query as _retrieval_query
        return _retrieval_query(
            query_text=query_text,
            graph=graph,
            db_path=db_path,
            limit=limit,
            hops=hops,
            graph_path=graph_path,
            synonyms=synonyms,
            fuzzy=fuzzy,
            fuzzy_min_results=fuzzy_min_results,
            terse=terse,
            caveman=caveman,
            skeleton=skeleton,
            boundary_pruning=boundary_pruning,
            root=root,
        )

    # --- Pre-Agent & Diff Context ---
    @staticmethod
    def task_context(
        task: str,
        graph: nx.Graph | None = None,
        root_dir: str = ".",
        token_budget: int = 1500,
        graph_path: str | None = None,
        include_diagram: bool = True,
        include_bodies: bool | str = "auto",
    ) -> dict[str, Any]:
        """Generate deterministic pre-agent architectural briefing before LLM calls."""
        return _task_context(
            task=task,
            graph=graph,
            root_dir=root_dir,
            token_budget=token_budget,
            graph_path=graph_path,
            include_diagram=include_diagram,
            include_bodies=include_bodies,
        )

    @staticmethod
    def diff_context(
        repo_root: Path | str = ".",
        base_ref: str = "HEAD~1",
        graph: nx.Graph | None = None,
        graph_path: Path | str | None = None,
        token_budget: int = 2000,
    ) -> dict[str, Any]:
        """Generate git diff-scoped architectural and blast radius context."""
        return _diff_context(
            root_dir=repo_root,
            base_ref=base_ref,
            graph=graph,
            graph_path=graph_path,
            token_budget=token_budget,
        )

    # --- Skeletonization & Code Elision ---
    @staticmethod
    def skeletonize_file(file_path: Path | str) -> str:
        """Extract Tree-sitter interface skeleton for a file."""
        return _skeletonize_file(file_path)

    @staticmethod
    def skeletonize_code(code: bytes | str, parser: Any, lang: str) -> str:
        """Extract Tree-sitter interface skeleton from source code bytes."""
        return _skeletonize_code(code, parser, lang)

    @staticmethod
    def get_symbol_code(
        file_path: Path | str,
        line_number: int,
        *,
        skeletonize: bool = False,
        compact_docstrings: bool = False,
    ) -> str:
        """Extract source code slice for a symbol, optionally skeletonized."""
        return _get_symbol_code_from_file(
            file_path,
            line_number,
            skeletonize=skeletonize,
            compact_docstrings=compact_docstrings,
        )


_default_engine = GraphQueryEngine()


def get_query_engine() -> GraphQueryEngine:
    """Return default singleton GraphQueryEngine."""
    return _default_engine
