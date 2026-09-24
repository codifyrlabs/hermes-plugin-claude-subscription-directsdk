"""Fake `claude` for sidecar tests. Modes (env CLAUDE_SUBSCRIPTION_DIRECTSDK_FAKE_MODE): text | tool | hang."""
import json
import os
import pathlib
import sys
import time

MODE = os.environ.get("CLAUDE_SUBSCRIPTION_DIRECTSDK_FAKE_MODE", "text")
DUMP = os.environ.get("CLAUDE_SUBSCRIPTION_DIRECTSDK_FAKE_ENV_DUMP")
PID_FILE = os.environ.get("CLAUDE_SUBSCRIPTION_DIRECTSDK_FAKE_PID_FILE")

if "--version" in sys.argv:
    print("2.1.263 (Claude Code)")
    sys.exit()
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": MODE != "logged_out", "subscriptionType": "max"}))
    sys.exit()
if sys.argv[1:3] == ["auth", "login"]:
    print("Opening browser to sign in…")
    print("If the browser didn't open, visit: \x1b[1mhttps://claude.ai/oauth/authorize?code=true&state=abc\x1b[0m")
    print("Paste code here if prompted > ", end="", flush=True)
    code = sys.stdin.readline().strip()
    sys.exit(0 if code == "good-code#state" else 1)
if DUMP:
    pathlib.Path(DUMP).write_text(json.dumps(dict(os.environ)))
if PID_FILE:
    pathlib.Path(PID_FILE).write_text(str(os.getpid()))
for line in sys.stdin:
    row = json.loads(line)
    if row.get("shouldQuery") is False:
        print(json.dumps({"type": "result", "num_turns": 0, "is_error": False}), flush=True)
if MODE == "logged_out":
    print(json.dumps({"type": "assistant", "error": "authentication_failed", "is_api_error_message": True,
                      "message": {"role": "assistant", "model": "<synthetic>",
                                  "content": [{"type": "text", "text": "Not logged in"}], "stop_reason": "stop_sequence"}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "result": "Not logged in"}), flush=True)
    sys.exit(1)
if MODE == "hang":
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": "started"}}}), flush=True)
    time.sleep(60)
if MODE == "tool":
    blocks = [{"type": "text", "text": "checking\n"},
              {"type": "tool_use", "id": "toolu_test", "name": "mcp__hermes__probe", "input": {"value": "x"}}]
else:
    blocks = [{"type": "text", "text": "hello"}]
for b in blocks:
    if b["type"] == "text":
        print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": b["text"]}}}), flush=True)
tool = len(blocks) > 1
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": blocks, "id": "msg_test",
                  "model": "claude-sonnet-5", "stop_reason": "tool_use" if tool else "end_turn"}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}), flush=True)
usage = {"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 11}
print(json.dumps({"type": "result", "num_turns": 2 if tool else 1, "subtype": "error_max_turns" if tool else "success",
                  "is_error": tool, "usage": usage, "total_cost_usd": 0.0125,
                  "modelUsage": {"claude-sonnet-5": {"costBasis": "list"}}}), flush=True)
sys.exit(1 if tool else 0)
