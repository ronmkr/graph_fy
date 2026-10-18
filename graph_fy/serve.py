# MCP stdio server - exposes graph query tools to Claude and other agents
from __future__ import annotations
import json
import math
import os
import re
import sys
import warnings
from array import array
from collections import OrderedDict
from pathlib import Path
import threading
from typing import NamedTuple
import networkx as nx
from networkx.readwrite import json_graph
from graph_fy.security import sanitize_label, check_graph_file_size_cap, _CONTROL_CHAR_RE
from graph_fy.build import edge_data, edge_datas
from graph_fy.paths import default_graph_json as _default_graph_json

try:
    with warnings.catch_warnings():
        # jieba/jieba-py carry invalid regex escapes that emit a SyntaxWarning on
        # compile; the message and line moved across Python versions (3.12 also
        # reworded it), so ignore the category wholesale here rather than pinning
        # a message/lineno. Under `-W error::SyntaxWarning` this keeps the import
        # from escalating to a fatal SyntaxError.
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba as _jieba  # type: ignore[import-untyped]
except (ImportError, SyntaxError):
    # A stray/broken jieba must never take down the whole server import.
    _jieba = None


class ToolError(Exception):
    """Raised by a tool handler to signal an error result.

    A normal string return is sent as an ordinary (successful) text result. A
    ToolError is instead turned into a tool result with ``isError: true`` so a
    client that only checks ``isError`` can tell a genuine failure — e.g. the
    ``gh`` CLI missing or a PR that cannot be resolved — from success.
    """


