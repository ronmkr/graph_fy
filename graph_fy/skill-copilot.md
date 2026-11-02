---
name: graph_fy
description: "Use for any question about a codebase, its architecture, file relationships, blast radius analysis, or pre-agent task context — especially when graph_fy_out/ exists. Essential for cold-start repo priming (`graph_fy prime`), pre-edit AST context briefings (`graph_fy context`), PR diff review (`graph_fy diff`), and checking codebase health (`graph_fy report --agent-ready`). Local AST and deterministic statistical graph algorithms with zero external calls."
---

# /graph_fy

Turn any folder of code, docs, or papers into a queryable knowledge graph with community detection, an honest audit trail (EXTRACTED/INFERRED/AMBIGUOUS), and 3 outputs: interactive HTML, GraphRAG-ready JSON, and GRAPH_REPORT.md.

## Usage

```
/graph_fy [path]                                      # full pipeline on path (defaults to .; HTML viz; add --obsidian for vault)
/graph_fy <url> [--branch <branch>]                   # clone repo then run full pipeline (multiple URLs cross-merge)
/graph_fy <path> --mode deep                          # thorough extraction, richer INFERRED edges
/graph_fy <path> --update                             # incremental - re-extract only new/changed files
/graph_fy <path> --directed                           # build directed graph (preserves source→target direction)
/graph_fy <path> --cluster-only                       # rerun clustering on existing graph
/graph_fy <path> --no-viz                             # skip visualization, emit report + JSON only
/graph_fy <path> --svg|--graphml|--wiki|--mcp         # export SVG, GraphML, crawlable wiki, or start MCP server
/graph_fy <path> --neo4j|--falkordb [--push <uri>]    # export Cypher or push directly to graph DB
/graph_fy <path> --watch                              # watch folder, auto-rebuild on code changes (no LLM needed)
/graph_fy prime                                       # deterministic ~150-token architectural cold-start header
/graph_fy context "<task>"                            # zero-LLM pre-agent analysis briefing (callers, callees, skeletons, blast radius)
/graph_fy diff [base_ref] [--fail-on-breaking]        # transitive diff blast radius & affected tests for PR review (CI gate)
/graph_fy report --agent-ready                        # audit codebase agent-readiness scorecard (god nodes, acyclicity)
/graph_fy impact <target>                             # predictive blast radius with degree-damped utility hub pruning
/graph_fy add <url> [--author "A"] [--contributor "C"] # fetch URL into ./raw, update graph
/graph_fy query "<question>" [--dfs] [--budget N]     # BFS/DFS traversal of graph.json
/graph_fy query "<question>" --skeleton|--rag-prompt  # AST skeleton elision or prompt-ready context block
/graph_fy path "NodeA" "NodeB"                        # shortest path between two concepts
/graph_fy explain "NodeName"                          # plain-language explanation of node and neighbors
```

## Execution Flow

- `--help` / `-h`: Print `## Usage` verbatim and stop.
- **Existing graph (`graph_fy_out/graph.json` exists in project root):**
  - Coding / debugging / refactoring: **run `graph_fy context "<task>"` (or call MCP `get_agent_context`) immediately.** Provides AST skeletons, callers, callees, and blast radius without reading raw files.
  - Exploration questions: **skip Steps 1–5 and jump straight to `## For /graph_fy query` (`graph_fy query "<question>"`).** Do not rebuild.
- If no path given, use `.` (current directory).
- GitHub URL (`http[s]://github.com/...`): run Step 0 first, then proceed with resolved local path.
- Follow steps in order.

### Step 0 - GitHub repos and multi-path merge (only if a URL or several paths)

Only when path is one or more `https://github.com/...` URLs, or several subfolders to merge. See `references/github-and-merge.md`. A plain local path skips this step.

### Step 1 - Ensure graph_fy is installed

