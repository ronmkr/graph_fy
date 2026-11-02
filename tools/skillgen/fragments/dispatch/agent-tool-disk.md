**Step B2 - Dispatch ALL subagents in a single message**

Call the Agent tool with `subagent_type="general-purpose"` in parallel (one call per chunk in a single message). Derive `CHUNK_PATH="$(pwd)/graph_fy_out/.graphify_chunk_0N.json"`.

Pass each subagent the prompt from `references/extraction-spec.md` with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, DEEP_MODE, and CHUNK_PATH substituted, writing JSON to CHUNK_PATH.
