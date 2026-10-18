"""High-density AST skeletonization and code elision using tree-sitter.

Extracts signatures, types, docstrings, and contracts while eliding function/method
bodies to `...`, drastically compressing source code for LLM context windows (80-90% token savings).
"""
from __future__ import annotations

import importlib
from pathlib import Path
import re
from typing import Any

from tree_sitter import Language, Parser

_PARSER_CACHE: dict[str, tuple[Parser, str]] = {}

_LANG_MAPPING: dict[str, tuple[str, str | None, str]] = {
    ".py": ("tree_sitter_python", None, "python"),
    ".pyi": ("tree_sitter_python", None, "python"),
    ".js": ("tree_sitter_javascript", None, "javascript"),
    ".mjs": ("tree_sitter_javascript", None, "javascript"),
    ".cjs": ("tree_sitter_javascript", None, "javascript"),
    ".jsx": ("tree_sitter_javascript", None, "javascript"),
    ".ts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".mts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".cts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx", "tsx"),
    ".go": ("tree_sitter_go", None, "go"),
    ".rs": ("tree_sitter_rust", None, "rust"),
    ".java": ("tree_sitter_java", None, "java"),
    ".c": ("tree_sitter_c", None, "c"),
    ".h": ("tree_sitter_c", None, "c"),
    ".cpp": ("tree_sitter_cpp", None, "cpp"),
    ".hpp": ("tree_sitter_cpp", None, "cpp"),
    ".cc": ("tree_sitter_cpp", None, "cpp"),
    ".cxx": ("tree_sitter_cpp", None, "cpp"),
    ".cs": ("tree_sitter_c_sharp", None, "c_sharp"),
    ".sol": ("tree_sitter_solidity", None, "solidity"),
}

_BLOCK_NODE_TYPES = frozenset({
    "block",
    "statement_block",
    "compound_statement",
    "declaration_list",
})

_FUNC_NODE_TYPES = frozenset({
    "function_definition",
    "function_declaration",
    "method_definition",
    "method_declaration",
    "arrow_function",
    "generator_function_declaration",
    "function_item",
})

_CONTAINER_NODE_TYPES = frozenset({
    "class_definition",
    "class_declaration",
    "interface_declaration",
    "struct_item",
    "impl_item",
    "enum_declaration",
    "type_alias_declaration",
})


def get_parser_for_path(path: Path | str) -> tuple[Parser, str] | tuple[None, None]:
    """Retrieve or initialize a tree-sitter parser for a given file path based on suffix."""
    p_str = str(path).lower()
    suffix = Path(path).suffix.lower()
    if not suffix:
        if p_str.startswith("."):
            suffix = p_str
        elif f".{p_str}" in _LANG_MAPPING:
            suffix = f".{p_str}"
        elif p_str in _LANG_MAPPING:
            suffix = p_str
    if suffix in _PARSER_CACHE:
        return _PARSER_CACHE[suffix]

    config = _LANG_MAPPING.get(suffix)
    if not config:
        return None, None

    mod_name, fn_name, lang_name = config
    try:
        mod = importlib.import_module(mod_name)
        lang_fn = getattr(mod, fn_name) if fn_name else getattr(mod, "language")
        ts_lang = Language(lang_fn())
        parser = Parser(ts_lang)
        _PARSER_CACHE[suffix] = (parser, lang_name)
        return parser, lang_name
    except Exception:
        return None, None