```bash
PYTHON=""
GRAPHIFY_BIN=$(which graph_fy 2>/dev/null || which graphify 2>/dev/null)
if [ -z "$PYTHON" ] && command -v uv >/dev/null 2>&1; then
    _UV_PY=$(uv tool run --from graphifyy python -c "import sys; print(sys.executable)" 2>/dev/null)
    if [ -n "$_UV_PY" ]; then PYTHON="$_UV_PY"; fi
fi
if [ -z "$PYTHON" ] && [ -n "$GRAPHIFY_BIN" ]; then
    _SHEBANG=$(head -1 "$GRAPHIFY_BIN" | tr -d '#!')
    case "$_SHEBANG" in
        *[!a-zA-Z0-9/_.@-]*) ;;
        *) "$_SHEBANG" -c "import graph_fy" 2>/dev/null && PYTHON="$_SHEBANG" ;;
    esac
fi
if [ -z "$PYTHON" ]; then PYTHON="python3"; fi
if ! "$PYTHON" -c "import graph_fy" 2>/dev/null; then
    if command -v uv >/dev/null 2>&1; then
        uv tool install --upgrade graphifyy -q 2>&1 | tail -3
        _UV_PY=$(uv tool run --from graphifyy python -c "import sys; print(sys.executable)" 2>/dev/null)
        if [ -n "$_UV_PY" ]; then PYTHON="$_UV_PY"; fi
    else
        "$PYTHON" -m pip install graphifyy -q 2>/dev/null \
          || "$PYTHON" -m pip install graphifyy -q --break-system-packages 2>&1 | tail -3
    fi
fi
mkdir -p graph_fy_out
"$PYTHON" -c "import sys; open('graph_fy_out/.graphify_python', 'w', encoding='utf-8').write(sys.executable)"
"$PYTHON" -c "import os, sys; out_path = os.path.abspath('graph_fy_out/.graphify_root'); os.chdir(sys.stdin.readline().rstrip('\n')); open(out_path, 'w', encoding='utf-8').write(os.getcwd())" <<'GRAPHIFY_ROOT_EOF' || exit 1
INPUT_PATH
GRAPHIFY_ROOT_EOF
if [ -d .git ] || git rev-parse --git-dir >/dev/null 2>&1; then
    if [ ! -f .gitignore ] || ! grep -q "graph_fy_out" .gitignore 2>/dev/null; then
        printf "\n# graph_fy output\ngraph_fy_out/\ngraph_fy_out/\n.graphify_*\n.graphify_python\n.graphify_root\n" >> .gitignore
    fi
fi
```

If the import succeeds, print nothing and move straight to Step 2.

**In every subsequent bash block, replace `python3` with `$(cat graph_fy_out/.graphify_python)` to use the correct interpreter.**

### Step 2 - Detect files

```bash
$(cat graph_fy_out/.graphify_python) -c "
import json; from pathlib import Path; from graph_fy.detect import detect
res = detect(Path('INPUT_PATH'))
Path('graph_fy_out/.graphify_detect.json').write_text(json.dumps(res, ensure_ascii=False), encoding=\"utf-8\")
print(f'Detected {res[\"total_files\"]} files')
"
```

Replace INPUT_PATH with actual path. Silently summarize without printing raw JSON:
```
Corpus: X files · ~Y words (code: N, docs: N, papers: N, images: N, video: N)
```
- If `total_files` == 0: stop ("No supported files found in [path]").
- If `skipped_sensitive`: report count and names of skipped files.
- If `total_words` > 2,000,000 OR `total_files` > 500: warn user, show top 5 subdirectories by count, ask to narrow (suggest `--no-cluster` if all in root).
- Otherwise: proceed to Step 2.5 (if video files) or Step 3.

### Step 2.5 - Video and audio (only if video files detected)

Skip if zero video files detected. Transcribe video/audio to text first, then treat transcripts as doc files in Step 3.

### Step 3 - Extract entities and relationships

Track whether `--mode deep` was requested (`DEEP_MODE=true`).

