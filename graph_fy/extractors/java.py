"""Java and Groovy AST extractors."""
from __future__ import annotations

import re
from pathlib import Path

from graph_fy.extractors.base import _file_stem, _make_id, _read_text
from graph_fy.extractors.models import LanguageConfig


_JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS = frozenset({
    "class_declaration",
    "interface_declaration",
    "record_declaration",
    "method_declaration",
    "constructor_declaration",
})


def _java_type_parameters_in_scope(node, source: bytes) -> frozenset[str]:
    """Return Java type-parameter names visible from ``node``."""
    names: set[str] = set()
    scope = node
    while scope is not None:
        if scope.type in _JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS:
            params = scope.child_by_field_name("type_parameters")
            if params is not None:
                for param in params.children:
                    if param.type != "type_parameter":
                        continue
                    name_node = next(
                        (child for child in param.children if child.type == "type_identifier"),
                        None,
                    )
                    if name_node is not None:
                        names.add(_read_text(name_node, source))
        scope = scope.parent
    return frozenset(names)


_JAVA_BUILTIN_TYPES = frozenset({
    # java.lang — core
    "Object", "String", "CharSequence", "StringBuilder", "StringBuffer",
    "Number", "Byte", "Short", "Integer", "Long", "Float", "Double",
    "Boolean", "Character", "Void", "Class", "Enum", "Record", "Math",
    "System", "Thread", "Runnable", "Comparable", "Iterable", "Cloneable",
    "AutoCloseable", "Appendable", "Readable", "Process", "ProcessBuilder",
    "Runtime", "Package", "ThreadLocal", "InheritableThreadLocal",
    # java.lang — throwables
    "Throwable", "Exception", "RuntimeException", "Error",
    "IllegalArgumentException", "IllegalStateException", "NullPointerException",
    "IndexOutOfBoundsException", "ArrayIndexOutOfBoundsException",
    "ClassCastException", "NumberFormatException", "ArithmeticException",
    "UnsupportedOperationException", "InterruptedException",
    "CloneNotSupportedException", "SecurityException", "StackOverflowError",
    "OutOfMemoryError", "AssertionError",
    # java.util — collections & core
    "Collection", "List", "ArrayList", "LinkedList", "Vector", "Stack",
    "Set", "HashSet", "LinkedHashSet", "TreeSet", "SortedSet", "NavigableSet",
    "EnumSet", "Map", "HashMap", "LinkedHashMap", "TreeMap", "SortedMap",
    "NavigableMap", "Hashtable", "EnumMap", "Properties", "Queue", "Deque",
    "ArrayDeque", "PriorityQueue", "Iterator", "ListIterator", "Comparator",
    "Optional", "OptionalInt", "OptionalLong", "OptionalDouble", "Collections",
    "Arrays", "Objects", "Date", "Calendar", "Random", "UUID", "Scanner",
    "StringJoiner", "StringTokenizer", "BitSet", "Spliterator", "Locale",
    "NoSuchElementException", "ConcurrentModificationException",
    # java.util.stream
    "Stream", "IntStream", "LongStream", "DoubleStream", "Collector",
    "Collectors",
    # java.util.function
    "Function", "BiFunction", "Consumer", "BiConsumer", "Supplier",
    "Predicate", "BiPredicate", "UnaryOperator", "BinaryOperator",
    "IntFunction", "ToIntFunction", "ToLongFunction", "ToDoubleFunction",
    # java.util.concurrent
    "Callable", "Future", "CompletableFuture", "CompletionStage", "Executor",
    "ExecutorService", "Executors", "ScheduledExecutorService", "TimeUnit",
    "ConcurrentHashMap", "ConcurrentMap", "CopyOnWriteArrayList",
    "BlockingQueue", "CountDownLatch", "Semaphore", "CyclicBarrier",
    "AtomicInteger", "AtomicLong", "AtomicBoolean", "AtomicReference",
    # java.time
    "Instant", "Duration", "Period", "LocalDate", "LocalTime", "LocalDateTime",
    "ZonedDateTime", "OffsetDateTime", "ZoneId", "ZoneOffset", "DayOfWeek",
    "Month", "Year", "Clock", "DateTimeFormatter",
    # java.io / java.nio.file
    "IOException", "UncheckedIOException", "FileNotFoundException", "File",
    "InputStream", "OutputStream", "Reader", "Writer", "BufferedReader",
    "BufferedWriter", "InputStreamReader", "OutputStreamWriter", "FileReader",
    "FileWriter", "PrintStream", "PrintWriter", "ByteArrayInputStream",
    "ByteArrayOutputStream", "Serializable", "Closeable", "Path", "Paths",
    "Files",
    # java.math
    "BigDecimal", "BigInteger",
})


