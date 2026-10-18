"""Python import repointing passes (package imports and loose sibling imports)."""
from __future__ import annotations

from pathlib import Path
from graph_fy.extractors.base import _file_stem, _make_id


def _file_node_id(rel_path: Path) -> str:
    """File-level node ID matching the skill.md spec: ``{parent_dir}_{stem}``."""
    return _make_id(_file_stem(rel_path))


def _repoint_python_package_imports(paths, all_nodes, all_edges, root) -> None:
    """Repoint Python absolute-import edges to the real file node under a nested
    (e.g. ``src/``) package root (#2072).

    Absolute imports target an id derived from the dotted module path
    (``_make_id('pkg.mod')`` -> ``pkg_mod``), but file-node ids are
    scan-root-relative (``src_pkg_mod`` when the code lives under ``src/``), so
    the edge dangles and is silently dropped — the graph loses most ``imports``
    edges purely because of where the scan started. Build an alias map from the
    dotted-module id to the real file-node id by detecting each ``.py`` file's
    package root (the contiguous run of ancestor dirs carrying ``__init__.py``)
    and rewrite matching ``imports``/``imports_from`` edge targets. Guards: never
    shadow an existing node id, and drop an alias claimed by more than one file
    (ambiguous -> leave dangling, as before). Files whose package root IS the
    scan root are skipped (ids already coincide)."""
    try:
        root = Path(root).resolve()
    except OSError:
        root = Path(root)
    node_ids = {n.get("id") for n in all_nodes if isinstance(n, dict)}
    alias_to_files: dict[str, set[str]] = {}
    for p in paths:
        if p.suffix.lower() not in (".py", ".pyi"):
            continue
        try:
            rel = Path(p).resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        parts = rel.parts
        if len(parts) < 2:
            continue  # top-level file: scan-root-relative id already matches
        d = Path(p).resolve().parent
        levels = 0
        # Bounded by the number of dirs between the file and the scan root, so a
        # pathological `/__init__.py` chain can't loop forever.
        while levels < len(parts) - 1 and (d / "__init__.py").is_file():
            levels += 1
            d = d.parent
        if levels == 0:
            continue  # not inside a package (namespace pkg / loose module)
        mod_parts = parts[-(levels + 1):]  # package dirs + the file itself
        if len(mod_parts) == len(parts):
            continue  # package root == scan root: file-node id already coincides
        file_node = _file_node_id(rel)
        alias = _make_id(str(Path(*mod_parts).with_suffix("")))
        alias_to_files.setdefault(alias, set()).add(file_node)
        if p.name in ("__init__.py", "__init__.pyi") and len(mod_parts) > 1:
            # `import pkg` / `from pkg import x` targets the package-dir id.
            pkg_alias = _make_id(str(Path(*mod_parts[:-1])))
            alias_to_files.setdefault(pkg_alias, set()).add(file_node)
    alias_map = {
        a: next(iter(fs))
        for a, fs in alias_to_files.items()
        if len(fs) == 1 and a not in node_ids
    }
    if not alias_map:
        return
    for e in all_edges:
        # Only repoint edges emitted from a Python file: a non-Python import edge
        # (e.g. C# `using Pkg.Mod;`, Java/Go dotted imports) can have a dangling
        # target string that coincides with a Python alias, and repointing it
        # would fabricate a cross-language import edge (#2072 review).
        if (
            isinstance(e, dict)
            and e.get("relation") in ("imports", "imports_from")
            and str(e.get("source_file", "")).lower().endswith((".py", ".pyi"))
        ):
            tgt = e.get("target")
            if tgt in alias_map:
                e["target"] = alias_map[tgt]


def _repoint_python_sibling_imports(paths, all_nodes, all_edges, root) -> None:
    """Repoint Python sibling-import edges to the real file node in directories
    without an __init__.py (#3430).

    When a Python file below the scan root lives in a non-package directory
    (no __init__.py in its immediate parent), plain imports of same-directory
    modules (e.g. `scripts/main.py: import greeter`) target a bare name (`greeter`),
    while the real file node is scan-root-relative (`scripts_greeter`). Because
    the directory is not a package, `_repoint_python_package_imports` skips it
    (levels == 0), leaving the edge dangling and causing downstream member calls
    (`greeter.greet()`) to be dropped.

    This pass is strictly importer-directory-local:
    - Only applies when the importing file's parent directory has no __init__.py.
    - Resolves only to unambiguous same-directory candidate modules in the scanned corpus.
    - Never builds a global alias map and never searches outside the importer's directory.
    - Preserves local aliases (e.g. `import greeter as g`).
    """
    try:
        root = Path(root).resolve()
    except OSError:
        root = Path(root)

    node_ids = {n.get("id") for n in all_nodes if isinstance(n, dict)}

    # Index corpus modules by directory for non-package directories only.
    # dir_path -> {module_name_id: set_of_file_node_ids}
    dir_siblings: dict[Path, dict[str, set[str]]] = {}
    for p in paths:
        if p.suffix.lower() not in (".py", ".pyi"):
            continue
        try:
            p_res = Path(p).resolve()
            rel = p_res.relative_to(root)
        except (ValueError, OSError):
            continue

        file_node = _file_node_id(rel)
        if file_node not in node_ids:
            continue

        if p_res.name in ("__init__.py", "__init__.pyi"):
            # Sibling package directory inside parent_dir (parent_dir / subpkg / __init__.py)
            pkg_dir = p_res.parent
            parent_dir = pkg_dir.parent
            if not (parent_dir / "__init__.py").is_file() and not (parent_dir / "__init__.pyi").is_file():
                mod_key = _make_id(pkg_dir.name)
                dir_siblings.setdefault(parent_dir, {}).setdefault(mod_key, set()).add(file_node)
            continue

        d = p_res.parent
        # PEP 328 guard: if the directory is a package, implicit relative imports
        # are forbidden in Python 3. Do not index packages as loose sibling directories.
        if (d / "__init__.py").is_file() or (d / "__init__.pyi").is_file():
            continue

        mod_key = _make_id(p_res.stem)
        dir_siblings.setdefault(d, {}).setdefault(mod_key, set()).add(file_node)

    # Require an unambiguous single candidate per module name in that directory.
    dir_alias_map: dict[Path, dict[str, str]] = {
        d: {
            mod: next(iter(fns))
            for mod, fns in mod_map.items()
            if len(fns) == 1
        }
        for d, mod_map in dir_siblings.items()
    }

    if not dir_alias_map:
        return

    for e in all_edges:
        if not (
            isinstance(e, dict)
            and e.get("relation") in ("imports", "imports_from")
            and str(e.get("source_file", "")).lower().endswith((".py", ".pyi"))
        ):
            continue

        sf = e.get("source_file")
        if not sf:
            continue
        try:
            sf_path = Path(sf)
            if not sf_path.is_absolute():
                sf_path = (root / sf_path).resolve()
            else:
                sf_path = sf_path.resolve()
        except OSError:
            continue

        imp_dir = sf_path.parent
        aliases = dir_alias_map.get(imp_dir)
        if not aliases:
            continue

        tgt = e.get("target")
        if tgt in aliases:
            repointed = aliases[tgt]
            if repointed != e.get("source"):
                e["target"] = repointed
