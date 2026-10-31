#!/usr/bin/env python3
"""Stands in for the claude CLI in tests: records how it was called, then prints a valid JSON result."""

import json
import os
import sys
import time

sys.stdin.read()
call = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "mcp": os.environ.get("ENABLE_CLAUDEAI_MCP_SERVERS"),
    "pid": os.getpid(),
}
with open(os.environ["FAKE_CLAUDE_RECORD"], "a") as f:
    f.write(json.dumps(call) + "\n")
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
print(json.dumps({"is_error": False, "structured_output": {"chunks": []}}))