def compact_signature(code: str, lang: str | None = None) -> str:
    """Produce a single-line signature without docstrings or bodies.

    e.g.:
      def login(user: str, pwd: str) -> bool: ...
      class Auth: ...
    """
    if not code or not code.strip():
        return ""

    clean_code = code.strip()

    parser = None
    lang_name = None
    if lang:
        ext = lang if lang.startswith(".") else f".{lang}"
        parser, lang_name = get_parser_for_path(ext)
    if parser is None:
        parser, lang_name = get_parser_for_path(".py")

    extended_blocks = _BLOCK_NODE_TYPES | frozenset({
        "class_body",
        "interface_body",
        "struct_field_declaration_list",
        "field_declaration_list",
        "enum_body",
    })

    if parser is not None:
        try:
            source_bytes = clean_code.encode("utf-8")
            tree = parser.parse(source_bytes)

            def find_decl(node: Any) -> Any:
                if node.type in _FUNC_NODE_TYPES or node.type in _CONTAINER_NODE_TYPES:
                    return node
                for child in node.children:
                    res = find_decl(child)
                    if res is not None:
                        return res
                return None

            target_node = find_decl(tree.root_node)
            if target_node is not None:
                parent = target_node.parent
                decl_root = target_node
                if parent is not None and parent.type in ("export_statement", "decorated_definition"):
                    decl_root = parent

                block_child = None
                for child in target_node.children:
                    if child.type in extended_blocks:
                        block_child = child
                        break

                if block_child is not None:
                    sig_bytes = source_bytes[decl_root.start_byte : block_child.start_byte].strip()
                    sig_text = sig_bytes.decode("utf-8", errors="replace")
                    sig_text = re.sub(r"\s+", " ", sig_text).strip()
                    sig_text = re.sub(r"\(\s+", "(", sig_text)
                    sig_text = re.sub(r"\s+\)", ")", sig_text)
                    sig_text = re.sub(r",\s*\)", ")", sig_text)
                    if sig_text.endswith(":"):
                        return f"{sig_text} ..."
                    elif sig_text.endswith("{"):
                        return f"{sig_text.rstrip('{').strip()} {{ ... }}"
                    else:
                        return f"{sig_text}: ..."
        except Exception:
            pass

    first_non_empty = [ln.strip() for ln in clean_code.splitlines() if ln.strip()][0]
    collapsed = re.sub(r"\s+", " ", first_non_empty).strip()
    collapsed = re.sub(r"\(\s+", "(", collapsed)
    collapsed = re.sub(r"\s+\)", ")", collapsed)
    collapsed = re.sub(r",\s*\)", ")", collapsed)
    if collapsed.endswith(":"):
        return f"{collapsed} ..."
    if ":" in collapsed:
        head = collapsed.split(":")[0] + ":"
        return f"{head} ..."
    return f"{collapsed} ..."


def skeletonize_code(
    source_bytes: bytes,
    parser: Parser,
    lang_name: str,
    *,
    keep_docstrings: bool = True,
    compact_docstrings: bool = False,
    token_budget: int | None = None,
) -> str:
    """Parse source bytes with tree-sitter and replace function bodies with `...`."""
    try:
        tree = parser.parse(source_bytes)
        root = tree.root_node
    except Exception:
        return source_bytes.decode("utf-8", errors="replace")

    elisions: list[tuple[int, int, bytes]] = []

    def visit(node: Any) -> None:
        if node.type in _FUNC_NODE_TYPES:
            body_node = None
            for child in node.children:
                if child.type in _BLOCK_NODE_TYPES:
                    body_node = child
                    break

            if body_node is not None:
                # Find docstring if in python
                if lang_name == "python" and keep_docstrings:
                    doc_node = None
                    first_stmt = body_node.children[0] if body_node.children else None
                    if first_stmt and first_stmt.type == "expression_statement":
                        for sc in first_stmt.children:
                            if sc.type == "string":
                                doc_node = first_stmt
                                break
                    if doc_node is not None:
                        doc_line_start = source_bytes.rfind(b"\n", 0, doc_node.start_byte)
                        indent_len = (doc_node.start_byte - doc_line_start - 1) if doc_line_start != -1 else 4
                        indent = b" " * max(indent_len, 4)

                        if compact_docstrings:
                            doc_bytes = source_bytes[doc_node.start_byte : doc_node.end_byte]
                            doc_str = doc_bytes.decode("utf-8", errors="replace").strip()
                            clean_quotes = doc_str.strip('"""').strip("'''").strip('"').strip("'").strip()
                            first_line = clean_quotes.splitlines()[0].strip() if clean_quotes else ""
                            if first_line and len(doc_str.splitlines()) > 1:
                                compact_repr = f'"""{first_line}"""'.encode("utf-8")
                                elisions.append((doc_node.start_byte, body_node.end_byte, compact_repr + b"\n" + indent + b"..."))
                            else:
                                elisions.append((doc_node.end_byte, body_node.end_byte, b"\n" + indent + b"..."))
                        else:
                            elisions.append((doc_node.end_byte, body_node.end_byte, b"\n" + indent + b"..."))
                        return
                    else:
                        elisions.append((body_node.start_byte, body_node.end_byte, b" ..."))
                        return
                else:
                    elisions.append((body_node.start_byte, body_node.end_byte, b" { ... }"))
                    return

        for child in node.children:
            visit(child)

    visit(root)

    elisions.sort(key=lambda x: x[0])
    out = []
    curr = 0
    for start, end, repl in elisions:
        if start > curr:
            out.append(source_bytes[curr:start])
        out.append(repl)
        curr = end
    if curr < len(source_bytes):
        out.append(source_bytes[curr:])

    res = b"".join(out).decode("utf-8", errors="replace")
    if token_budget is not None and token_budget > 0:
        max_chars = token_budget * 4
        if len(res) > max_chars:
            res = res[:max_chars].rstrip() + "\n// ... [skeleton truncated to token budget]"
    return res