> **graph_fy needs no API key. Never ask the user for one, and never block on one.** Code is extracted structurally (AST) with no LLM and no key at all. Semantic extraction (docs, papers, images only) uses Gemini if `GEMINI_API_KEY`/`GOOGLE_API_KEY` is set; otherwise the host agent itself acts as the LLM. No other API keys (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) are read. Proceed without prompting for keys.

Run Part A (AST) and Part B (semantic) in parallel when both exist. AST is deterministic and fast. Merge in Part C.

#### Part A - Structural extraction for code files

For any code files detected, run AST extraction:
```bash
$(cat graph_fy_out/.graphify_python) -c "
import json; from pathlib import Path; from graph_fy.extract import collect_files, extract
detect = json.loads(Path('graph_fy_out/.graphify_detect.json').read_text(encoding=\"utf-8\"))
code_files = [p for f in detect.get('files', {}).get('code', []) for p in (collect_files(Path(f)) if Path(f).is_dir() else [Path(f)])]
res = extract(code_files, cache_root=Path('INPUT_PATH')) if code_files else {'nodes':[], 'edges':[], 'input_tokens':0, 'output_tokens':0}
Path('graph_fy_out/.graphify_ast.json').write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding=\"utf-8\")
if code_files: print(f'AST: {len(res[\"nodes\"])} nodes, {len(res[\"edges\"])} edges')
"
```

#### Part B - Semantic extraction (parallel subagents)

**Fast path:** If zero docs/papers/images detected (code-only corpus), write empty semantic placeholder and proceed directly to Part C:
```bash
$(cat graph_fy_out/.graphify_python) -c "
import json, pathlib
pathlib.Path('graph_fy_out/.graphify_semantic.json').write_text(json.dumps({'nodes':[],'edges':[],'hyperedges':[],'input_tokens':0,'output_tokens':0}), encoding=\"utf-8\")
"
```

When non-code files exist, check cache first (`SPEC_PATH` is absolute path to `references/extraction-spec.md`):
```bash
$(cat graph_fy_out/.graphify_python) -c "
import json; from pathlib import Path; from graph_fy.cache import check_semantic_cache
detect = json.loads(Path('graph_fy_out/.graphify_detect.json').read_text(encoding=\"utf-8\"))
all_files = [f for cat in ('document', 'paper', 'image') for f in detect['files'].get(cat, [])]
cn, ce, ch, uncached = check_semantic_cache(all_files, root='INPUT_PATH', prompt_file='SPEC_PATH')
if cn or ce or ch:
    Path('graph_fy_out/.graphify_cached.json').write_text(json.dumps({'nodes': cn, 'edges': ce, 'hyperedges': ch}, ensure_ascii=False), encoding=\"utf-8\")
else:
    Path('graph_fy_out/.graphify_cached.json').unlink(missing_ok=True)
Path('graph_fy_out/.graphify_uncached.txt').write_text('\n'.join(uncached), encoding=\"utf-8\")
print(f'Cache: {len(all_files)-len(uncached)} files hit, {len(uncached)} files need extraction')
"
```

Split `graph_fy_out/.graphify_uncached.txt` into chunks of 20-25 files (1 image/chunk). If all cached, skip to Part C.

**Step B2 - Dispatch ALL subagents in a single message**

Call the Agent tool with `subagent_type="general-purpose"` in parallel (one call per chunk in a single message). Derive `CHUNK_PATH="$(pwd)/graph_fy_out/.graphify_chunk_0N.json"`.

Pass each subagent the prompt from `references/extraction-spec.md` with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, DEEP_MODE, and CHUNK_PATH substituted, writing JSON to CHUNK_PATH.

**Step B3 - Collect, cache, and merge**

