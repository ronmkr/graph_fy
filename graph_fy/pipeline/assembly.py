"""ID remapping, provenance tagging, POSIX normalization, and graph assembly."""
from __future__ import annotations

import os
from pathlib import Path, PurePath

from graph_fy.extractors.base import _file_node_id, _file_stem, _make_id


def _default_portable_out_of_root_sf(p: Path, root: Path) -> str:
    """Portable source_file relativization when path lies outside root."""
    try:
        rel = os.path.relpath(str(p), str(root)).replace("\\", "/")
    except ValueError:
        return p.name
    updepth = 0
    for seg in rel.split("/"):
        if seg == "..":
            updepth += 1
        else:
            break
    return p.name if updepth > 3 else rel


def assemble_graph(
    all_nodes: list[dict],
    all_edges: list[dict],
    root: Path,
    paths: list[Path],
    failed_sources: list[str],
    file_node_id_fn=None,
    portable_sf_fn=None,
) -> dict:
    """Execute phase 4: relativize, remap IDs, tag provenance, and assemble final dictionary."""
    if file_node_id_fn is None:
        file_node_id_fn = _file_node_id

    if portable_sf_fn is None:
        portable_sf_fn = lambda p: _default_portable_out_of_root_sf(p, root)

    ext_id_remap: dict[str, str] = {}
    owned_ids = {n.get("id") for n in all_nodes}
    sf_forms: dict[str, tuple[str, str, tuple[str, ...]]] = {}

    def _sf_entry(sf: str, sf_path: Path) -> tuple[str, str, tuple[str, ...]]:
        cached = sf_forms.get(sf)
        if cached is not None:
            return cached
        try:
            rel = sf_path.relative_to(root)
        except ValueError:
            portable = portable_sf_fn(sf_path)
            canonical_id = _make_id("ext", portable)
            new_sf = portable
        else:
            canonical_id = file_node_id_fn(rel)
            new_sf = rel.as_posix()
        try:
            sf_resolved = sf_path.resolve()
        except (OSError, RuntimeError):
            sf_resolved = sf_path

        keys = tuple({
            _make_id(str(sf_path)),
            _make_id(str(sf_resolved)),
            _make_id(_file_stem(sf_path)),
            _make_id(_file_stem(sf_resolved)),
        })
        entry = (new_sf, canonical_id, keys)
        sf_forms[sf] = entry
        return entry

    for item in all_nodes + all_edges:
        sf = item.get("source_file")
        if sf:
            sf_path = Path(sf)
            if sf_path.is_absolute():
                new_sf, canonical_id, keys = _sf_entry(str(sf), sf_path)
                if "id" in item:
                    for key in keys:
                        if key == canonical_id or key in ext_id_remap:
                            continue
                        if key in owned_ids and item.get("id") != key:
                            continue
                        ext_id_remap[key] = canonical_id
                item["source_file"] = new_sf
        df = item.get("definition_file")
        if df:
            df_path = Path(df)
            if df_path.is_absolute():
                new_df, _, _ = _sf_entry(str(df), df_path)
                item["definition_file"] = new_df

    if ext_id_remap:
        _ENTRY = "__entry"

        def _canon(nid: str) -> str:
            if nid in ext_id_remap:
                return ext_id_remap[nid]
            if nid.endswith(_ENTRY) and nid[: -len(_ENTRY)] in ext_id_remap:
                return ext_id_remap[nid[: -len(_ENTRY)]] + _ENTRY
            if nid not in owned_ids:
                idx = nid.rfind("_")
                while idx > 0:
                    canonical = ext_id_remap.get(nid[:idx])
                    if canonical is not None:
                        return canonical + nid[idx:]
                    idx = nid.rfind("_", 0, idx)
            return nid

        for n in all_nodes:
            if n.get("id"):
                n["id"] = _canon(n["id"])
        for e in all_edges:
            if e.get("source"):
                e["source"] = _canon(e["source"])
            if e.get("target"):
                e["target"] = _canon(e["target"])

    for n in all_nodes:
        n.pop("origin_file", None)

    for e in all_edges:
        e.pop("local_alias", None)

    for n in all_nodes:
        n["_origin"] = "ast"
    for e in all_edges:
        e["_origin"] = "ast"

    # Inject temporal coupling co-changes and churn annotations from git history
    try:
        from graph_fy.git_history import inject_git_temporal_edges
        inject_git_temporal_edges(all_nodes, all_edges, root, file_node_id_fn=file_node_id_fn)
    except Exception:
        pass

    for _item in (*all_nodes, *all_edges):
        _sf = _item.get("source_file")
        if _sf and "\\" in str(_sf):
            _item["source_file"] = PurePath(_sf).as_posix()
        _df = _item.get("definition_file")
        if _df and "\\" in str(_df):
            _item["definition_file"] = PurePath(_df).as_posix()

    return {
        "nodes": all_nodes,
        "edges": all_edges,
        "input_tokens": 0,
        "output_tokens": 0,
        "failed_sources": failed_sources,
        "extracted_sources": [str(p) for p in paths],
    }
