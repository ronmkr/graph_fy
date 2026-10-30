"""Cross-file AST node merging, canonicalization, and rewiring algorithms."""
from __future__ import annotations

import re
from pathlib import Path
from graph_fy.extractors.base import _LANGUAGE_BUILTIN_GLOBALS
from graph_fy.extractors.resolution import _is_type_like_definition
from graph_fy.cargo_introspect import package_name


def _canonicalize_csharp_namespace_nodes(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Collapse duplicate C# namespace node entries to one canonical node per label."""
    by_label: dict[str, list[dict]] = {}
    for node in all_nodes:
        if node.get("type") != "namespace":
            continue
        label = node.get("label")
        if isinstance(label, str):
            by_label.setdefault(label, []).append(node)

    remap: dict[str, str] = {}
    drop_node_ids: set[int] = set()
    for group in by_label.values():
        if len(group) < 2:
            continue
        canonical = sorted(
            group,
            key=lambda node: (
                str(node.get("source_file") or ""),
                str(node.get("source_location") or ""),
                str(node.get("id") or ""),
            ),
        )[0]
        canonical_id = canonical.get("id")
        for node in group:
            if node is canonical:
                continue
            drop_node_ids.add(id(node))
            dup_id = node.get("id")
            if isinstance(dup_id, str) and isinstance(canonical_id, str):
                remap[dup_id] = canonical_id

    if remap:
        for edge in all_edges:
            if edge.get("source") in remap:
                edge["source"] = remap[str(edge["source"])]
            if edge.get("target") in remap:
                edge["target"] = remap[str(edge["target"])]

    if drop_node_ids:
        all_nodes[:] = [node for node in all_nodes if id(node) not in drop_node_ids]


# Languages whose identifiers are case-insensitive, so cross-file name resolution
# may fold case. Everywhere else, case is semantic (`Path` the class vs `PATH` the
# env var are distinct) and folding manufactures false edges / super-hubs (#1581).
_CASE_INSENSITIVE_EXTS = frozenset({
    ".php", ".phtml", ".php3", ".php4", ".php5", ".php7", ".phps",  # PHP fns/classes
    ".sql",                                                          # SQL identifiers
    ".nim", ".nims", ".nimble",                                      # Nim (style-insensitive)
    ".cbl", ".cob", ".cobol", ".cpy",                                # COBOL identifiers
})
_CASE_INSENSITIVE_EXTENSIONS = _CASE_INSENSITIVE_EXTS


def _lang_is_case_insensitive(source_file: object) -> bool:
    """True when the file's language resolves identifiers case-insensitively (#1581)."""
    if not source_file:
        return False
    return Path(str(source_file)).suffix.lower() in _CASE_INSENSITIVE_EXTS


# Language interop families for cross-file call resolution. A call in one language
# can never bind by name to a definition in another family — a TSX component does
# not invoke a Kotlin method, and a Python function does not invoke a Java one.
_LANG_FAMILY_BY_EXT: dict[str, str] = {
    # JS/TS module graph (SFCs embed JS/TS)
    ".js": "jsts", ".jsx": "jsts", ".mjs": "jsts", ".cjs": "jsts",
    ".ts": "jsts", ".tsx": "jsts", ".mts": "jsts", ".cts": "jsts",
    ".vue": "jsts", ".svelte": "jsts", ".astro": "jsts",
    # JVM interop
    ".java": "jvm", ".kt": "jvm", ".kts": "jvm",
    ".scala": "jvm", ".groovy": "jvm", ".gradle": "jvm",
    # C-family: shared headers, Objective-C/C++ mix, Swift↔ObjC bridging
    ".c": "native", ".h": "native", ".cpp": "native", ".cc": "native",
    ".cxx": "native", ".hpp": "native", ".cu": "native", ".cuh": "native",
    ".metal": "native", ".m": "native", ".mm": "native", ".swift": "native",
    # Single-language families
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby", ".rake": "ruby",
    ".php": "php", ".phtml": "php", ".php3": "php", ".php4": "php",
    ".php5": "php", ".php7": "php", ".phps": "php",
    ".cs": "dotnet", ".razor": "dotnet", ".cshtml": "dotnet", ".xaml": "dotnet",
    ".lua": "lua", ".luau": "lua",
    ".zig": "zig",
    ".ex": "elixir", ".exs": "elixir",
    ".jl": "julia",
    ".dart": "dart",
    ".sh": "shell", ".bash": "shell",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".cbl": "cobol", ".cob": "cobol", ".cobol": "cobol", ".cpy": "cobol",
}
_EXT_TO_LANG: dict[str, str] = {
    ".cbl": "cobol",
    ".cob": "cobol",
    ".cobol": "cobol",
    ".cpy": "cobol",
}
_LANG_BY_EXT = _LANG_FAMILY_BY_EXT


def _lang_family(source_file: object) -> str | None:
    """Interop family of the file's language, or None when unknown/not code."""
    if not source_file:
        return None
    return _LANG_FAMILY_BY_EXT.get(Path(str(source_file)).suffix.lower())


_LANGUAGE_BUILTIN_BASE_CLASSES: dict[str, frozenset[str]] = {
    "php": frozenset({
        "Throwable", "Exception", "ErrorException", "Error", "TypeError",
        "ValueError", "ArgumentCountError", "ArithmeticError",
        "DivisionByZeroError", "RuntimeException", "LogicException",
        "InvalidArgumentException", "DomainException", "LengthException",
        "OutOfRangeException", "OutOfBoundsException", "RangeException",
        "OverflowException", "UnderflowException", "UnexpectedValueException",
        "BadFunctionCallException", "BadMethodCallException", "JsonException",
    }),
    "jvm": frozenset({
        "Throwable", "Exception", "RuntimeException", "Error",
        "IllegalArgumentException", "IllegalStateException",
        "UnsupportedOperationException", "IndexOutOfBoundsException",
        "NullPointerException", "IOException",
    }),
    "python": frozenset({
        "BaseException", "Exception", "ValueError", "TypeError", "KeyError",
        "IndexError", "RuntimeError", "NotImplementedError", "AttributeError",
        "OSError", "IOError", "StopIteration", "Warning", "UserWarning",
        "DeprecationWarning",
    }),
    "jsts": frozenset({
        "Error", "TypeError", "RangeError", "SyntaxError", "ReferenceError",
        "EvalError", "URIError", "AggregateError",
    }),
    "dotnet": frozenset({
        "Exception", "ApplicationException", "SystemException",
        "ArgumentException", "ArgumentNullException",
        "ArgumentOutOfRangeException", "InvalidOperationException",
        "NotImplementedException", "NotSupportedException",
    }),
    "ruby": frozenset({
        "Exception", "StandardError", "RuntimeError", "ArgumentError",
        "TypeError", "NameError", "NoMethodError", "IOError",
    }),
}

_LANGUAGE_BUILTIN_BASE_CLASSES_CI: dict[str, frozenset[str]] = {
    family: frozenset(name.lower() for name in names)
    for family, names in _LANGUAGE_BUILTIN_BASE_CLASSES.items()
}


def _node_label_key(node: dict, fold: bool = False) -> str:
    label = str(node.get("label", "")).strip()
    key = re.sub(r"[^a-zA-Z0-9]+", "", label)
    return key.lower() if fold else key


def _is_top_level_function_definition(node: dict) -> bool:
    """A free/top-level function def (label ``name()``), not a method or type."""
    label = str(node.get("label", "")).strip()
    return (
        node.get("file_type") == "code"
        and label.endswith(")")
        and not label.startswith(".")
        and "." not in label
    )


def _rewire_unique_stub_nodes(
    nodes: list[dict], edges: list[dict], root: Path | None = None
) -> None:
    """Map unresolved no-source stubs to a unique real definition with the same label."""
    scan_root = Path(root).resolve() if root is not None else None
    crate_by_dir: dict[Path, str | None] = {}

    def crate_of(source_file: str) -> str | None:
        """The crate a Rust file belongs to: its nearest Cargo.toml with a package
        name, looking no higher than the scan root. A file under none has no crate."""
        if not source_file:
            return None
        path = Path(source_file)
        if not path.is_absolute() and scan_root is not None:
            path = scan_root / path
        directory = path.parent
        walked: list[Path] = []
        crate: str | None = None
        while True:
            if directory in crate_by_dir:
                crate = crate_by_dir[directory]
                break
            walked.append(directory)
            manifest = directory / "Cargo.toml"
            if manifest.is_file() and (crate := package_name(manifest)):
                break
            if directory == scan_root or directory.parent == directory:
                break
            directory = directory.parent
        for seen in walked:
            crate_by_dir[seen] = crate
        return crate

    workspace_crates: set[str] = set()
    for node in nodes:
        sf = str(node.get("source_file", ""))
        if sf.endswith(".rs") and (crate := crate_of(sf)):
            workspace_crates.add(crate)

    reexports: dict[tuple[str, str], set[str]] = {}
    for edge in edges:
        info = edge.get("_rust_pub_use")
        owner = crate_of(str(edge.get("source_file", "")))
        if not isinstance(info, dict) or owner is None:
            continue
        root_seg = str(info.get("root", ""))
        if root_seg != "crate":
            reexports.setdefault((owner, str(info.get("label", ""))), set()).add(root_seg)
    for _ in range(len(workspace_crates)):
        changed = False
        for (crate, label), targets in tuple(reexports.items()):
            more = {
                deep for r in tuple(targets)
                for deep in reexports.get((r, label), ())
            }
            if not more <= targets:
                targets |= more
                changed = True
        if not changed:
            break

    def in_stub_crate(stub: dict, candidates: list[dict]) -> list[dict]:
        origin = str(stub.get("origin_file", ""))
        own = crate_of(origin) if origin.endswith(".rs") else None
        if own is None:
            return candidates
        roots = stub.get("_rust_use_roots") or ["crate"]
        label = str(stub.get("label", ""))
        named = {own if r == "crate" else r for r in roots} & workspace_crates
        allowed = set(named)
        for r in named:
            allowed |= reexports.get((r, label), set())

        def keep(crates: set[str]) -> list[dict]:
            return [
                candidate for candidate in candidates
                if not str(candidate.get("source_file", "")).endswith(".rs")
                or crate_of(str(candidate.get("source_file", ""))) in (crates | {None})
            ]

        scoped_named = keep(named)
        return scoped_named if len(scoped_named) == 1 else keep(allowed)

    real_by_label: dict[str, list[dict]] = {}       # exact-case type-like (all languages)
    real_by_label_ci: dict[str, list[dict]] = {}    # case-INSENSITIVE-language reals only
    func_by_label: dict[str, list[dict]] = {}       # top-level function defs (#1781)
    stubs: list[dict] = []

    for node in nodes:
        key = _node_label_key(node)
        if not key:
            continue
        if node.get("source_file"):
            if _is_type_like_definition(node):
                real_by_label.setdefault(key, []).append(node)
                if _lang_is_case_insensitive(node.get("source_file")):
                    real_by_label_ci.setdefault(
                        _node_label_key(node, fold=True), []).append(node)
            elif _is_top_level_function_definition(node):
                func_by_label.setdefault(key, []).append(node)
            continue
        stubs.append(node)

    stub_ids = {str(s.get("id")) for s in stubs if s.get("id")}
    stub_families: dict[str, set] = {}
    supertype_stub_ids: set[str] = set()
    _SUPERTYPE_RELATIONS = {"inherits", "implements", "extends"}
    for edge in edges:
        rel = edge.get("relation")
        for endpoint in ("source", "target"):
            nid = edge.get(endpoint)
            if nid in stub_ids:
                fam = _lang_family(edge.get("source_file"))
                if fam is not None:
                    stub_families.setdefault(str(nid), set()).add(fam)
                if endpoint == "target" and rel in _SUPERTYPE_RELATIONS:
                    supertype_stub_ids.add(str(nid))

    remap: dict[str, str] = {}
    for stub in stubs:
        stub_id = str(stub.get("id", ""))
        if not stub_id:
            continue
        candidates = in_stub_crate(stub, real_by_label.get(_node_label_key(stub), []))
        if len(candidates) != 1:
            candidates = in_stub_crate(
                stub, real_by_label_ci.get(_node_label_key(stub, fold=True), []))
        if len(candidates) != 1:
            fcands = in_stub_crate(stub, func_by_label.get(_node_label_key(stub), []))
            if len(fcands) == 1 and stub_id not in supertype_stub_ids:
                fams = stub_families.get(stub_id, set())
                cand_fam = _lang_family(fcands[0].get("source_file"))
                if not fams or cand_fam is None or cand_fam in fams:
                    candidates = fcands
        if len(candidates) != 1:
            continue
        target_id = candidates[0].get("id")
        if isinstance(target_id, str) and target_id and target_id != stub_id:
            remap[stub_id] = target_id

    if not remap:
        return

    by_id = {node.get("id"): node for node in nodes if node.get("id")}
    csharp_scoped_relations = {"inherits", "implements", "references", "imports"}

    def _names_own_builtin_base(edge: dict, stub_id: str, remapped_id: str) -> bool:
        if edge.get("relation") not in _SUPERTYPE_RELATIONS:
            return False
        edge_fam = _lang_family(edge.get("source_file"))
        if edge_fam is None:
            return False
        label = str(by_id.get(stub_id, {}).get("label", "")).strip()
        if _lang_is_case_insensitive(edge.get("source_file")):
            builtins = _LANGUAGE_BUILTIN_BASE_CLASSES_CI.get(edge_fam, frozenset())
            label = label.lower()
        else:
            builtins = _LANGUAGE_BUILTIN_BASE_CLASSES.get(edge_fam, frozenset())
        if label not in builtins:
            return False
        target_fam = _lang_family(by_id.get(remapped_id, {}).get("source_file"))
        return target_fam is not None and target_fam != edge_fam

    def _scoped_by_a_use(stub: dict) -> bool:
        roots = stub.get("_rust_use_roots") or []
        return "crate" in roots or bool(set(roots) & workspace_crates)

    rust_by_name_only = {
        stub_id for stub_id in remap
        if str(by_id.get(stub_id, {}).get("origin_file", "")).endswith(".rs")
        and not _scoped_by_a_use(by_id.get(stub_id, {}))
    } - {edge.get("target") for edge in edges if edge.get("_rust_use_name")}

    for edge in edges:
        is_csharp_scoped_edge = (
            str(edge.get("source_file", "")).endswith((".cs", ".razor", ".cshtml"))
            and edge.get("relation") in csharp_scoped_relations
        )
        source = edge.get("source")
        if source in remap:
            remapped_source = remap[str(source)]
            if not (
                is_csharp_scoped_edge
                and str(by_id.get(remapped_source, {}).get("source_file", "")).endswith(".cs")
            ):
                edge["source"] = remapped_source
        target = edge.get("target")
        if target in remap:
            remapped_target = remap[str(target)]
            if not (
                is_csharp_scoped_edge
                and str(by_id.get(remapped_target, {}).get("source_file", "")).endswith(".cs")
            ) and not _names_own_builtin_base(edge, str(target), remapped_target):
                edge["target"] = remapped_target
                if target in rust_by_name_only:
                    edge["confidence"] = "INFERRED"

    referenced = {x for e in edges for x in (e.get("source"), e.get("target"))}
    drop_ids = {stub_id for stub_id in remap if stub_id not in referenced}
    nodes[:] = [node for node in nodes if node.get("id") not in drop_ids]


def _remap_objc_field_tables(per_file: list, mapping: dict) -> None:
    """Rewrite objc_field_types["tables"] KEYS through an id remap (#3150)."""
    for result in per_file:
        ft = result.get("objc_field_types") if isinstance(result, dict) else None
        tables = ft.get("tables") if isinstance(ft, dict) else None
        if not isinstance(tables, dict):
            continue
        if any(k in mapping for k in tables):
            ft["tables"] = {mapping.get(k, k): v for k, v in tables.items()}


def _merge_swift_extensions(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Collapse cross-file Swift `extension Foo` nodes into the canonical `Foo`."""
    extension_nids: set[str] = set()
    extension_labels: dict[str, str] = {}
    for result in per_file:
        for ext in result.get("swift_extensions", []) or []:
            extension_nids.add(ext["nid"])
            extension_labels[ext["nid"]] = ext["label"]

    if not extension_nids:
        return

    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    label_to_canonical: dict[str, list[str]] = {}
    for n in all_nodes:
        if n.get("id") in extension_nids:
            continue
        label = n.get("label")
        if not label:
            continue
        if _lang_family(n.get("source_file")) != "native":
            continue
        if label in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        if not (n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n)):
            continue
        label_to_canonical.setdefault(label, []).append(n["id"])

    remap: dict[str, str] = {}
    for ext_nid in extension_nids:
        candidates = label_to_canonical.get(extension_labels[ext_nid], [])
        if len(candidates) != 1:
            continue
        canonical_nid = candidates[0]
        if canonical_nid != ext_nid:
            remap[ext_nid] = canonical_nid

    if not remap:
        return

    all_nodes[:] = [n for n in all_nodes if n.get("id") not in remap]

    def _key_of(e: dict, src: str, tgt: str) -> tuple:
        return (src, tgt, e.get("relation"), e.get("source_file"), e.get("source_location"))

    rewritten: list[dict] = []
    seen_keys: set[tuple] = set()
    for e in all_edges:
        src0, tgt0 = e.get("source"), e.get("target")
        src = remap.get(src0, src0)
        tgt = remap.get(tgt0, tgt0)
        if src == src0 and tgt == tgt0:
            seen_keys.add(_key_of(e, src0, tgt0))
            rewritten.append(e)
            continue
        if src == tgt:
            continue
        e["source"] = src
        e["target"] = tgt
        key = _key_of(e, src, tgt)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rewritten.append(e)
    all_edges[:] = rewritten


def _merge_csharp_partial_class_nodes(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
    paths: list[Path],
    root: Path,
) -> None:
    """Collapse C# `partial class Foo` halves split across files into ONE node (#2332)."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for n in all_nodes:
        if not str(n.get("source_file", "")).endswith(".cs"):
            continue
        if n.get("file_type") != "code":
            continue
        md = n.get("metadata") or {}
        if not md.get("is_partial") or md.get("is_nested_type"):
            continue
        label = n.get("label")
        if not label:
            continue
        groups.setdefault((str(md.get("namespace", "")), str(label)), []).append(n)

    if not any(len(members) >= 2 for members in groups.values()):
        return

    nid_to_path: dict[str, Path] = {}
    for result, path in zip(per_file, paths):
        for pn in result.get("nodes") or []:
            nid_to_path.setdefault(pn["id"], path)

    proj_exts = (".csproj", ".fsproj", ".vbproj")
    project_dirs: set[Path] = set()
    for p in paths:
        if p.suffix.lower() in proj_exts:
            try:
                project_dirs.add(p.resolve().parent)
            except OSError:
                pass
    try:
        stop = root.resolve()
    except OSError:
        stop = root
    dir_assembly: dict[Path, str] = {}

    def _assembly_of_dir(d: Path) -> str:
        chain: list[Path] = []
        key = ""
        while True:
            cached = dir_assembly.get(d)
            if cached is not None:
                key = cached
                break
            chain.append(d)
            if d in project_dirs:
                key = str(d)
                break
            try:
                has_project = any(
                    c.suffix.lower() in proj_exts for c in d.iterdir()
                )
            except OSError:
                has_project = False
            if has_project:
                key = str(d)
                break
            if d == stop or d.parent == d:
                break
            d = d.parent
        for c in chain:
            dir_assembly[c] = key
        return key

    def _assembly_of_node(nid: str) -> str:
        path = nid_to_path.get(nid)
        if path is None:
            return ""
        try:
            d = path.resolve().parent
        except OSError:
            return ""
        return _assembly_of_dir(d)

    remap: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        by_assembly: dict[str, list[dict]] = {}
        for n in members:
            by_assembly.setdefault(_assembly_of_node(n["id"]), []).append(n)
        for halves in by_assembly.values():
            if len(halves) < 2:
                continue
            halves.sort(key=lambda n: (
                str(n.get("source_file", "")),
                str(n.get("source_location", "")),
                str(n.get("id", "")),
            ))
            canonical_nid = halves[0]["id"]
            for other in halves[1:]:
                if other["id"] != canonical_nid:
                    remap[other["id"]] = canonical_nid

    if not remap:
        return

    all_nodes[:] = [n for n in all_nodes if n.get("id") not in remap]

    rewritten: list[dict] = []
    seen_keys: set[tuple] = set()
    for e in all_edges:
        src = remap.get(e.get("source"), e.get("source"))
        tgt = remap.get(e.get("target"), e.get("target"))
        if src == tgt:
            continue
        e["source"] = src
        e["target"] = tgt
        key = (src, tgt, e.get("relation"), e.get("source_file"), e.get("source_location"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rewritten.append(e)
    all_edges[:] = rewritten

    for result in per_file:
        for rc in result.get("raw_calls", []) or []:
            cn = rc.get("caller_nid")
            if cn in remap:
                rc["caller_nid"] = remap[cn]