def _java_collect_type_refs(
    node,
    source: bytes,
    generic: bool,
    out: list[tuple[str, str]],
    skip: frozenset[str] | None = None,
    preserve_qualified: bool = False,
) -> None:
    """Walk a Java type expression; append (name, role) tuples."""
    if node is None:
        return
    if skip is None:
        skip = _java_type_parameters_in_scope(node, source)
    t = node.type
    if t in ("integral_type", "floating_point_type", "boolean_type", "void_type"):
        return
    if t == "type_identifier":
        name = _read_text(node, source)
        if name and name not in skip and name not in _JAVA_BUILTIN_TYPES:
            out.append((name, "generic_arg" if generic else "type"))
        return
    if t == "scoped_type_identifier":
        raw = _read_text(node, source)
        simple = raw.rsplit(".", 1)[-1]
        text = raw if preserve_qualified else raw.rsplit(".", 1)[-1]
        if text and simple not in _JAVA_BUILTIN_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        for c in node.children:
            if c.type in ("type_identifier", "scoped_type_identifier"):
                raw = _read_text(c, source)
                simple = raw.rsplit(".", 1)[-1]
                text = (
                    raw
                    if preserve_qualified and c.type == "scoped_type_identifier"
                    else simple
                )
                if (
                    text
                    and simple not in _JAVA_BUILTIN_TYPES
                    and (c.type == "scoped_type_identifier" or simple not in skip)
                ):
                    out.append((text, "generic_arg" if generic else "type"))
                break
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _java_collect_type_refs(
                            arg, source, True, out, skip, preserve_qualified
                        )
        return
    if t == "array_type":
        for c in node.children:
            if c.is_named:
                _java_collect_type_refs(
                    c, source, generic, out, skip, preserve_qualified
                )
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _java_collect_type_refs(
                    c, source, generic, out, skip, preserve_qualified
                )


def _java_receiver_type_name(type_node, source: bytes) -> str | None:
    """Return the concrete declared type usable for Java receiver resolution."""
    if type_node is None:
        return None
    t = type_node.type
    if t == "type_identifier":
        name = _read_text(type_node, source)
    elif t == "scoped_type_identifier":
        name = _read_text(type_node, source).rsplit(".", 1)[-1]
    elif t == "generic_type":
        base = next(
            (
                child
                for child in type_node.children
                if child.type in ("type_identifier", "scoped_type_identifier")
            ),
            None,
        )
        return _java_receiver_type_name(base, source)
    else:
        return None
    if (
        not name
        or name in _JAVA_BUILTIN_TYPES
        or name in _java_type_parameters_in_scope(type_node, source)
    ):
        return None
    return name


def _java_declarator_names(declaration_node, source: bytes) -> list[str]:
    names: list[str] = []
    for child in declaration_node.children:
        if child.type != "variable_declarator":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is not None:
            name = _read_text(name_node, source)
            if name:
                names.append(name)
    return names


def _java_lambda_parameters(
    lambda_node,
    source: bytes,
) -> list[tuple[str, str | None]]:
    parameters = lambda_node.child_by_field_name("parameters")
    if parameters is None:
        return []
    if parameters.type == "identifier":
        return [(_read_text(parameters, source), None)]
    if parameters.type == "inferred_parameters":
        return [
            (_read_text(child, source), None)
            for child in parameters.children
            if child.type == "identifier"
        ]
    bindings: list[tuple[str, str | None]] = []
    for parameter in parameters.children:
        if parameter.type not in ("formal_parameter", "spread_parameter"):
            continue
        name_node = parameter.child_by_field_name("name")
        if name_node is not None:
            bindings.append((
                _read_text(name_node, source),
                _java_receiver_type_name(
                    parameter.child_by_field_name("type"), source
                ),
            ))
    return bindings