Wait for subagents. Combine chunks, save cache, and merge with cached results:
```bash
$(cat graph_fy_out/.graphify_python) -c "
import json, glob; from pathlib import Path; from graph_fy.cache import save_semantic_cache
chunks = [json.loads(Path(c).read_text(encoding=\"utf-8\")) for c in sorted(glob.glob('graph_fy_out/.graphify_chunk_*.json'))]
new_n = [n for c in chunks for n in c.get('nodes', [])]
new_e = [e for c in chunks for e in c.get('edges', [])]
new_h = [h for c in chunks for h in c.get('hyperedges', [])]
tin, tout = sum(c.get('input_tokens', 0) for c in chunks), sum(c.get('output_tokens', 0) for c in chunks)
uncached = [l for l in Path('graph_fy_out/.graphify_uncached.txt').read_text(encoding=\"utf-8\").splitlines() if l] if Path('graph_fy_out/.graphify_uncached.txt').exists() else []
if new_n or new_e:
    save_semantic_cache(new_n, new_e, new_h, root='INPUT_PATH', allowed_source_files=uncached, prompt_file='SPEC_PATH')
cached = json.loads(Path('graph_fy_out/.graphify_cached.json').read_text(encoding=\"utf-8\")) if Path('graph_fy_out/.graphify_cached.json').exists() else {'nodes':[], 'edges':[], 'hyperedges':[]}
all_n = cached.get('nodes', []) + new_n
seen = set()
deduped_n = [n for n in all_n if not (n['id'] in seen or seen.add(n['id']))]
merged = {'nodes': deduped_n, 'edges': cached.get('edges', []) + new_e, 'hyperedges': cached.get('hyperedges', []) + new_h, 'input_tokens': tin, 'output_tokens': tout}
Path('graph_fy_out/.graphify_semantic.json').write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding=\"utf-8\")
print(f'Extraction complete: {len(deduped_n)} nodes, {len(merged[\"edges\"])} edges')
"
rm -f graph_fy_out/.graphify_cached.json graph_fy_out/.graphify_uncached.txt graph_fy_out/.graphify_semantic_new.json
```

#### Part C - Merge AST + semantic into final extraction

```bash
$(cat graph_fy_out/.graphify_python) -c "
import json; from pathlib import Path
ast = json.loads(Path('graph_fy_out/.graphify_ast.json').read_text(encoding=\"utf-8\"))
sem = json.loads(Path('graph_fy_out/.graphify_semantic.json').read_text(encoding=\"utf-8\"))
seen = {n['id'] for n in ast['nodes']}
merged_nodes = list(ast['nodes']) + [n for n in sem['nodes'] if n['id'] not in seen]
merged = {'nodes': merged_nodes, 'edges': ast['edges'] + sem['edges'], 'hyperedges': sem.get('hyperedges', []), 'input_tokens': sem.get('input_tokens', 0), 'output_tokens': sem.get('output_tokens', 0)}
Path('graph_fy_out/.graphify_extract.json').write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding=\"utf-8\")
print(f'Merged: {len(merged_nodes)} nodes, {len(merged[\"edges\"])} edges ({len(ast[\"nodes\"])} AST + {len(sem[\"nodes\"])} semantic)')
"
```

### Step 4 - Build graph, cluster, analyze, generate outputs

Replace `IS_DIRECTED` with `True` if `--directed` was given (preserves source→target direction), else `False`.

