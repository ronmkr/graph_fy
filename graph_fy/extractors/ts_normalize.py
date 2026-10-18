"""TypeScript import type normalization.

tree-sitter-typescript misparses `import(...)` types used inside explicit call-
expression type arguments (e.g. `f<typeof import("mod")>()` or
`f<import("mod").Foo>()`) as binary comparison expressions (`<` and `>`).
The trailing `();` generates an ERROR node, leaving an open `binary_expression`
that absorbs subsequent declarations as anonymous `function_expression` or `class`
expressions, silently dropping them from extraction. Normalizing `import(...)`
within generic call type arguments `<...>(...)` to a valid type identifier of
identical byte length keeps AST parsing clean while preserving source offsets.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from typing import Any

_TS_IMPORT_CALL_RE = re.compile(
    rb"\bimport\s*\(\s*['\"][^'\"\r\n]+['\"]\s*\)"
)


def _ts_import_is_code(root: Any, start: int) -> bool:
    """Return whether an import-call match starts in executable source.

    Regex matching is only used to locate a literal specifier; comments,
    strings, regular expressions, and template text must never be fed into the
    structural masking pass.  A template substitution is executable again, so
    it is the one exception to the ``template_string`` guard.
    """
    node = root.descendant_for_byte_range(start, start + 1)
    if node is None:
        # A malformed tree can leave a byte range uncovered. Treat it as code
        # and let the structural pass decide whether it belongs to a type list;
        # never turn an uncertain lexical result into a crash.
        return True
    in_template_substitution = False
    while node is not None:
        if node.type == "comment" or node.type in ("string", "regex", "regex_pattern"):
            return False
        if node.type == "template_substitution":
            in_template_substitution = True
        elif node.type == "template_string" and not in_template_substitution:
            return False
        node = node.parent
    return True


def _ts_type_argument_ranges(root: Any, *, call_only: bool) -> list[tuple[int, int]]:
    """Collect byte ranges tree-sitter already parsed as type arguments."""
    ranges: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "type_arguments":
            parent = node.parent
            if not call_only or (
                parent is not None and parent.type in ("call_expression", "new_expression")
            ):
                ranges.append((node.start_byte, node.end_byte))
        stack.extend(node.children)
    return ranges


# Sorted type-argument ranges prepared for lookup: (starts, spans, prefix_max_end).
# See _ts_build_range_index. A real alias, not a string: quoting the right-hand
# side would make this a `str` value, which type checkers reject as a type.
_TsRangeIndex = tuple[list[int], list[tuple[int, int]], list[int]]


def _ts_ranges_containing(
    ranges: _TsRangeIndex, offset: int
) -> list[tuple[int, int]]:
    """Return the ranges in ``ranges`` that contain ``offset``.

    Scanning the full range list for every match made
    :func:`_normalize_ts_import_types` O(matches x ranges); on a large monorepo
    file with thousands of generic calls and thousands of ``import(...)`` types
    that quadratic blowup pinned a worker at 100% CPU with no output and no file
    I/O, so extraction never finished (#3359).

    Every range containing ``offset`` must start at or before it, so binary-search
    to the last such start and walk left. A range that ends before ``offset`` is
    not itself a hit but must not stop the walk -- a closed sibling can sit inside
    an enclosing range that does contain ``offset``. The index's running maximum of
    ends (``prefix_max_end``) gives the correct stop: once no range at or before
    this position reaches past ``offset``, none of the remaining ones can.
    """
    starts, spans, prefix_max_end = ranges
    hits: list[tuple[int, int]] = []
    index = bisect_right(starts, offset) - 1
    while index >= 0 and prefix_max_end[index] > offset:
        start, end = spans[index]
        if end > offset:
            hits.append((start, end))
        index -= 1
    hits.reverse()
    return hits


def _ts_build_range_index(root: Any, *, call_only: bool) -> _TsRangeIndex:
    """Index :func:`_ts_type_argument_ranges` for :func:`_ts_ranges_containing`.

    Returns the ranges sorted by start, split into a bare ``starts`` list for
    :func:`bisect_right`, plus a prefix-maximum of ``end`` values so a lookup can
    tell when no earlier range can still reach a given offset.
    """
    spans = sorted(_ts_type_argument_ranges(root, call_only=call_only))
    starts = [start for start, _ in spans]
    prefix_max_end: list[int] = []
    running = -1
    for _, end in spans:
        running = max(running, end)
        prefix_max_end.append(running)
    return starts, spans, prefix_max_end


def _ts_error_nodes(root: Any) -> list[Any]:
    """Return parser error nodes without depending on a grammar's error name."""
    errors: list[Any] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_error:
            errors.append(node)
        stack.extend(node.children)
    return errors


def _ts_mask_candidate_is_malformed(
    original_root: Any,
    masked_range: tuple[int, int],
    errors: list[Any],
) -> bool:
    """Tell whether a masked generic is backed by an actual parse failure.

    A valid runtime comparison can have the same token shape as a generic call
    after the import is replaced (``a < import("x") > (a)``).  Tree-sitter
    exposes the opening ``<`` as a binary operator in the original tree for
    both forms, so the structural second pass alone cannot distinguish them.
    Only repair that ambiguity when the original parser has an error adjacent
    to the candidate's closing angle; that is the signature of the known
    ``import(...)``-type grammar failure.  Valid comparisons, including ones
    nested in another call's arguments, remain byte-for-byte untouched.
    """
    start, end = masked_range
    opener = original_root.descendant_for_byte_range(start, start + 1)
    if opener is None or opener.type != "<":
        # A grammar that already gives us a structural type_arguments node is
        # safe to normalize; no binary/comparison ambiguity is present.
        return True
    if opener.parent is None or opener.parent.type != "binary_expression":
        return True

    # A malformed generic's ERROR starts at the closing `>` (or at the call
    # punctuation immediately following it). Keep this window deliberately
    # narrow so an unrelated syntax error elsewhere cannot authorize masking a
    # valid runtime comparison.
    for error in errors:
        if error.start_byte <= end + 2 and error.end_byte >= end - 1:
            return True
    return False


def _normalize_ts_import_types(source: bytes, *, tsx: bool = False) -> bytes | None:
    """Rewrite only syntactic TypeScript ``import(...)`` type arguments.

    The first implementation of #3154 used a ``<...>`` regular expression.
    In semicolon-less code that expression could span two comparison
    operators, so a *runtime* dynamic import was blanked before parsing.  A
    temporary, byte-preserving mask lets tree-sitter identify the actual
    ``type_arguments`` node without asking it to parse the known-invalid
    ``import(...)`` call-site form.  We then rewrite only placeholders inside
    call/new-expression type arguments.  Keeping every replacement the same
    byte length preserves source offsets and line locations.
    """
    raw_matches = list(_TS_IMPORT_CALL_RE.finditer(source))
    if not raw_matches:
        return None

    # tree-sitter is already a required TypeScript dependency for extraction.
    # If it cannot be loaded here, leave the source untouched; the normal AST
    # path will report its own dependency/parser error rather than applying an
    # unsafe textual guess.
    try:
        import tree_sitter_typescript as ts_typescript
        from tree_sitter import Language, Parser

        language_factory = (
            ts_typescript.language_tsx if tsx else ts_typescript.language_typescript
        )
        parser = Parser(Language(language_factory()))
        original_root = parser.parse(source).root_node
    except Exception:
        return None

    matches = [
        match for match in raw_matches
        if _ts_import_is_code(original_root, match.start())
    ]
    if not matches:
        return None

    # If the original tree already has a type_arguments node around a match,
    # its syntax is parseable as written. This includes the grammar's deliberate
    # comparison-vs-generic ambiguity (`a < b, import("...") > (d)`); retaining
    # that source is essential because it is a runtime expression, not a type.
    original_type_ranges = _ts_build_range_index(original_root, call_only=False)
    matches = [
        match for match in matches
        if not _ts_ranges_containing(original_type_ranges, match.start())
    ]
    if not matches:
        return None

    def placeholder(match: "re.Match[bytes]") -> bytes:
        # Keep CR/LF bytes intact. The first byte becomes an ordinary type
        # identifier and the remaining bytes are padding, so every downstream
        # byte offset remains identical to the user's source.
        matched = match.group(0)
        return b"T" + re.sub(rb"[^\r\n]", b" ", matched[1:])

    masked = bytearray(source)
    for match in matches:
        masked[match.start():match.end()] = placeholder(match)

    root = parser.parse(bytes(masked)).root_node

    # Only call/new-expression type arguments need this workaround. Ordinary
    # type annotations already parse ``import(...)`` correctly and should keep
    # their native AST shape. A range from an outer call's type_arguments also
    # covers nested generic arguments.
    type_argument_ranges = _ts_build_range_index(root, call_only=True)
    errors = _ts_error_nodes(original_root)

    # `type_argument_ranges` is an index tuple, so test the spans it holds --
    # the tuple itself is always truthy.
    if not type_argument_ranges[1]:
        return None

    norm = bytearray(source)
    changed = False
    for match in matches:
        containing_ranges = _ts_ranges_containing(type_argument_ranges, match.start())
        if containing_ranges and any(
            _ts_mask_candidate_is_malformed(original_root, candidate, errors)
            for candidate in containing_ranges
        ):
            norm[match.start():match.end()] = placeholder(match)
            changed = True
    return bytes(norm) if changed else None