def _java_method_receiver_types(
    method_node,
    source: bytes,
    field_types: dict[str, str],
) -> dict[str, str]:
    """Build the receiver type table visible to one Java method."""
    method_types: dict[str, str] = {}
    ambiguous: set[str] = set()

    def bind(name: str, type_name: str | None) -> None:
        if not name or not type_name or name in ambiguous:
            return
        previous = method_types.get(name)
        if previous is not None and previous != type_name:
            method_types.pop(name, None)
            ambiguous.add(name)
        else:
            method_types[name] = type_name

    params = method_node.child_by_field_name("parameters")
    if params is not None:
        for param in params.children:
            if param.type not in ("formal_parameter", "spread_parameter"):
                continue
            type_name = _java_receiver_type_name(
                param.child_by_field_name("type"), source
            )
            name_node = param.child_by_field_name("name")
            if name_node is not None:
                bind(_read_text(name_node, source), type_name)

    body = method_node.child_by_field_name("body")
    stack = list(body.children) if body is not None else []
    while stack:
        node = stack.pop()
        if node.type in (
            "class_declaration",
            "class_body",
            "interface_declaration",
            "record_declaration",
            "enum_declaration",
            "annotation_type_declaration",
        ):
            continue
        if node.type == "lambda_expression":
            for name, type_name in _java_lambda_parameters(node, source):
                if type_name is None or field_types.get(name) not in (None, type_name):
                    method_types.pop(name, None)
                    ambiguous.add(name)
                else:
                    bind(name, type_name)
        if node.type == "local_variable_declaration":
            type_name = _java_receiver_type_name(
                node.child_by_field_name("type"), source
            )
            for name in _java_declarator_names(node, source):
                if field_types.get(name) not in (None, type_name):
                    method_types.pop(name, None)
                    ambiguous.add(name)
                else:
                    bind(name, type_name)
        stack.extend(node.children)

    table = dict(field_types)
    table.update(method_types)
    for name in ambiguous:
        table.pop(name, None)
    table.update({f"this.{name}": type_name for name, type_name in field_types.items()})
    return table


def _java_annotation_nodes(declaration_node) -> list:
    """Return annotations from a Java declaration's `modifiers` child."""
    modifiers = None
    for child in declaration_node.children:
        if child.type == "modifiers":
            modifiers = child
            break
    if modifiers is None:
        return []
    return [
        child
        for child in modifiers.children
        if child.type in ("marker_annotation", "annotation")
    ]


def _java_annotation_names(declaration_node, source: bytes) -> list[tuple[str, str]]:
    """Collect ``(simple, raw)`` annotation names from a Java declaration's
    `modifiers` child. ``raw`` keeps the dotted qualifier of an inline-qualified
    annotation (``@org.pkg.Foo``); it equals ``simple`` when unqualified."""
    names: list[tuple[str, str]] = []
    for anno in _java_annotation_nodes(declaration_node):
        name_node = anno.child_by_field_name("name")
        if name_node is None:
            for sub in anno.children:
                if sub.type in ("identifier", "scoped_identifier", "type_identifier"):
                    name_node = sub
                    break
        if name_node is not None:
            raw = _read_text(name_node, source)
            text = raw.rsplit(".", 1)[-1]
            if text:
                names.append((text, raw))
    return names


def _java_annotation_class_literal_refs(
    declaration_node,
    source: bytes,
) -> list[str]:
    """Collect Java type names used as class literals in annotation arguments."""
    names: list[str] = []
    for anno in _java_annotation_nodes(declaration_node):
        arguments = anno.child_by_field_name("arguments")
        if arguments is None:
            continue
        stack = [arguments]
        while stack:
            current = stack.pop()
            if current.type == "class_literal":
                type_node = next(
                    (child for child in current.children if child.is_named),
                    None,
                )
                refs: list[tuple[str, str]] = []
                _java_collect_type_refs(
                    type_node, source, False, refs, preserve_qualified=True
                )
                names.extend(name for name, _role in refs)
                continue
            stack.extend(child for child in current.children if child.is_named)
    return names


def _java_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                     nodes: list, edges: list, seen_ids: set, function_bodies: list,
                     parent_class_nid: str | None, add_node_fn, add_edge_fn,
                     walk_fn) -> bool:
    """Handle enum_constant for Java. Returns True if handled."""
    if node.type == "enum_constant" and parent_class_nid:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return True
        const_name = _read_text(name_node, source)
        line = node.start_point[0] + 1
        const_nid = _make_id(parent_class_nid, const_name)
        add_node_fn(const_nid, const_name, line)
        add_edge_fn(parent_class_nid, const_nid, "case_of", line)
        for child in node.children:
            if child.type == "class_body":
                for member in child.children:
                    walk_fn(member, parent_class_nid=const_nid)
        return True
    return False



def _import_java(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    def _walk_scoped(n) -> str:
        parts: list[str] = []
        cur = n
        while cur:
            if cur.type == "scoped_identifier":
                name_node = cur.child_by_field_name("name")
                if name_node:
                    parts.append(_read_text(name_node, source))
                cur = cur.child_by_field_name("scope")
            elif cur.type == "identifier":
                parts.append(_read_text(cur, source))
                break
            else:
                break
        parts.reverse()
        return ".".join(parts)

    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            path_str = _walk_scoped(child)
            module_name = path_str.split(".")[-1].strip("*").strip(".") or (
                path_str.split(".")[-2] if len(path_str.split(".")) > 1 else path_str
            )
            if module_name:
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


_JAVA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_java",
    class_types=frozenset({
        "class_declaration", "interface_declaration", "record_declaration",
        "enum_declaration", "annotation_type_declaration",
    }),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"method_invocation", "object_creation_expression"}),
    call_function_field="name",
    call_accessor_node_types=frozenset(),
    function_boundary_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_handler=_import_java,
)