```bash
mkdir -p graph_fy_out
$(cat graph_fy_out/.graphify_python) -c "
import json; from pathlib import Path
from graph_fy.build import build_from_json
from graph_fy.cluster import cluster, score_all
from graph_fy.analyze import god_nodes, surprising_connections, suggest_questions
from graph_fy.report import generate
from graph_fy.export import to_json

extraction = json.loads(Path('graph_fy_out/.graphify_extract.json').read_text(encoding=\"utf-8\"))
detection  = json.loads(Path('graph_fy_out/.graphify_detect.json').read_text(encoding=\"utf-8\"))

G = build_from_json(extraction, root='INPUT_PATH', directed=IS_DIRECTED)
if G.number_of_nodes() == 0:
    print('ERROR: Graph is empty - extraction produced no nodes.'); raise SystemExit(1)
communities = cluster(G)
cohesion = score_all(G, communities)
gods, surprises = god_nodes(G), surprising_connections(G, communities)
labels = {cid: f'Community {cid}' for cid in communities}
questions = suggest_questions(G, communities, labels)

if not to_json(G, communities, 'graph_fy_out/graph.json'):
    print('ERROR: refused to shrink graph_fy_out/graph.json (#479). Re-run with --force.'); raise SystemExit(1)
report = generate(G, communities, cohesion, labels, gods, surprises, detection, {'input': extraction.get('input_tokens', 0), 'output': extraction.get('output_tokens', 0)}, 'INPUT_PATH', suggested_questions=questions)
Path('graph_fy_out/GRAPH_REPORT.md').write_text(report, encoding=\"utf-8\")
analysis = {'communities': {str(k): v for k, v in communities.items()}, 'cohesion': {str(k): v for k, v in cohesion.items()}, 'gods': gods, 'surprises': surprises, 'questions': questions}
Path('graph_fy_out/.graphify_analysis.json').write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding=\"utf-8\")
print(f'Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, {len(communities)} communities')
"
```

If this step prints `ERROR: Graph is empty`, stop and report failure to user. Replace INPUT_PATH with actual path.

### Step 4.5 - Graph health check (read-only integrity gate)

Non-destructive diagnostic before labeling (surfaces dangling/missing endpoints, collapsed edges, self-loops). Read-only; never aborts.

```bash
$(cat graph_fy_out/.graphify_python) -c "
import json; from pathlib import Path; from graph_fy.diagnostics import diagnose_extraction, format_diagnostic_report
extraction = json.loads(Path('graph_fy_out/.graphify_extract.json').read_text(encoding=\"utf-8\"))
summary = diagnose_extraction(extraction, directed=IS_DIRECTED, root='INPUT_PATH')
print(format_diagnostic_report(summary))
flags = [f'{summary[k]} {lbl}' for k, lbl in (('dangling_endpoint_edges', 'dangling'), ('missing_endpoint_edges', 'missing'), ('self_loop_edges', 'self-loops'), ('directed_same_endpoint_collapsed_edges', 'collapsed (dir)'), ('undirected_same_endpoint_collapsed_edges', 'collapsed (undir)')) if summary.get(k, 0)]
if flags: print('GRAPH HEALTH WARNING: ' + '; '.join(flags))
"
```

### Step 5 - Label communities

Read `graph_fy_out/.graphify_analysis.json`. Assign each community a 2-5 word name from its nodes, then regenerate report:

```bash
$(cat graph_fy_out/.graphify_python) -c "
import json; from pathlib import Path
from graph_fy.build import build_from_json
from graph_fy.cluster import score_all
from graph_fy.analyze import god_nodes, surprising_connections, suggest_questions
from graph_fy.report import generate
from graph_fy.export import to_json

extraction = json.loads(Path('graph_fy_out/.graphify_extract.json').read_text(encoding=\"utf-8\"))
detection  = json.loads(Path('graph_fy_out/.graphify_detect.json').read_text(encoding=\"utf-8\"))
analysis   = json.loads(Path('graph_fy_out/.graphify_analysis.json').read_text(encoding=\"utf-8\"))

G = build_from_json(extraction, root='INPUT_PATH', directed=IS_DIRECTED)
communities = {int(k): v for k, v in analysis['communities'].items()}
cohesion = {int(k): v for k, v in analysis['cohesion'].items()}
labels = LABELS_DICT
questions = suggest_questions(G, communities, labels)

report = generate(G, communities, cohesion, labels, analysis['gods'], analysis['surprises'], detection, {'input': extraction.get('input_tokens', 0), 'output': extraction.get('output_tokens', 0)}, 'INPUT_PATH', suggested_questions=questions)
Path('graph_fy_out/GRAPH_REPORT.md').write_text(report, encoding=\"utf-8\")
Path('graph_fy_out/.graphify_labels.json').write_text(json.dumps({str(k): v for k, v in labels.items()}, ensure_ascii=False), encoding=\"utf-8\")
if not to_json(G, communities, 'graph_fy_out/graph.json', community_labels=labels):
    print('ERROR: refused to shrink graph_fy_out/graph.json (#479). Re-run with --force.'); raise SystemExit(1)
print('Report updated with community labels')
"
```

