"""Tests for CommonMark ATX Headings and Fenced Blocks (#64, #4790, #4787)."""
from pathlib import Path
from graph_fy.extractors.markdown import extract_markdown


def _write(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_markdown_atx_headings_commonmark(tmp_path: Path):
    md_file = _write(tmp_path, "doc.md", """\
# Introduction #

Some intro text.

## Setup ##
Setup details.

   ### Indented Heading ###

Details about indented heading.

    #### Four Spaces Indented Code Block ####

## ##

## Real Heading with # inside #

Final text.
""")

    result = extract_markdown(md_file)
    headings = [n for n in result["nodes"] if n.get("node_kind") == "heading"]
    heading_labels = [h["label"] for h in headings]

    assert "Introduction" in heading_labels
    assert "Setup" in heading_labels
    assert "Indented Heading" in heading_labels
    assert "Real Heading with # inside" in heading_labels

    # Trailing hashes should NOT be part of labels
    assert "Setup ##" not in heading_labels
    assert "Introduction #" not in heading_labels

    # 4 spaces is an indented code block, not heading
    assert not any("Four Spaces" in h for h in heading_labels)

    # Hash-only line should not mint heading
    assert not any(h == "##" for h in heading_labels)


def test_markdown_fenced_code_block_tilde_and_backtick(tmp_path: Path):
    md_file = _write(tmp_path, "fences.md", """\
# Title

~~~python
```
# Leaked Heading Inside Fence
```
~~~

## After Tilde Fence

Text after fence.
""")

    result = extract_markdown(md_file)
    headings = [n for n in result["nodes"] if n.get("node_kind") == "heading"]
    heading_labels = [h["label"] for h in headings]

    assert "Title" in heading_labels
    assert "After Tilde Fence" in heading_labels
    assert not any("Leaked Heading" in h for h in heading_labels)