_GROOVY_CONFIG = LanguageConfig(
    ts_module="tree_sitter_groovy",
    class_types=frozenset({"class_declaration", "interface_declaration"}),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"method_invocation"}),
    call_function_field="name",
    call_accessor_node_types=frozenset(),
    function_boundary_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_handler=_import_java,
)


def extract_java(path: Path) -> dict:
    """Extract classes, interfaces, methods, constructors, and imports from a .java file."""
    from graph_fy.extractors.engine import _extract_generic
    result = _extract_generic(path, _JAVA_CONFIG)
    if "error" not in result:
        try:
            from graph_fy.extractors.config_keys import extract_config_keys_from_source
            source_text = path.read_text(encoding="utf-8", errors="ignore")
            extract_config_keys_from_source(path, source_text, result)
        except Exception:
            pass
    return result


def _is_spock_file(path: Path, ts_result: dict) -> bool:
    """Return True when the file contains Spock-style ``def "feature"()`` methods
    that tree-sitter-groovy cannot parse, detected by checking the raw source."""
    _SPOCK_FEATURE_RE = re.compile(r"""^\s*def\s+[\"']""", re.MULTILINE)
    try:
        return bool(_SPOCK_FEATURE_RE.search(path.read_text(errors="replace")))
    except OSError:
        return False


def _extract_spock_fallback(path: Path, ts_result: dict) -> dict:
    """Regex-based fallback for Spock spec files where tree-sitter-groovy cannot parse
    ``def "feature name"()`` methods. Merges import edges from the tree-sitter pass
    (which survive reliably) with class and feature-method nodes extracted via regex.
    """
    source = path.read_text(errors="replace")
    str_path = str(path)
    stem = _file_stem(path)

    file_node = next((n for n in ts_result.get("nodes", []) if n.get("label") == path.name), None)
    nodes: list[dict] = [file_node] if file_node else []
    edges: list[dict] = [e for e in ts_result.get("edges", []) if e.get("context") == "import"]
    seen_ids: set[str] = {n["id"] for n in nodes}

    def _add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def _add_edge(src: str, tgt: str, relation: str, line: int,
                  confidence: str = "EXTRACTED") -> None:
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    lines_text = source.splitlines()

    class_re = re.compile(r"^\s*(?:[\w@]+\s+)*class\s+(\w+)")
    feature_re = re.compile(r"""^\s*def\s+(?:\"([^\"]+)\"|'([^']+)')\s*\(""")
    plain_method_re = re.compile(r"""^\s*def\s+(\w+)\s*\(""")

    current_class_nid: str | None = None
    file_nid = _make_id(str_path)

    if file_nid not in seen_ids:
        _add_node(file_nid, path.name, 1)

    for lineno, line_text in enumerate(lines_text, start=1):
        cm = class_re.match(line_text)
        if cm:
            class_name = cm.group(1)
            class_nid = _make_id(stem, class_name)
            _add_node(class_nid, class_name, lineno)
            _add_edge(file_nid, class_nid, "contains", lineno)
            current_class_nid = class_nid
            continue

        if current_class_nid is None:
            continue

        fm = feature_re.match(line_text)
        if fm:
            method_name = fm.group(1) or fm.group(2)
            method_label = f'"{method_name}"'
            method_nid = _make_id(current_class_nid, method_name)
            _add_node(method_nid, method_label, lineno)
            _add_edge(current_class_nid, method_nid, "method", lineno)
            continue

        pm = plain_method_re.match(line_text)
        if pm:
            method_name = pm.group(1)
            if method_name not in ("if", "while", "for", "switch", "catch"):
                method_label = f".{method_name}()"
                method_nid = _make_id(current_class_nid, method_name)
                _add_node(method_nid, method_label, lineno)
                _add_edge(current_class_nid, method_nid, "method", lineno)

    return {"nodes": nodes, "edges": edges}


def extract_groovy(path: Path) -> dict:
    """Extract classes, methods, constructors, and imports from a .groovy/.gradle file.

    Falls back to a regex-based Spock extractor when tree-sitter-groovy cannot parse
    ``def "feature name"()`` methods (common in Spock specification classes).
    """
    from graph_fy.extractors.engine import _extract_generic
    result = _extract_generic(path, _GROOVY_CONFIG)
    if _is_spock_file(path, result):
        result = _extract_spock_fallback(path, result)
    return result

