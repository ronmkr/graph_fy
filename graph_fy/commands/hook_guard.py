from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

_SEARCH_NUDGE = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": (
            'MANDATORY: graph_fy_out/graph.json exists. You MUST run '
            '`graph_fy query "<question>"` before grepping raw files. Only grep '
            'after graph_fy has oriented you, or to modify/debug specific lines.'
        ),
    }
}, ensure_ascii=False, separators=(",", ":")) + "\n"

_READ_NUDGE = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": (
            'MANDATORY: graph_fy_out/graph.json exists. You MUST run graph_fy '
            'before reading source files. Use: `graph_fy query "<question>"` '
            '(scoped subgraph), `graph_fy explain "<concept>"`, or '
            '`graph_fy path "<A>" "<B>"`. Only read raw files after graph_fy has '
            'oriented you, or to modify/debug specific lines. This rule applies to '
            'subagents too — include it in every subagent prompt involving code '
            'exploration.'
        ),
    }
}, ensure_ascii=False, separators=(",", ":")) + "\n"

_READ_NUDGE_STALE = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": (
            'graph_fy_out/graph.json exists but may be STALE for this file (the file '
            'changed after the last build). Prefer `graph_fy query "<question>"` for '
            'orientation, and run `graph_fy update` to refresh the graph. Reading the '
            'file directly is fine.'
        ),
    }
}, ensure_ascii=False, separators=(",", ":")) + "\n"

_READ_DENY = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            'graph_fy strict mode: this project has a fresh knowledge graph that covers '
            'this file. Run `graph_fy query "<your question>"` (or `graph_fy explain` / '
            '`graph_fy path`) FIRST to orient yourself, then re-issue this Read — it '
            'will be allowed. This block fires at most once per session; reading raw '
            'files to modify or debug specific lines is fine after one query. Apply the '
            'same rule in any subagent prompt that explores code.'
        ),
    }
}, ensure_ascii=False, separators=(",", ":")) + "\n"

_HOOK_SOURCE_EXTS = (
    '.py', '.js', '.cjs', '.ts', '.tsx', '.jsx', '.astro', '.vue', '.svelte', '.go',
    '.rs', '.java', '.rb', '.c', '.h', '.cpp', '.hpp', '.cc', '.cs', '.kt',
    '.swift', '.php', '.scala', '.lua', '.sh', '.md', '.rst', '.txt', '.mdx',
)

_GEMINI_NUDGE_TEXT = (
    'graph_fy: knowledge graph at graph_fy_out/. For focused questions, run '
    '`graph_fy query "<question>"` (scoped subgraph, usually much smaller than '
    'GRAPH_REPORT.md) instead of grepping raw files. Read GRAPH_REPORT.md only '
    'for broad architecture context.'
)


def _hook_strict_enabled(flag: bool) -> bool:
    """Resolve strict mode: graph_fy_HOOK_STRICT env overrides the baked-in flag."""
    v = os.environ.get("graph_fy_HOOK_STRICT", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return flag


def _touch_query_stamp(graph_path: "Path") -> None:
    """Record that graph_fy oriented the agent recently, next to the queried graph."""
    try:
        from graph_fy.paths import write_text_atomic
        stamp = Path(graph_path).parent / "cache" / "last_query_stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(stamp, str(time.time()))
    except Exception:
        pass


def _query_stamp_fresh() -> bool:
    """True if a query/explain/path ran within graph_fy_HOOK_STRICT_TTL (default 1800s)."""
    from graph_fy.paths import out_path
    try:
        ttl = float(os.environ.get("graph_fy_HOOK_STRICT_TTL", "1800"))
        return (time.time() - out_path("cache", "last_query_stamp").stat().st_mtime) < ttl
    except Exception:
        return False


def _mark_session_denied(session_id: str) -> bool:
    """Atomically claim a one-time strict block for this session."""
    from graph_fy.paths import out_path
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id))[:64]
    if not sid:
        return False
    try:
        d = out_path("cache", "hook_sessions")
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / f"{sid}.denied"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        try:
            cutoff = time.time() - 86400
            for entry in os.scandir(d):
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    pass
        except OSError:
            pass
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


_SEARCH_COMMANDS = frozenset({
    "grep", "egrep", "fgrep", "zgrep", "rg", "ripgrep", "find", "fd", "ack", "ag",
})
_COMMAND_WRAPPERS = frozenset({
    "sudo", "command", "exec", "nohup", "time", "nice", "ionice", "env",
    "xargs", "timeout", "stdbuf", "doas",
})
_HEREDOC_OPEN_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _bash_invokes_search(cmd_str: str) -> bool:
    """Whether a Bash command actually RUNS a search tool (#3121)."""
    text = cmd_str
    m = _HEREDOC_OPEN_RE.search(text)
    while m:
        nl_idx = text.find("\n", m.end())
        if nl_idx == -1:
            break
        term = re.compile(r"^\s*" + re.escape(m.group(2)) + r"\s*$", re.MULTILINE)
        t = term.search(text, nl_idx + 1)
        if t is None:
            text = text[: nl_idx + 1]
            break
        text = text[: nl_idx + 1] + text[t.end():]
        m = _HEREDOC_OPEN_RE.search(text, nl_idx + 1)

    text = re.sub(r"'[^']*'", " ", text)
    text = re.sub(r'"[^"]*"', " ", text)

    for segment in re.split(r"[|;&\n]|\$\(|`|\(|\)|\{|\}", text):
        tokens = segment.split()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if "=" in tok.split("/")[-1] and not tok.startswith(("-", "/")):
                i += 1  # VAR=value prefix
                continue
            name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
            name = name[:-4] if name.endswith(".exe") else name
            if name in _COMMAND_WRAPPERS:
                i += 1
                while i < len(tokens) and tokens[i].startswith("-"):
                    i += 1
                continue
            if name in _SEARCH_COMMANDS:
                return True
            if name == "git" and any(
                t2 == "grep" for t2 in tokens[i + 1:i + 4] if not t2.startswith("-")
            ):
                return True
            break
    return False