def _load_graph(graph_path: str) -> nx.Graph:
    try:
        resolved = Path(graph_path).resolve()
        if resolved.suffix != ".json":
            raise ValueError(f"Graph path must be a .json file, got: {graph_path!r}")
        if not resolved.exists():
            raise FileNotFoundError(f"Graph file not found: {resolved}")
        check_graph_file_size_cap(resolved)
        safe = resolved
        data = json.loads(safe.read_text(encoding="utf-8"))
        if "links" not in data and "edges" in data:
            data = dict(data, links=data["edges"])
        # Stash the on-disk logical flag before the load-time override below:
        # `directed: True` exists only so renderers can recover stored arc
        # order (#2309); tools that care about logical direction (#2487) must
        # not mistake the override for graph truth.
        _logical_directed = bool(data.get("directed", False))
        data = {**data, "directed": True}
        try:
            from graph_fy.build import graph_has_legacy_ids as _legacy
            if _legacy(data.get("nodes", [])):
                print(
                    "[graph_fy] note: this graph uses the pre-#1504 node-ID scheme; "
                    "rebuild with `graph_fy extract --force` for path-qualified IDs.",
                    file=sys.stderr,
                )
        except Exception:
            pass
        try:
            G = json_graph.node_link_graph(data, edges="links")
        except TypeError:
            G = json_graph.node_link_graph(data)
        G.graph["_logical_directed"] = _logical_directed
        # Attach the work-memory overlay (derived sidecar next to graph.json) so
        # the query/MCP read surface can annotate NODE lines display-only. Empty
        # when no sidecar exists, leaving un-annotated output byte-identical.
        try:
            from graph_fy.reflect import load_learning_overlay as _llo
            G.graph["_learning_overlay"] = _llo(resolved)
        except Exception:
            G.graph["_learning_overlay"] = {}
        return G
    except json.JSONDecodeError as exc:
        print(f"error: graph.json is corrupted ({exc}). Re-run /graph_fy to rebuild.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def _communities_from_graph(G: nx.Graph) -> dict[int, list[str]]:
    """Reconstruct community dict from community property stored on nodes."""
    communities: dict[int, list[str]] = {}
    for node_id, data in G.nodes(data=True):
        cid = data.get("community")
        if cid is not None:
            communities.setdefault(int(cid), []).append(node_id)
    return communities


def _max_server_contexts() -> int:
    """Return the project-context LRU capacity (default 8, minimum 1).

    ``graph_fy_MAX_CONTEXTS`` overrides the default. Invalid or blank values
    use 8; zero and negative values clamp to 1, since each request needs a
    graph context. The server's configured default graph is pinned separately
    and does not count against this limit.
    """
    raw = os.environ.get("graph_fy_MAX_CONTEXTS", "").strip()
    if not raw:
        return 8
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


class _GraphContextCache:
    """Thread-safe graph contexts: one pinned default plus an LRU of projects."""

    def __init__(self, max_contexts: int):
        self._max_contexts = max_contexts
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._pinned: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _load_entry(self, resolved_path: str, key: tuple[int, int]) -> dict:
        """Build one entry for an already-resolved path and known file key.

        ``_load_graph`` is also used by the CLI, where invalid input terminates
        the process. A client-supplied ``project_path`` must instead become a
        tool error, so the shared MCP server can continue serving other graphs.
        """
        try:
            graph = _load_graph(resolved_path)
        except SystemExit as exc:
            raise RuntimeError(f"could not load graph.json at {resolved_path}") from exc
        # Warm the index before exposing the graph so its first query does not
        # pay the expensive build cost.
        _get_trigram_index(graph)
        communities = _communities_from_graph(graph)
        entry = {
            "key": key,
            "G": graph,
            "communities": communities,
        }
        return entry

    def load(self, resolved_path: str, *, pinned: bool = False) -> tuple[nx.Graph, dict[int, list[str]]]:
        """Return a fresh context, retaining project contexts by LRU order.

        ``resolved_path`` is resolved by the caller, making this method the
        sole owner of file statting and cache-key construction.

        ``pinned=True`` is reserved for the server's configured default graph;
        it remains warm without consuming a project-cache slot.
        """
        with self._lock:
            try:
                stat_result = Path(resolved_path).stat()
            except FileNotFoundError:
                raise FileNotFoundError(f"graph.json not found: {resolved_path}") from None
            key = (stat_result.st_mtime_ns, stat_result.st_size)
            entries = self._pinned if pinned else self._entries
            entry = entries.get(resolved_path)
            if entry is not None and entry["key"] == key:
                if not pinned:
                    self._entries.move_to_end(resolved_path)
                return entry["G"], entry["communities"]

            entry = self._load_entry(resolved_path, key)
            entries[resolved_path] = entry
            if not pinned:
                self._entries.move_to_end(resolved_path)
                while len(self._entries) > self._max_contexts:
                    self._entries.popitem(last=False)
            return entry["G"], entry["communities"]


from graph_fy.export import _strip_diacritics


from graph_fy.serve_search import (  # noqa: F401
    _search_tokens,
    _has_chinese,
    _segment_chinese,
    _is_searchable,
    _QUERY_STOPWORDS,
    _query_terms,
    _EXACT_MATCH_BONUS,
    _PREFIX_MATCH_BONUS,
    _SUBSTRING_MATCH_BONUS,
    _SOURCE_MATCH_BONUS,
    _RATIONALE_MATCH_BONUS,
    _ATTRIBUTE_MATCH_BONUS,
    _flatten_attr_text,
    _node_attributes_text,
    _compute_idf,
    _trigrams,
    _node_rationale_text,
    _node_search_text,
    _get_trigram_index,
    _trigram_candidates,
    _QueryScores,
    _score_nodes,
    _score_query,
    _pick_scored_endpoint,
    _pick_seeds,
    _RELATIONAL_INTENT_TERMS,
)

from graph_fy.serve_traversal import (  # noqa: F401
    _CONTEXT_HINTS,
    _CONTEXT_FILTER_ALIASES,
    _normalize_context_filters,
    _infer_context_filters,
    _resolve_context_filters,
    _filter_graph_by_context,
    _complete_induced_edges,
    _bfs,
    _dfs,
    _subgraph_to_text,
    _cut_lines_to_budget,
    _display_graph_path,
    _traversal_view,
    _query_graph_text,
    _resolve_path_scoped_symbol,
    _label_has_literal_exact_match,
    _find_node_tiers,
    _find_node,
    find_node_ambiguity,
    _node_arg,
    _resolve_single_node,
    _shortest_path_text,
    _filter_blank_stdin,
    _community_header,
)

def _build_server(graph_path: str):
    """Build the configured low-level MCP Server (shared by every transport).

    All graph query tools and resources are registered here over a single
    ``mcp.server.Server`` instance; the caller picks the transport (stdio or
    Streamable HTTP) and runs it. Hot-reload of graph.json works the same way
    regardless of transport, since reloads happen inside the tool handlers.
    """
    try:
        from mcp.server import Server
        from mcp import types
    except ImportError as e:
        raise ImportError('mcp not installed. Run: pip install "graph_fyy[mcp]"') from e
    try:
        from mcp.types import AnyUrl
    except ImportError:
        # mcp >= 2.0 dropped the AnyUrl re-export; it was always pydantic's
        # AnyUrl (pydantic is an mcp dependency, so this import cannot miss).
        from pydantic import AnyUrl

    from graph_fy import paths as _paths

    # Graph contexts comprise one pinned configured default plus a bounded LRU
    # of project_path graphs. This preserves the configured graph's warm index
    # while preventing a shared server from retaining every project it serves.
    _default_graph_path = str(Path(graph_path).resolve())
    _ctx_cache = _GraphContextCache(_max_server_contexts())

    def _load_ctx(path: str):
        """Return the current default or project graph context as a tool error.

        Unlike ``_load_graph``, this never lets a missing or corrupt client
        graph terminate the MCP process; it raises so other projects remain
        available on the same server.
        """
        resolved_path = str(Path(path).resolve())
        return _ctx_cache.load(resolved_path, pinned=resolved_path == _default_graph_path)

    def _resolve_graph_path(project_path) -> str:
        """Map an optional project_path to a concrete graph.json path. ``None``
        keeps the server's default graph (backward-compatible); a project_path
        resolves to ``<project_path>/<graph_fy_OUT>/graph.json``, honouring the
        graph_fy_OUT override so worktree/shared-output setups keep working."""
        if not project_path:
            return _default_graph_path
        return str(Path(project_path) / _paths.graph_fy_OUT / "graph.json")

    # Active per-request context, rebound by _select_graph() and read by the tool
    # handlers below. No lock needed on the hot path: _select_graph and the
    # handler run in one synchronous stretch of each call_tool coroutine (no
    # await between them), so a concurrent call never observes a half-applied
    # swap.
    active_graph_path = _default_graph_path
    try:
        G, communities = _load_ctx(_default_graph_path)
    except (FileNotFoundError, RuntimeError):
        # No default graph at startup → run as a pure multi-project server. Tools
        # then require project_path; a call without one gets a clear error rather
        # than the process refusing to start (which is what _load_graph would do).
        G, communities = None, {}

    def _select_graph(project_path) -> None:
        nonlocal G, communities, active_graph_path
        path = _resolve_graph_path(project_path)
        G, communities = _load_ctx(path)
        active_graph_path = str(Path(path).resolve())

    # NOTE: no decorators here — the handlers below are plain coroutines,
    # bound to the Server at the END of this function in a version-aware way:
    # mcp 1.x exposes the @server.list_tools()/... decorator API, mcp 2.x
    # replaced it with on_list_tools=/... constructor callbacks.
    async def list_tools() -> list[types.Tool]:
        _tools = [
            types.Tool(
                name="query_graph",
                description="Search the knowledge graph using BFS or DFS. Returns relevant nodes and edges as text context.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "Natural language question or keyword search"},
                        "mode": {"type": "string", "enum": ["bfs", "dfs"], "default": "bfs",
                                 "description": "bfs=broad context, dfs=trace a specific path"},
                        "depth": {"type": "integer", "default": 3, "description": "Traversal depth (1-6)"},
                        "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
                        "context_filter": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional explicit edge-context filter, e.g. ['call', 'field']",
                        },
                    },
                    "required": ["question"],
                },
            ),
            types.Tool(
                name="get_node",
                description="Get full details for a specific node by label or ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Node label or ID to look up"},
                        "node_id": {"type": "string", "description": "Alias for label (node id)"},
                    },
                },
            ),
            types.Tool(
                name="get_neighbors",
                description="Get all direct neighbors of a node with edge details.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "node_id": {"type": "string", "description": "Alias for label (node id)"},
                        "relation_filter": {"type": "string", "description": "Optional: filter by relation type"},
                        "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
                    },
                },
            ),
            types.Tool(
                name="get_community",
                description="Get all nodes in a community by community ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "community_id": {"type": "integer", "description": "Community ID (0-indexed by size)"},
                        "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
                    },
                    "required": ["community_id"],
                },
            ),
            types.Tool(
                name="god_nodes",
                description="Return the most connected nodes - the core abstractions of the knowledge graph.",
                inputSchema={"type": "object", "properties": {
                    "top_n": {"type": "integer", "default": 10},
                    "exclude_hubs_percentile": {"type": "number",
                                                "description": "Suppress nodes whose degree exceeds this percentile (0-100) of the degree distribution, matching cluster()'s hub exclusion"},
                }},
            ),
            types.Tool(
                name="graph_stats",
                description="Return summary statistics: node count, edge count, communities, confidence breakdown.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="shortest_path",
                description=(
                    "Find the shortest path between two concepts in the knowledge graph. "
                    "Follows stored edge direction by default; set undirected=true to ignore it."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source concept label or keyword"},
                        "target": {"type": "string", "description": "Target concept label or keyword"},
                        "max_hops": {"type": "integer", "default": 8, "description": "Maximum hops to consider"},
                        "undirected": {"type": "boolean", "default": False,
                                       "description": "Ignore stored edge direction when searching"},
                    },
                    "required": ["source", "target"],
                },
            ),
            types.Tool(
                name="list_prs",
                description=(
                    "List open GitHub PRs with CI status, review state, and graph impact "
                    "(which communities each PR touches, blast radius). Use this before starting "
                    "work to check if a PR already covers the area you're about to change."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "base": {"type": "string", "description": "Base branch to filter PRs by (auto-detected if omitted)"},
                        "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
                    },
                },
            ),
            types.Tool(
                name="get_pr_impact",
                description=(
                    "Get detailed graph impact for a specific PR: which files it changes, "
                    "which knowledge-graph communities are affected, and how many nodes are touched. "
                    "Use this to assess merge risk or check for overlap with your current work."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "pr_number": {"type": "integer", "description": "PR number to analyse"},
                        "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
                    },
                    "required": ["pr_number"],
                },
            ),
            types.Tool(
                name="triage_prs",
                description=(
                    "Return all actionable open PRs (correct base, not stale) with full graph impact data "
                    "so you can reason about review priority, merge order, and conflict risk. "
                    "Call this when the user asks 'what PRs should I review?' or 'what's ready to merge?'"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "base": {"type": "string", "description": "Base branch to filter PRs by (auto-detected if omitted)"},
                        "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
                    },
                },
            ),
            types.Tool(
                name="get_impact_analysis",
                description=(
                    "Compute predictive impact analysis and blast radius for a given target symbol, node, or file. "
                    "Traverses downstream dependency closure along calls, imports, inherits, and references edges, "
                    "detects affected unit/integration tests, external API endpoints, IaC resources, and computes a "
                    "blast radius risk score and tier (LOW, MEDIUM, HIGH, CRITICAL)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Target symbol name, node ID, or source file path"},
                        "depth": {"type": "integer", "default": 10, "description": "Downstream traversal depth"},
                        "terse": {"type": "boolean", "default": False, "description": "Return compact 1-line summary"},
                    },
                    "required": ["target"],
                },
            ),
            types.Tool(
                name="get_blast_radius",
                description="Alias for get_impact_analysis: assess blast radius and downstream dependents for a target.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Target symbol name, node ID, or source file path"},
                        "depth": {"type": "integer", "default": 10, "description": "Downstream traversal depth"},
                        "terse": {"type": "boolean", "default": False, "description": "Return compact 1-line summary"},
                    },
                    "required": ["target"],
                },
            ),
            types.Tool(
                name="get_execution_flow",
                description=(
                    "Trace execution flow slicing from an entry point (HTTP route, gRPC RPC, CLI command, event consumer) "
                    "forward to terminal data sinks (SQL tables, Redis cache, Kafka topics, external HTTP endpoints). "
                    "Optionally filter to specific paths ending at a target sink."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entry_point": {"type": "string", "description": "Entry point identifier or label (e.g. 'POST /login', 'AuthService.authenticate', 'main()')"},
                        "sink": {"type": "string", "description": "Optional terminal sink identifier or label (e.g. 'users table', 'redis_cache')"},
                        "max_depth": {"type": "integer", "default": 15, "description": "Maximum traversal depth (default 15)"},
                    },
                    "required": ["entry_point"],
                },
            ),
            types.Tool(
                name="find_definitions",
                description="Look up exact symbol declaration node(s) in graph.json, returning name, type, source_file, line ranges (start_line, end_line), and docstring with token savings.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Exact symbol name to look up"},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="get_callers",
                description="Query inbound call hierarchy (nodes that have outgoing 'calls' edge to the symbol node).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Symbol name to query callers for"},
                    },
                    "required": ["symbol"],
                },
            ),
            types.Tool(
                name="get_callees",
                description="Query outbound call hierarchy (nodes that the symbol node has outgoing 'calls' edges to).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Symbol name to query callees for"},
                    },
                    "required": ["symbol"],
                },
            ),
            types.Tool(
                name="find_references",
                description="Enumerate all cross-file usages, calls, and imports referencing the symbol.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Symbol name to find references for"},
                    },
                    "required": ["symbol"],
                },
            ),
            types.Tool(
                name="find_implementations",
                description="Find all classes/structs inheriting from or implementing the specified interface/abstract class.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Interface or abstract class symbol name"},
                    },
                    "required": ["symbol"],
                },
            ),
            types.Tool(
                name="get_code_skeleton",
                description="Extract Tree-sitter interface skeleton of a file (signatures, types, docstrings; bodies elided).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Source file path to extract skeleton for"},
                    },
                    "required": ["path"],
                },
            ),
            types.Tool(
                name="get_symbol_implementation",
                description="Retrieve exact implementation code for a specific node_id or symbol on demand.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "node_id_or_symbol": {"type": "string", "description": "Node ID or symbol name to extract implementation for"},
                        "skeletonize": {"type": "boolean", "default": False, "description": "Whether to elide nested function bodies"},
                    },
                    "required": ["node_id_or_symbol"],
                },
            ),
            types.Tool(
                name="get_diff_context",
                description="Retrieve git diff-scoped ego-network context (touched symbols, 1-hop callers, affected tests, API routes) for code review.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "base_ref": {"type": "string", "default": "HEAD", "description": "Git ref to diff against"},
                        "token_budget": {"type": "integer", "default": 2000, "description": "Maximum token budget for returned review context"},
                    },
                },
            ),
            types.Tool(
                name="refine_context",
                description="Iterative feedback querying (RepoCoder): extract unresolved identifiers from draft code and retrieve missing skeletons.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "code_draft": {"type": "string", "description": "Incomplete code draft written by agent"},
                        "token_budget": {"type": "integer", "default": 2000, "description": "Maximum token budget for returned dependency context"},
                    },
                    "required": ["code_draft"],
                },
            ),
            types.Tool(
                name="get_agent_context",
                description="Generate a holistic, deterministic pre-agent architectural briefing (subsystems, Mermaid diagram, Tier 1 AST skeletons, execution flow, co-changes, tests, token savings) before calling frontier models.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Coding task or architectural inquiry description"},
                        "token_budget": {"type": "integer", "default": 1500, "description": "Maximum token budget for returned briefing"},
                        "include_diagram": {"type": "boolean", "default": True, "description": "Whether to include Mermaid flow diagram"},
                    },
                    "required": ["task"],
                },
            ),
        ]
        # Multi-project support: every tool accepts an optional project_path.
        # Injected here (rather than repeated in 11 literal schemas) so the set
        # stays in lockstep as tools are added. Omitting it keeps the historical
        # single-graph behaviour, so this is purely additive for existing callers.
        for _t in _tools:
            # The constructor accepts the camelCase alias in both majors, but
            # attribute access is inputSchema on mcp 1.x and input_schema on 2.x.
            _schema = getattr(_t, "inputSchema", None)
            if _schema is None:
                _schema = _t.input_schema
            _schema.setdefault("properties", {})["project_path"] = {
                "type": "string",
                "description": (
                    "Absolute path to a project directory containing "
                    "graph_fy_out/graph.json. Optional — defaults to the graph "
                    "this server was started with."
                ),
            }
        return _tools

    def _tool_query_graph(arguments: dict) -> str:
        import time as _time
        from graph_fy import querylog
        question = arguments["question"]
        mode = arguments.get("mode", "bfs")
        depth = min(int(arguments.get("depth", 3)), 6)
        budget = int(arguments.get("token_budget", 2000))
        context_filter = arguments.get("context_filter")
        _t0 = _time.perf_counter()
        result = _query_graph_text(
            G,
            question,
            mode=mode,
            depth=depth,
            token_budget=budget,
            context_filters=context_filter,
            graph_path=str(active_graph_path),
        )
        querylog.log_query(
            kind="mcp_query",
            question=question,
            corpus=str(active_graph_path),
            result=result,
            mode=mode,
            depth=depth,
            token_budget=budget,
            duration_ms=(_time.perf_counter() - _t0) * 1000,
        )
        return result

    def _tool_get_node(arguments: dict) -> str:
        raw = _node_arg(arguments)
        if not raw:
            return "Provide a node label or id (accepted keys: label, node_id, id)."
        label = raw.lower()
        nid, err = _resolve_single_node(G, label)
        if err:
            return err
        d = G.nodes[nid]
        attrs = d.get("attributes")
        attrs_line = []
        if isinstance(attrs, dict) and attrs:
            raw_attrs = json.dumps(attrs, sort_keys=True)
            if len(raw_attrs) > 1000:
                raw_attrs = raw_attrs[:997] + "..."
            attrs_line = [f"  Attributes: {_CONTROL_CHAR_RE.sub('', raw_attrs)}"]
        # Sanitise every LLM-derived field before concatenation (F-010).
        return "\n".join([
            f"Node: {sanitize_label(d.get('label', nid))}",
            f"  ID: {sanitize_label(nid)}",
            f"  Source: {sanitize_label(str(d.get('source_file', '')))} {sanitize_label(str(d.get('source_location', '')))}",
            # A C/C++/ObjC symbol declared in a header and defined in the sibling
            # impl file is ONE node keyed to the header, so Source alone points at
            # the declaration. Name where it is implemented too, when known.
            *([f"  Defined in: {sanitize_label(str(d.get('definition_file', '')))} "
               f"{sanitize_label(str(d.get('definition_location', '')))}"]
              if d.get("definition_file") else []),
            f"  Type: {sanitize_label(str(d.get('file_type', '')))}",
            f"  Community: {sanitize_label(str(d.get('community_name') or d.get('community', '')))}",
            f"  Degree: {G.degree(nid)}",
            *attrs_line,
        ])

    def _tool_get_neighbors(arguments: dict) -> str:
        raw = _node_arg(arguments)
        if not raw:
            return "Provide a node label or id (accepted keys: label, node_id, id)."
        label = raw.lower()
        rel_filter = arguments.get("relation_filter", "").lower()
        nid, err = _resolve_single_node(G, label)
        if err:
            return err
        lines = [f"Neighbors of {sanitize_label(G.nodes[nid].get('label', nid))}:"]
        def _edge_at(d: dict) -> str:
            # Edge location = the relation SITE (call/import line) in the source
            # node's file, not a def line (#BUG1).
            loc = str(d.get("source_location") or "")
            return (
                f" at={sanitize_label(str(d.get('source_file') or ''))}:{sanitize_label(loc)}"
                if loc else ""
            )
        for nb in G.successors(nid):
            d = edge_data(G, nid, nb)
            rel = d.get("relation", "")
            if rel_filter and rel_filter not in rel.lower():
                continue
            lines.append(
                f"  --> {sanitize_label(G.nodes[nb].get('label', nb))} "
                f"[{sanitize_label(str(rel))}] [{sanitize_label(str(d.get('confidence', '')))}]{_edge_at(d)}"
            )
        for nb in G.predecessors(nid):
            d = edge_data(G, nb, nid)
            rel = d.get("relation", "")
            if rel_filter and rel_filter not in rel.lower():
                continue
            lines.append(
                f"  <-- {sanitize_label(G.nodes[nb].get('label', nb))} "
                f"[{sanitize_label(str(rel))}] [{sanitize_label(str(d.get('confidence', '')))}]{_edge_at(d)}"
            )
        budget = int(arguments.get("token_budget", 2000))
        return _cut_lines_to_budget(
            lines, budget, "Narrow with relation_filter or use get_node for a specific symbol"
        )

    def _tool_get_community(arguments: dict) -> str:
        cid = int(arguments["community_id"])
        nodes = communities.get(cid, [])
        if not nodes:
            return f"Community {cid} not found."
        header = _community_header(cid, G.nodes[nodes[0]].get("community_name"))
        lines = [f"{header} ({len(nodes)} nodes):"]
        for n in nodes:
            d = G.nodes[n]
            # Sanitise label and source_file (F-010).
            lines.append(
                f"  {sanitize_label(d.get('label', n))} "
                f"[{sanitize_label(str(d.get('source_file', '')))}]"
            )
        budget = int(arguments.get("token_budget", 2000))
        return _cut_lines_to_budget(
            lines, budget, "Raise token_budget or use get_node for specific members"
        )

    def _tool_god_nodes(arguments: dict) -> str:
        from graph_fy.analyze import god_nodes as _god_nodes
        _pct = arguments.get("exclude_hubs_percentile")
        nodes = _god_nodes(
            G, top_n=int(arguments.get("top_n", 10)),
            exclude_hubs_percentile=float(_pct) if _pct is not None else None,
        )
        lines = ["God nodes (most connected):"]
        lines += [f"  {i}. {n['label']} - {n['degree']} edges" for i, n in enumerate(nodes, 1)]
        return "\n".join(lines)

    def _tool_graph_stats(_: dict) -> str:
        confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
        total = len(confs) or 1
        return (
            f"Nodes: {G.number_of_nodes()}\n"
            f"Edges: {G.number_of_edges()}\n"
            f"Communities: {len(communities)}\n"
            f"EXTRACTED: {round(confs.count('EXTRACTED')/total*100)}%\n"
            f"INFERRED: {round(confs.count('INFERRED')/total*100)}%\n"
            f"AMBIGUOUS: {round(confs.count('AMBIGUOUS')/total*100)}%\n"
        )

    def _tool_shortest_path(arguments: dict) -> str:
        return _shortest_path_text(G, arguments)

    def _tool_list_prs(arguments: dict) -> str:
        from graph_fy.prs import fetch_prs, fetch_worktrees, format_prs_text, _detect_default_branch
        repo = arguments.get("repo") or None
        base = arguments.get("base") or _detect_default_branch(repo)
        try:
            prs = fetch_prs(repo=repo, base=base)
        except RuntimeError as e:
            raise ToolError(f"Error: {e}") from e
        worktrees = fetch_worktrees()
        for pr in prs:
            pr.worktree_path = worktrees.get(pr.branch)
        return format_prs_text(prs, base)

    def _tool_get_pr_impact(arguments: dict) -> str:
        from graph_fy.prs import fetch_pr_files, compute_pr_impact, _gh, _parse_ci
        number = int(arguments["pr_number"])
        repo = arguments.get("repo") or None
        # Use gh pr view directly — works for any base branch, not just the default
        view_args = ["pr", "view", str(number), "--json",
                     "title,headRefName,baseRefName,author,isDraft,reviewDecision,statusCheckRollup,updatedAt"]
        if repo:
            view_args += ["--repo", repo]
        pr_data = _gh(*view_args)
        if pr_data is None:
            raise ToolError(f"PR #{number} not found or gh not authenticated.")
        files = fetch_pr_files(number, repo)
        if not files:
            return f"PR #{number}: no changed files found (may require gh auth)."
        comms, nodes = compute_pr_impact(files, G)
        ci = _parse_ci(pr_data.get("statusCheckRollup") or [])
        lines = [
            f"PR #{number}: {pr_data['title']}",
            f"CI: {ci}  Review: {pr_data.get('reviewDecision') or 'none'}",
            f"Base: {pr_data['baseRefName']}  Author: {(pr_data.get('author') or {}).get('login', '?')}",
            f"\nGraph impact: {nodes} nodes across {len(comms)} communities",
            f"Communities touched: {comms}",
            f"Files changed ({len(files)}):",
        ]
        lines += [f"  {f}" for f in files[:20]]
        if len(files) > 20:
            lines.append(f"  … and {len(files) - 20} more")
        return "\n".join(lines)

    def _tool_triage_prs(arguments: dict) -> str:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from graph_fy.prs import fetch_prs, fetch_worktrees, fetch_pr_files, compute_pr_impact, _STATUS_ORDER, _detect_default_branch
        repo = arguments.get("repo") or None
        base = arguments.get("base") or _detect_default_branch(repo)
        try:
            prs = fetch_prs(repo=repo, base=base)
        except RuntimeError as e:
            raise ToolError(f"Error: {e}") from e
        worktrees = fetch_worktrees()
        for pr in prs:
            pr.worktree_path = worktrees.get(pr.branch)
        actionable = [p for p in prs if p.base_branch == base and p.status not in ("WRONG-BASE", "STALE")]
        if not actionable:
            return f"No actionable PRs targeting {base}."
        # Fetch diffs concurrently then compute graph impact using in-memory G
        workers = min(8, len(actionable))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_pr = {pool.submit(fetch_pr_files, pr.number, repo): pr for pr in actionable}
            for fut in as_completed(future_to_pr):
                pr = future_to_pr[fut]
                try:
                    files = fut.result()
                except Exception:
                    files = []
                if files:
                    pr.files_changed = files
                    pr.communities_touched, pr.nodes_affected = compute_pr_impact(files, G)
        header = (
            f"Actionable PRs targeting {base}: {len(actionable)}\n"
            "Rank these by review priority. Higher blast_radius = more graph communities affected = higher merge risk.\n"
        )
        lines = [header]
        for p in sorted(actionable, key=lambda x: (_STATUS_ORDER.index(x.status) if x.status in _STATUS_ORDER else 99)):
            impact = f"  blast_radius={p.blast_radius}" if p.blast_radius else ""
            wt = f"  worktree={p.worktree_path}" if p.worktree_path else ""
            lines.append(
                f"PR #{p.number} [{p.status}] CI={p.ci_status} review={p.review_decision or 'none'} "
                f"age={p.days_old}d author={p.author}{impact}{wt}\n  title: {p.title}"
            )
        return "\n\n".join(lines)

    def _tool_get_impact_analysis(arguments: dict) -> str:
        from graph_fy.impact import compute_impact, format_impact_report
        target = arguments.get("target", "")
        if not target:
            raise ToolError("Missing required argument: 'target'")
        depth = int(arguments.get("depth", 10))
        terse = bool(arguments.get("terse", False))
        impact_data = compute_impact(G, target, max_depth=depth)
        return format_impact_report(impact_data, terse=terse)

    def _tool_get_execution_flow(arguments: dict) -> str:
        from graph_fy.mcp import get_execution_flow
        entry = arguments.get("entry_point", "")
        if not entry:
            raise ToolError("Missing required argument: 'entry_point'")
        sink = arguments.get("sink") or None
        depth = int(arguments.get("max_depth", 15))
        return str(get_execution_flow(entry_point=entry, sink=sink, max_depth=depth, graph=G))

    def _tool_find_definitions(arguments: dict) -> str:
        from graph_fy.mcp import find_definitions
        query = arguments.get("query", "")
        if not query:
            raise ToolError("Missing required argument: 'query'")
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = find_definitions(query, graph=G, root_dir=root)
        return json.dumps(res, indent=2)

    def _tool_get_callers(arguments: dict) -> str:
        from graph_fy.mcp import get_callers
        symbol = arguments.get("symbol", "")
        if not symbol:
            raise ToolError("Missing required argument: 'symbol'")
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = get_callers(symbol, graph=G, root_dir=root)
        return json.dumps(res, indent=2)

    def _tool_get_callees(arguments: dict) -> str:
        from graph_fy.mcp import get_callees
        symbol = arguments.get("symbol", "")
        if not symbol:
            raise ToolError("Missing required argument: 'symbol'")
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = get_callees(symbol, graph=G, root_dir=root)
        return json.dumps(res, indent=2)

    def _tool_find_references(arguments: dict) -> str:
        from graph_fy.mcp import find_references
        symbol = arguments.get("symbol", "")
        if not symbol:
            raise ToolError("Missing required argument: 'symbol'")
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = find_references(symbol, graph=G, root_dir=root)
        return json.dumps(res, indent=2)

    def _tool_find_implementations(arguments: dict) -> str:
        from graph_fy.mcp import find_implementations
        symbol = arguments.get("symbol", "")
        if not symbol:
            raise ToolError("Missing required argument: 'symbol'")
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = find_implementations(symbol, graph=G, root_dir=root)
        return json.dumps(res, indent=2)

    def _tool_get_code_skeleton(arguments: dict) -> str:
        from graph_fy.mcp import get_code_skeleton
        path = arguments.get("path", "")
        if not path:
            raise ToolError("Missing required argument: 'path'")
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = get_code_skeleton(path, root_dir=root)
        return json.dumps(res, indent=2)

    def _tool_get_symbol_implementation(arguments: dict) -> str:
        from graph_fy.mcp import get_symbol_implementation
        target = arguments.get("node_id_or_symbol", "")
        if not target:
            raise ToolError("Missing required argument: 'node_id_or_symbol'")
        skeletonize = bool(arguments.get("skeletonize", False))
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = get_symbol_implementation(target, graph=G, root_dir=root, skeletonize=skeletonize)
        return json.dumps(res, indent=2)

    def _tool_get_diff_context(arguments: dict) -> str:
        from graph_fy.mcp import get_diff_context
        base_ref = str(arguments.get("base_ref", "HEAD"))
        budget = int(arguments.get("token_budget", 2000))
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = get_diff_context(base_ref=base_ref, root_dir=root, token_budget=budget, graph=G)
        return json.dumps(res, indent=2)

    def _tool_refine_context(arguments: dict) -> str:
        from graph_fy.mcp import refine_context
        draft = arguments.get("code_draft", "")
        if not draft:
            raise ToolError("Missing required argument: 'code_draft'")
        budget = int(arguments.get("token_budget", 2000))
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = refine_context(code_draft=draft, root_dir=root, token_budget=budget, graph=G)
        return json.dumps(res, indent=2)

    def _tool_get_agent_context(arguments: dict) -> str:
        from graph_fy.mcp import get_agent_context
        task = arguments.get("task", "")
        if not task:
            raise ToolError("Missing required argument: 'task'")
        budget = int(arguments.get("token_budget", 1500))
        include_diagram = bool(arguments.get("include_diagram", True))
        root = str(Path(active_graph_path).parent.parent) if Path(active_graph_path).parent.name == "graph_fy_out" else str(Path(active_graph_path).parent)
        res = get_agent_context(task=task, root_dir=root, token_budget=budget, graph=G, include_diagram=include_diagram)
        return json.dumps(res, indent=2)

    _handlers = {
        "query_graph": _tool_query_graph,
        "get_node": _tool_get_node,
        "get_neighbors": _tool_get_neighbors,
        "get_community": _tool_get_community,
        "god_nodes": _tool_god_nodes,
        "graph_stats": _tool_graph_stats,
        "shortest_path": _tool_shortest_path,
        "list_prs": _tool_list_prs,
        "get_pr_impact": _tool_get_pr_impact,
        "triage_prs": _tool_triage_prs,
        "get_impact_analysis": _tool_get_impact_analysis,
        "get_blast_radius": _tool_get_impact_analysis,
        "get_execution_flow": _tool_get_execution_flow,
        "find_definitions": _tool_find_definitions,
        "get_callers": _tool_get_callers,
        "get_callees": _tool_get_callees,
        "find_references": _tool_find_references,
        "find_implementations": _tool_find_implementations,
        "get_code_skeleton": _tool_get_code_skeleton,
        "get_symbol_implementation": _tool_get_symbol_implementation,
        "get_diff_context": _tool_get_diff_context,
        "refine_context": _tool_refine_context,
        "get_agent_context": _tool_get_agent_context,
    }

    def _load_community_labels() -> dict[int, str]:
        labels_path = Path(active_graph_path).parent / ".graph_fy_labels.json"
        if labels_path.exists():
            try:
                return {int(k): v for k, v in json.loads(labels_path.read_text(encoding="utf-8")).items()}
            except Exception:
                pass
        return {cid: f"Community {cid}" for cid in communities}

    async def list_resources() -> list[types.Resource]:
        # Plain-string URIs on purpose: mcp 1.x types the field as AnyUrl and
        # coerces strings, mcp 2.x types it as str and REJECTS AnyUrl objects.
        return [
            types.Resource(uri="graph_fy://report", name="Graph Report", description="Full GRAPH_REPORT.md", mimeType="text/markdown"),
            types.Resource(uri="graph_fy://stats", name="Graph Stats", description="Node/edge/community counts and confidence breakdown", mimeType="text/plain"),
            types.Resource(uri="graph_fy://god-nodes", name="God Nodes", description="Top 10 most-connected nodes", mimeType="text/plain"),
            types.Resource(uri="graph_fy://surprises", name="Surprising Connections", description="Cross-community surprising connections", mimeType="text/plain"),
            types.Resource(uri="graph_fy://audit", name="Confidence Audit", description="EXTRACTED/INFERRED/AMBIGUOUS edge breakdown", mimeType="text/plain"),
            types.Resource(uri="graph_fy://questions", name="Suggested Questions", description="Suggested questions for this codebase", mimeType="text/plain"),
        ]

    async def read_resource(uri: AnyUrl) -> str:
        _select_graph(None)  # resources read the server's default graph
        uri_str = str(uri)
        if uri_str == "graph_fy://report":
            report_path = Path(active_graph_path).parent / "GRAPH_REPORT.md"
            if report_path.exists():
                return report_path.read_text(encoding="utf-8")
            return "GRAPH_REPORT.md not found. Run graph_fy extract first."
        if uri_str == "graph_fy://stats":
            return _tool_graph_stats({})
        if uri_str == "graph_fy://god-nodes":
            return _tool_god_nodes({"top_n": 10})
        if uri_str == "graph_fy://surprises":
            try:
                from graph_fy.analyze import surprising_connections
                surprises = surprising_connections(G, communities, top_n=10)
                if not surprises:
                    return "No surprising connections found."
                lines = ["Surprising cross-community connections:"]
                for s in surprises:
                    lines.append(f"  {s.get('source', '')} <-> {s.get('target', '')} [{s.get('relation', '')}]")
                return "\n".join(lines)
            except Exception as exc:
                return f"Could not compute surprising connections: {exc}"
        if uri_str == "graph_fy://audit":
            confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
            total = len(confs) or 1
            return (
                f"Total edges: {total}\n"
                f"EXTRACTED: {confs.count('EXTRACTED')} ({round(confs.count('EXTRACTED')/total*100)}%)\n"
                f"INFERRED: {confs.count('INFERRED')} ({round(confs.count('INFERRED')/total*100)}%)\n"
                f"AMBIGUOUS: {confs.count('AMBIGUOUS')} ({round(confs.count('AMBIGUOUS')/total*100)}%)\n"
            )
        if uri_str == "graph_fy://questions":
            try:
                from graph_fy.analyze import suggest_questions
                community_labels = _load_community_labels()
                questions = suggest_questions(G, communities, community_labels, top_n=10)
                if not questions:
                    return "No suggested questions available."
                lines = ["Suggested questions:"]
                for q in questions:
                    if isinstance(q, dict):
                        lines.append(f"  - {q.get('question', '')}")
                    else:
                        lines.append(f"  - {q}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Could not generate questions: {exc}"
        raise ValueError(f"Unknown resource: {uri_str}")

    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        arguments = dict(arguments or {})
        project_path = arguments.pop("project_path", None)
        handler = _handlers.get(name)
        if not handler:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
        try:
            _select_graph(project_path)  # bind G/communities to the target graph
            return [types.TextContent(type="text", text=handler(arguments))]
        except ToolError:
            # A handler-signalled error: propagate so the result is marked
            # isError:true (the mcp 1.x decorator wraps a raised exception into
            # an error result; the 2.x path catches it in _on_call_tool).
            raise
        except Exception as exc:
            return [types.TextContent(type="text", text=f"Error executing {name}: {exc}")]

    if hasattr(Server, "list_tools"):
        # mcp 1.x: decorator-based registration. The SDK wraps the raw returns
        # (list[Tool] -> ListToolsResult, str -> resource contents) itself.
        server = Server("graph_fy")
        server.list_tools()(list_tools)
        server.call_tool()(call_tool)
        server.list_resources()(list_resources)
        server.read_resource()(read_resource)
    else:
        # mcp 2.x: handlers ride the Server constructor as on_* callbacks with
        # the (ctx, params) -> Result contract, so wrap the same impls and
        # build the result models the 1.x decorators used to build for us.
        async def _on_list_tools(ctx, params) -> types.ListToolsResult:
            return types.ListToolsResult(tools=await list_tools())

        async def _on_call_tool(ctx, params) -> types.CallToolResult:
            try:
                content = await call_tool(params.name, dict(params.arguments or {}))
            except ToolError as exc:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=str(exc))],
                    isError=True,
                )
            return types.CallToolResult(content=content)

        async def _on_list_resources(ctx, params) -> types.ListResourcesResult:
            return types.ListResourcesResult(resources=await list_resources())

        async def _on_read_resource(ctx, params) -> types.ReadResourceResult:
            text = await read_resource(params.uri)
            mime = "text/markdown" if str(params.uri).startswith("graph_fy://report") else "text/plain"
            return types.ReadResourceResult(
                contents=[types.TextResourceContents(uri=params.uri, mimeType=mime, text=text)]
            )

        try:
            from importlib.metadata import version as _pkg_version
            try:
                _version = _pkg_version("graph_fy")
            except Exception:
                _version = _pkg_version("graph_fyy")
        except Exception:
            _version = "0"
        server = Server(
            "graph_fy",
            version=_version,
            on_list_tools=_on_list_tools,
            on_call_tool=_on_call_tool,
            on_list_resources=_on_list_resources,
            on_read_resource=_on_read_resource,
        )

    return server


