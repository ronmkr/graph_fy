"""graph_fy - extract · build · cluster · analyze · report."""

__version__ = "0.0.1"


def __getattr__(name):
    # Lazy imports so `graph_fy install` works before heavy deps are in place.
    _map = {
        "extract": ("graph_fy.extract", "extract"),
        "collect_files": ("graph_fy.extract", "collect_files"),
        "build_from_json": ("graph_fy.build", "build_from_json"),
        "cluster": ("graph_fy.cluster", "cluster"),
        "score_all": ("graph_fy.cluster", "score_all"),
        "cohesion_score": ("graph_fy.cluster", "cohesion_score"),
        "god_nodes": ("graph_fy.analyze", "god_nodes"),
        "surprising_connections": ("graph_fy.analyze", "surprising_connections"),
        "suggest_questions": ("graph_fy.analyze", "suggest_questions"),
        "generate": ("graph_fy.report", "generate"),
        "to_json": ("graph_fy.export", "to_json"),
        "to_html": ("graph_fy.export", "to_html"),
        "to_svg": ("graph_fy.export", "to_svg"),
        "to_canvas": ("graph_fy.export", "to_canvas"),
        "to_wiki": ("graph_fy.wiki", "to_wiki"),
        "reflect": ("graph_fy.reflect", "reflect"),
        "save_query_result": ("graph_fy.ingest", "save_query_result"),
        "lint": ("graph_fy.lint", "run_lint"),
        "compute_impact": ("graph_fy.impact", "compute_impact"),
        "format_impact_report": ("graph_fy.impact", "format_impact_report"),
        "find_definitions": ("graph_fy.mcp", "find_definitions"),
        "get_callers": ("graph_fy.mcp", "get_callers"),
        "get_callees": ("graph_fy.mcp", "get_callees"),
        "find_references": ("graph_fy.mcp", "find_references"),
        "find_implementations": ("graph_fy.mcp", "find_implementations"),
        "estimate_token_savings": ("graph_fy.mcp", "estimate_token_savings"),
        "GraphQueryEngine": ("graph_fy.query_engine", "GraphQueryEngine"),
        "get_query_engine": ("graph_fy.query_engine", "get_query_engine"),
    }
    if name in _map:
        import importlib
        mod_name, attr = _map[name]
        mod = importlib.import_module(mod_name)
        return getattr(mod, attr)
    raise AttributeError(f"module 'graph_fy' has no attribute {name!r}")