def skeletonize_file(
    file_path: Path | str,
    *,
    keep_docstrings: bool = True,
    compact_docstrings: bool = False,
    token_budget: int | None = None,
) -> str:
    """Read a source file and return its skeletonized representation."""
    p = Path(file_path)
    if not p.is_file():
        return ""

    try:
        source_bytes = p.read_bytes()
    except OSError:
        return ""

    parser, lang_name = get_parser_for_path(p)
    if parser is None or lang_name is None:
        text = source_bytes.decode("utf-8", errors="replace")
        if token_budget is not None and token_budget > 0:
            max_chars = token_budget * 4
            if len(text) > max_chars:
                text = text[:max_chars].rstrip() + "\n// ... [truncated to token budget]"
        return text

    return skeletonize_code(
        source_bytes,
        parser,
        lang_name,
        keep_docstrings=keep_docstrings,
        compact_docstrings=compact_docstrings,
        token_budget=token_budget,
    )


def get_symbol_code_from_file(
    file_path: Path | str,
    line_number: int,
    *,
    skeletonize: bool = False,
    compact_docstrings: bool = False,
) -> str:
    """Extract code for the symbol enclosing `line_number` (1-indexed).

    If skeletonize is True, elides inner function bodies.
    """
    p = Path(file_path)
    if not p.is_file() or line_number <= 0:
        return ""

    try:
        source_bytes = p.read_bytes()
    except OSError:
        return ""

    def _slice_fallback() -> str:
        lines = source_bytes.decode("utf-8", errors="replace").splitlines()
        target_idx = line_number - 1
        return "\n".join(lines[max(0, target_idx - 5) : min(len(lines), target_idx + 25)])

    parser, lang_name = get_parser_for_path(p)
    if parser is None or lang_name is None:
        return _slice_fallback()

    try:
        tree = parser.parse(source_bytes)
    except Exception:
        return ""

    target_row = line_number - 1

    def find_enclosing_symbol(node: Any) -> Any:
        if node.start_point.row <= target_row <= node.end_point.row:
            # First search children for tighter enclosing function/class/interface
            for child in node.children:
                sub = find_enclosing_symbol(child)
                if sub is not None:
                    return sub
            # If no child is a symbol node, but this node itself is, return it
            if node.type in _FUNC_NODE_TYPES or node.type in _CONTAINER_NODE_TYPES:
                return node
        return None

    enclosing = find_enclosing_symbol(tree.root_node)
    if enclosing is None:
        return _slice_fallback()

    sym_bytes = source_bytes[enclosing.start_byte : enclosing.end_byte]
    if not skeletonize:
        return sym_bytes.decode("utf-8", errors="replace")

    return skeletonize_code(sym_bytes, parser, lang_name, compact_docstrings=compact_docstrings)