Replace `LABELS_DICT` with actual dict (e.g. `{0: "Auth Flow", 1: "Data Pipeline"}`). Replace INPUT_PATH with actual path.

### Step 6 - Generate Obsidian vault (opt-in) + HTML

Generate HTML always (unless `--no-viz`). Generate Obsidian vault only if `--obsidian` requested (pass `--dir` if `--obsidian-dir` specified).

```bash
graph_fy export obsidian
```

```bash
graph_fy export html  # auto-aggregates to community view if graph > 5000 nodes
```

### Steps 6b-8 - Wiki, Neo4j, FalkorDB, SVG, GraphML, MCP, benchmark (only on their flags)

Only run when flag present (`--wiki`, `--neo4j`, `--falkordb`, `--svg`, `--graphml`, `--mcp`) or benchmark if `total_words` > 5,000. See `references/exports.md`.

---

### Step 9 - Save manifest, update cost tracker, clean up, and report

```bash
$(cat graph_fy_out/.graphify_python) -c "
import json
from pathlib import Path
from datetime import datetime, timezone
from graph_fy.detect import save_manifest
from graph_fy.cli import _stamped_manifest_files

detect = json.loads(Path('graph_fy_out/.graphify_detect.json').read_text(encoding=\"utf-8\"))
extract = json.loads(Path('graph_fy_out/.graphify_extract.json').read_text(encoding=\"utf-8\"))
_corpus = detect.get('all_files') or detect['files']
_manifest_files = _stamped_manifest_files(_corpus, extract, Path('INPUT_PATH'))
_cleared = {f for t, fl in detect['files'].items() if t in ('document', 'paper', 'image') for f in fl} - {f for fl in _manifest_files.values() for f in fl}
save_manifest(_manifest_files, root='INPUT_PATH', scan_corpus={f for fl in _corpus.values() for f in fl}, clear_semantic=_cleared or None)

in_tok, out_tok = extract.get('input_tokens', 0), extract.get('output_tokens', 0)
cost_path = Path('graph_fy_out/cost.json')
cost = json.loads(cost_path.read_text(encoding=\"utf-8\")) if cost_path.exists() else {'runs': [], 'total_input_tokens': 0, 'total_output_tokens': 0}
cost['runs'].append({'date': datetime.now(timezone.utc).isoformat(), 'input_tokens': in_tok, 'output_tokens': out_tok, 'files': detect.get('total_files', 0)})
cost['total_input_tokens'] += in_tok; cost['total_output_tokens'] += out_tok
cost_path.write_text(json.dumps(cost, indent=2, ensure_ascii=False), encoding=\"utf-8\")

print(f'This run: {in_tok:,} in / {out_tok:,} out tokens; All time: {cost[\"total_input_tokens\"]:,} in / {cost[\"total_output_tokens\"]:,} out ({len(cost[\"runs\"])} runs)')
"
rm -f graph_fy_out/.graphify_detect.json graph_fy_out/.graphify_extract.json graph_fy_out/.graphify_ast.json graph_fy_out/.graphify_semantic.json graph_fy_out/.graphify_analysis.json
find graph_fy_out -maxdepth 1 -name '.graphify_chunk_*.json' -delete 2>/dev/null
rm -f graph_fy_out/.needs_update 2>/dev/null || true
```

Replace INPUT_PATH with the actual path.