def serve(graph_path: str | None = None) -> None:
    """Start the MCP server over stdio (the default, per-developer transport)."""
    graph_path = graph_path or _default_graph_json()
    try:
        from mcp.server.stdio import stdio_server
    except ImportError as e:
        raise ImportError('mcp not installed. Run: pip install "graph_fyy[mcp]"') from e
    import asyncio

    server = _build_server(graph_path)

    async def main() -> None:
        async with stdio_server() as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    _filter_blank_stdin()
    asyncio.run(main())

from graph_fy.serve_http import (  # noqa: F401
    _MCPASGIApp,
    _ApiKeyMiddleware,
    _build_http_app,
    serve_http,
)

def _main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="python -m graph_fy.serve",
        description="Serve a graph_fy knowledge graph over MCP (stdio or Streamable HTTP).",
    )
    parser.add_argument(
        "graph_path",
        nargs="?",
        default=None,
        help="Path to graph.json (default: graph_fy_out/graph.json)",
    )
    parser.add_argument(
        "--graph",
        dest="graph_flag",
        default=None,
        metavar="PATH",
        help="Path to graph.json — alias for the positional argument",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport to serve on (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port (default: 8080)")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("graph_fy_API_KEY"),
        help="Require this key on the HTTP transport (env: graph_fy_API_KEY)",
    )
    parser.add_argument("--path", default="/mcp", help="HTTP mount path (default: /mcp)")
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="Return plain JSON responses instead of SSE streams",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="Run without per-session state (for load-balanced / CI deployments)",
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=3600.0,
        help="Reap stateful sessions idle this many seconds (default: 3600; 0 disables)",
    )
    args = parser.parse_args(argv)
    graph_path = args.graph_flag or args.graph_path or _default_graph_json()

    if args.transport == "http":
        serve_http(
            graph_path,
            host=args.host,
            port=args.port,
            api_key=args.api_key,
            path=args.path,
            json_response=args.json_response,
            stateless=args.stateless,
            session_timeout=args.session_timeout,
        )
    else:
        serve(graph_path)


if __name__ == "__main__":
    _main()
