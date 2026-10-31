#!/usr/bin/env python3
"""Stands in for the claude CLI in tests and browser checks: records how it was called, then prints a result that
matches the `--json-schema` argument (term, chunk batch or overview)."""

import json
import os
import re
import sys
import time

prompt = sys.stdin.read()
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
call = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "mcp": os.environ.get("ENABLE_CLAUDEAI_MCP_SERVERS"),
    "pid": os.getpid(),
}
if record := os.environ.get("FAKE_CLAUDE_RECORD"):
    with open(record, "a") as f:
        f.write(json.dumps(call) + "\n")
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
props = schema.get("properties", {})
if "meaning" in props:
    out = {"meaning": "Fake meaning.", "here": "Fake here."}
elif "chunks" in props:
    ids = re.findall(r'<chunk id="([^"]+)"', prompt)
    out = {
        "chunks": [
            {"id": i, "title": f"Fake title {i}", "what": "Fake what.", "why": "Fake why.", "concern": ""} for i in ids
        ]
    }
else:
    out = {"summary": "Fake summary.", "approach": "Fake approach.", "review_focus": ["Fake focus."]}
print(json.dumps({"is_error": False, "structured_output": out}))