Present completion summary:
```
Graph complete. Outputs in PATH_TO_DIR/graph_fy_out/
  graph.html            - interactive graph, open in browser
  GRAPH_REPORT.md       - audit report
  graph.json            - raw graph data
  obsidian/             - Obsidian vault (if --obsidian was given)
```
Paste these three concise sections from `GRAPH_REPORT.md`:
- **God Nodes**
- **Surprising Connections**
- **Suggested Questions**

Offer to trace the most interesting cross-community question with `/graph_fy query "[question]"`.

---

## Interpreter guard for subcommands

Before running any subcommand below (`--update`, `--cluster-only`, `query`, `path`, `explain`, `add`), check that `.graphify_python` exists. If missing, re-resolve:

```bash
if [ ! -f graph_fy_out/.graphify_python ]; then
    GRAPHIFY_BIN=$(which graph_fy 2>/dev/null || which graphify 2>/dev/null)
    if [ -n "$GRAPHIFY_BIN" ]; then
        PYTHON=$(head -1 "$GRAPHIFY_BIN" | tr -d '#!')
        case "$PYTHON" in *[!a-zA-Z0-9/_.@-]*) PYTHON="python3" ;; esac
    else
        PYTHON="python3"
    fi
    mkdir -p graph_fy_out
    "$PYTHON" -c "import sys; open('graph_fy_out/.graphify_python', 'w', encoding='utf-8').write(sys.executable)"
fi
```

## For --update and --cluster-only

`--update` re-extracts changed files; `--cluster-only` reruns clustering. See `references/update.md`.

---

## For /graph_fy query

When `graph_fy_out/graph.json` already exists and the user asks a question about the corpus, answer from the graph rather than rebuilding it:

```bash
graph_fy query "<question>"
```

Before traversal, expand the question against the graph's own vocabulary so a wording mismatch does not collapse the answer to noise. If the `graph_fy query` CLI is unavailable, fall back to an inline NetworkX traversal of `graph_fy_out/graph.json`. Answer using only what the graph output contains, and quote `source_location` when citing a specific fact. For that vocab-expansion step, the BFS/DFS traversal modes, the `--budget` cap, the NetworkX fallback, `save-result` feedback, and the `/graph_fy path` and `/graph_fy explain` flows, see `references/query.md`.

---

## For /graph_fy add and --watch

Adding URLs into the corpus or auto-rebuilding on changes: see `references/add-watch.md`.

---

## For the commit hook and native CLAUDE.md integration

When installing the git rebuild hook or project CLAUDE.md integration, see `references/hooks.md`.

---

## Honesty Rules

- Never invent an edge. If unsure, use AMBIGUOUS.
- Never skip the corpus check warning.
- Always show token cost in the report.
- Never hide cohesion scores behind symbols - show the raw number.
- Never run HTML viz on a graph with more than 5,000 nodes without warning the user.

---

## Assistant Behavior: Ponytail & Caveman Integration

- **Ponytail (Anti-Overengineering)**:
  - 7-Rung Ladder: YAGNI -> Graph-First Reuse (`graph_fy query`) -> Native Stdlib -> Existing Dependency -> Single line -> Minimal working diff.
  - Fix root causes at shared definitions; inspect all callers before editing.
  - Zero unrequested abstractions, wrappers, or boilerplate.
  - Tag deliberate shortcuts with `# ponytail: <ceiling>, <upgrade path>`.
- **Caveman (Token Conservation)**:
  - Dense, high-signal technical bullets. Zero conversational filler, pleasantries, or apologies.
  - 100% technical fidelity: exact file paths, line ranges, symbol names, and verbatim diffs.
- **Frontier Coding Agent Optimization**:
  - Run `graph_fy context "<task>"` (or MCP `get_agent_context`) for zero-LLM pre-agent AST briefings (<1,200 tokens).
  - Use `--skeleton` AST elision and `--rag-prompt` PPR reranking.
  - Launch MCP server (`graph_fy --mcp`) for fine-grained AST inspection.
  - Navigate `graph_fy_out/wiki/index.md` hierarchically; never ingest whole raw source files into context.