def _is_cwd_relative(value: str) -> bool:
    """Whether value is anchored at current working directory."""
    pure = PureWindowsPath(value) if os.name == "nt" else PurePosixPath(value)
    return not pure.root and not pure.drive


def _target_is_indexed(file_path: str, root: "Path") -> bool:
    """Guard the strict deny: only block a read of a file the graph actually indexes."""
    from graph_fy.paths import out_path
    if not file_path:
        return True
    try:
        mp = out_path("manifest.json")
        st = mp.stat()
        if st.st_size > 2_000_000:
            return True
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not manifest:
            return True
        p = Path(file_path)
        rels = set()
        try:
            rels.add(p.resolve().relative_to(root).as_posix())
        except (ValueError, OSError, RuntimeError):
            pass
        rels.add(p.name)
        keys = {str(k).replace("\\", "/") for k in manifest}
        abskey = str(p).replace("\\", "/")
        return abskey in keys or any(r and (r in keys or any(k.endswith("/" + r) or k == r for k in keys)) for r in rels)
    except Exception:
        return True


def _run_hook_guard(kind: str, strict: bool = False) -> None:
    """Shell-agnostic PreToolUse guard (#522)."""
    from graph_fy.paths import graph_fy_OUT_NAME, out_path

    if kind == "gemini":
        payload = {"decision": "allow"}
        try:
            if out_path("graph.json").is_file():
                payload["additionalContext"] = _GEMINI_NUDGE_TEXT
        except Exception:
            pass
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return

    try:
        d = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except Exception:
        return
    if not isinstance(d, dict):
        return
    t = d.get("tool_input", d)
    if not isinstance(t, dict):
        return
    try:
        if kind == "search":
            cmd_str = str(t.get("command", "") or "")
            is_grep_tool = not cmd_str and bool(t.get("pattern"))
            is_bash_search = bool(cmd_str) and _bash_invokes_search(cmd_str)
            if (is_grep_tool or is_bash_search) and out_path("graph.json").is_file():
                sys.stdout.write(_SEARCH_NUDGE)
        elif kind == "read":
            vals = [str(t.get("file_path") or ""), str(t.get("pattern") or ""), str(t.get("path") or "")]
            j = " ".join(vals).lower().replace("\\", "/")
            tails = [
                "." + seg.rsplit(".", 1)[-1]
                for v in vals if v
                for seg in [v.lower().replace("\\", "/").rsplit("/", 1)[-1]]
                if "." in seg
            ]
            under_out = "graph_fy_out/" in j or (graph_fy_OUT_NAME.lower() + "/") in j
            if under_out or not any(tl in _HOOK_SOURCE_EXTS for tl in tails):
                return

            root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
            try:
                root = root.resolve()
            except (OSError, RuntimeError):
                pass
            path_vals = [str(t.get("file_path") or ""), str(t.get("path") or "")]
            explicit = [v for v in path_vals if v]
            if explicit:
                in_project = False
                for v in explicit:
                    p = Path(v)
                    if _is_cwd_relative(v):
                        in_project = True
                        break
                    try:
                        p.resolve().relative_to(root)
                        in_project = True
                        break
                    except (ValueError, OSError, RuntimeError):
                        continue
                if not in_project:
                    return

            try:
                gmtime = os.stat(str(out_path("graph.json"))).st_mtime
            except OSError:
                return

            stale = False
            fp = str(t.get("file_path") or "")
            if fp:
                try:
                    stale = os.stat(fp).st_mtime > gmtime
                except OSError:
                    stale = False
            try:
                if out_path("needs_update").exists():
                    stale = True
            except Exception:
                pass
            if stale:
                sys.stdout.write(_READ_NUDGE_STALE)
                return

            tool_name = d.get("tool_name")
            if _hook_strict_enabled(strict) and tool_name in (None, "Read") \
                    and not _query_stamp_fresh() \
                    and _target_is_indexed(fp, root) \
                    and _mark_session_denied(str(d.get("session_id") or "")):
                sys.stdout.write(_READ_DENY)
                return
            sys.stdout.write(_READ_NUDGE)
    except Exception:
        pass


def cmd_hook_check() -> int:
    """Codex Desktop PreToolUse no-op."""
    return 0


def cmd_hook_guard(argv: list[str]) -> int:
    """Shell-agnostic PreToolUse guard dispatcher."""
    kind = argv[2] if len(argv) > 2 else ""
    strict = "--strict" in argv[3:]
    _run_hook_guard(kind, strict=strict)
    return 0
