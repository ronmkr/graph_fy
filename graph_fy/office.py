"""Office and document format converters for graph_fy.

Handles PDF, DOCX, and XLSX parsing, markdown generation, structural node extraction,
and local sidecar file conversion with zip bomb / size cap protection.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
import zipfile
import sys
from pathlib import Path
from typing import Any

from graph_fy.paths import os_path as _os_path

# Dedicated caps for Office (zip-based) and PDF documents. Applied before any
# parser touches it.
_OFFICE_MAX_RAW_BYTES = 50 * 1024 * 1024            # 50 MiB on-disk
_OFFICE_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024  # 512 MiB total uncompressed
_OFFICE_MAX_COMPRESSION_RATIO = 200                 # uncompressed : compressed


def _get_detect_attr(name: str, default: Any) -> Any:
    mod = sys.modules.get("graph_fy.detect")
    return getattr(mod, name, default) if mod is not None else default


def _file_within_size_cap(path: Path, cap: int | None = None) -> bool:
    """True if *path* exists and its on-disk size is within *cap*."""
    if cap is None:
        cap = _get_detect_attr("_OFFICE_MAX_RAW_BYTES", _OFFICE_MAX_RAW_BYTES)
    try:
        return path.stat().st_size <= cap
    except OSError:
        return False


def _zip_within_caps(path: Path) -> bool:
    """Reject a zip-based office file that is a likely zip/XML bomb.

    Two layers, because the zip central-directory sizes are attacker-controlled:
    1. A cheap pre-filter on the declared sizes (on-disk cap, summed-uncompressed
       cap, compression ratio) that rejects an honest bomb without decompressing.
    2. An authoritative pass that stream-decompresses every member with a hard
       byte ceiling, so a member that under-declares its size in the central
       directory cannot expand past the cap undetected. Decompression is chunked
       and bounded, so checking a bomb never materializes more than the ceiling.
    """
    fn_cap = _get_detect_attr("_file_within_size_cap", _file_within_size_cap)
    if not fn_cap(path):
        return False
    max_decomp = _get_detect_attr("_OFFICE_MAX_DECOMPRESSED_BYTES", _OFFICE_MAX_DECOMPRESSED_BYTES)
    max_ratio = _get_detect_attr("_OFFICE_MAX_COMPRESSION_RATIO", _OFFICE_MAX_COMPRESSION_RATIO)
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            compressed = sum(i.compress_size for i in infos) or 1
            declared = sum(i.file_size for i in infos)
            if declared > max_decomp:
                return False
            if declared / compressed > max_ratio:
                return False
            total = 0
            for info in infos:
                with zf.open(info) as member:
                    while True:
                        chunk = member.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_decomp:
                            return False
    except (zipfile.BadZipFile, OSError, EOFError):
        return False
    return True



def pdf_to_markdown(path: Path) -> str:
    """Convert a .pdf file to markdown text locally without external APIs."""
    if not _file_within_size_cap(path):
        return ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        lines: list[str] = []

        # Extract metadata frontmatter if present
        meta = reader.metadata
        if meta:
            title = getattr(meta, "title", None) or meta.get("/Title")
            author = getattr(meta, "author", None) or meta.get("/Author")
            fm_items: list[str] = []
            if title and str(title).strip():
                fm_items.append(f"title: {str(title).strip()}")
            if author and str(author).strip():
                fm_items.append(f"author: {str(author).strip()}")
            if fm_items:
                lines.append("---")
                lines.extend(fm_items)
                lines.append("---")
                lines.append("")

        num_pages = len(reader.pages)
        for idx, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            if num_pages > 1:
                lines.append(f"## Page {idx + 1}")
                lines.append("")
            # Clean and normalize lines (standardize bullet points and code backticks)
            for page_line in text.splitlines():
                stripped = page_line.strip()
                if stripped.startswith(("•", "·", "–", "—")):
                    stripped = "- " + stripped.lstrip("•·–— ").strip()
                # Normalize PDF smart quotes / typographic backticks often generated for `
                stripped = stripped.replace("‘", "`").replace("’", "`")
                lines.append(stripped)
            lines.append("")

        result = "\n".join(lines).strip()
        if result:
            return result
    except Exception:
        pass

    # Fallback: regex scan for literal strings in raw PDF byte streams
    try:
        raw_bytes = path.read_bytes()
        text_matches = re.findall(rb"\(([^)\r\n]+)\)\s*Tj", raw_bytes)
        if text_matches:
            extracted = [m.decode("latin1", errors="ignore").strip() for m in text_matches]
            return "\n".join(t for t in extracted if t)
    except Exception:
        pass

    return ""


def extract_pdf_text(path: Path) -> str:
    """Extract text from a .pdf file as markdown."""
    return pdf_to_markdown(path)


def docx_to_markdown(path: Path) -> str:
    """Convert a .docx file to markdown text using python-docx."""
    if not _zip_within_caps(path):
        return ""
    try:
        from docx import Document
        from docx.oxml.ns import qn
        doc = Document(str(path))
        lines = []
        for para in doc.paragraphs:
            style = para.style.name if para.style else ""
            text = para.text.strip()
            if not text:
                lines.append("")
                continue
            if style.startswith("Heading 1"):
                lines.append(f"# {text}")
            elif style.startswith("Heading 2"):
                lines.append(f"## {text}")
            elif style.startswith("Heading 3"):
                lines.append(f"### {text}")
            elif style.startswith("List"):
                lines.append(f"- {text}")
            else:
                lines.append(text)
        # Tables
        for table in doc.tables:
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            header = "| " + " | ".join(rows[0]) + " |"
            sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
            lines.extend([header, sep])
            for row in rows[1:]:
                lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)
    except ImportError:
        return ""
    except Exception:
        return ""


def xlsx_to_markdown(path: Path) -> str:
    """Convert an .xlsx file to markdown text using openpyxl."""
    if not _zip_within_caps(path):
        return ""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        sections = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                if all(cell is None for cell in row):
                    continue
                rows.append([str(cell) if cell is not None else "" for cell in row])
            if not rows:
                continue
            sections.append(f"## Sheet: {sheet_name}")
            if len(rows) >= 1:
                header = "| " + " | ".join(rows[0]) + " |"
                sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
                sections.extend([header, sep])
                for row in rows[1:]:
                    sections.append("| " + " | ".join(row) + " |")
        wb.close()
        return "\n".join(sections)
    except ImportError:
        return ""
    except Exception:
        return ""


def xlsx_extract_structure(path: Path) -> dict:
    """Extract structural nodes (sheets, named tables, column headers) from an .xlsx file.

    Returns a nodes/edges dict compatible with the graph_fy extract pipeline.
    Used in addition to xlsx_to_markdown so Claude sees both structure and content.
    """
    def _nid(*parts: str) -> str:
        return re.sub(r"[^a-z0-9_]", "_", "_".join(p.lower() for p in parts).strip("_"))

    try:
        import openpyxl
    except ImportError:
        return {"nodes": [], "edges": []}

    try:
        wb = openpyxl.load_workbook(str(path), read_only=False, data_only=True)
    except Exception:
        return {"nodes": [], "edges": []}

    stem = re.sub(r"[^a-z0-9]", "_", path.stem.lower())
    str_path = str(path)
    file_nid = _nid(str_path)
    nodes: list[dict] = [{"id": file_nid, "label": path.name, "file_type": "document",
                           "source_file": str_path, "source_location": None}]
    edges: list[dict] = []
    seen: set[str] = {file_nid}

    def _add(nid: str, label: str) -> None:
        if nid not in seen:
            seen.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "document",
                           "source_file": str_path, "source_location": None})

    def _edge(src: str, tgt: str, relation: str) -> None:
        edges.append({"source": src, "target": tgt, "relation": relation,
                       "confidence": "EXTRACTED", "source_file": str_path,
                       "source_location": None, "weight": 1.0})

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        sheet_nid = _nid(stem, sheet_name)
        _add(sheet_nid, f"{sheet_name} (sheet)")
        _edge(file_nid, sheet_nid, "contains")

        # Named Excel Tables (ListObjects)
        if hasattr(ws, "tables"):
            for tbl in ws.tables.values():
                tbl_nid = _nid(stem, sheet_name, tbl.name)
                _add(tbl_nid, tbl.name)
                _edge(sheet_nid, tbl_nid, "contains")
                # Column headers from table header row
                ref = tbl.ref  # e.g. "A1:D10"
                if ref:
                    try:
                        from openpyxl.utils import range_boundaries
                        min_col, min_row, max_col, _ = range_boundaries(ref)
                        header_row = list(ws.iter_rows(min_row=min_row, max_row=min_row,
                                                       min_col=min_col, max_col=max_col,
                                                       values_only=True))
                        if header_row:
                            for col_name in header_row[0]:
                                if col_name:
                                    col_nid = _nid(stem, tbl.name, str(col_name))
                                    _add(col_nid, str(col_name))
                                    _edge(tbl_nid, col_nid, "contains")
                    except Exception:
                        pass
        else:
            # Fallback: first non-empty row as column headers
            for row in ws.iter_rows(max_row=1, values_only=True):
                for cell in row:
                    if cell:
                        col_nid = _nid(stem, sheet_name, str(cell))
                        _add(col_nid, str(cell))
                        _edge(sheet_nid, col_nid, "contains")
                break

    try:
        wb.close()
    except Exception:
        pass

    return {"nodes": nodes, "edges": edges}


def convert_office_file(path: Path, out_dir: Path, root: "Path | None" = None) -> Path | None:
    """Convert a .docx or .xlsx to a markdown sidecar in out_dir.

    Returns the path of the converted .md file, or None if conversion failed
    or the required library is not installed.
    """
    ext = path.suffix.lower()
    if ext == ".docx":
        fn_docx = _get_detect_attr("docx_to_markdown", docx_to_markdown)
        text = fn_docx(path)
    elif ext == ".xlsx":
        fn_xlsx = _get_detect_attr("xlsx_to_markdown", xlsx_to_markdown)
        text = fn_xlsx(path)
    else:
        return None

    if not text.strip():
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    if root is None:
        root = out_dir.parent.parent
    try:
        key = path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        key = str(path.resolve())
    normalized_path = unicodedata.normalize("NFC", key)
    name_hash = hashlib.sha256(normalized_path.encode()).hexdigest()[:8]
    out_path = out_dir / f"{path.stem}_{name_hash}.md"
    try:
        if out_path.exists() and os.stat(_os_path(out_path)).st_mtime >= os.stat(_os_path(path)).st_mtime:
            return out_path
    except OSError:
        if out_path.exists():
            return out_path
    out_path.write_text(
        f"<!-- converted from {path.name} -->\n\n{text}",
        encoding="utf-8",
    )
    return out_path


def count_words(path: Path) -> int:
    try:
        ext = path.suffix.lower()
        if ext == ".pdf":
            fn_pdf = _get_detect_attr("extract_pdf_text", extract_pdf_text)
            return len(fn_pdf(path).split())
        if ext == ".docx":
            fn_docx = _get_detect_attr("docx_to_markdown", docx_to_markdown)
            return len(fn_docx(path).split())
        if ext == ".xlsx":
            fn_xlsx = _get_detect_attr("xlsx_to_markdown", xlsx_to_markdown)
            return len(fn_xlsx(path).split())
        if not stat.S_ISREG(os.stat(_os_path(path)).st_mode):
            return 0
        with open(_os_path(path), encoding="utf-8", errors="ignore") as f:
            return len(f.read().split())
    except Exception:
        return 0
