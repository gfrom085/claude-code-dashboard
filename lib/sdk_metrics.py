"""
SDK Metrics Helper — Write token usage from direct SDK API calls to the dated JSONL.
Each SDK script imports this and calls write_sdk_call() after a successful API call.

Rules:
- source MUST start with "sdk-" (e.g., "sdk-keypoint-gen", "sdk-call")
- session_id is always None for SDK calls
- session_type is always "sdk"
- Only write on successful calls (no zeros on 429/timeout)
- Atomic append via os.write() O_APPEND
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get(
    "CC_DASHBOARD_DATA_DIR",
    Path.home() / "active-projects" / "claude-code-dashboard" / "data"
))


def write_sdk_call(
    usage: dict,
    source: str,
    model: str,
    project: str | None = None,
    streaming: bool = False,
) -> None:
    """Write one JSONL line for an SDK API call. Atomic append.

    Args:
        usage: The usage dict from the API response (response.json()["usage"])
        source: Identifier prefixed with "sdk-" (e.g., "sdk-keypoint-gen")
        model: Model name from the response
        project: Optional project context
        streaming: Whether the call was streaming SSE
    """
    if not source.startswith("sdk-"):
        raise ValueError(f"source must start with 'sdk-', got '{source}'")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = DATA_DIR / f"token-metrics-{date_str}.jsonl"

    cache_creation = usage.get("cache_creation", {})
    line = json.dumps({
        "v": 2,
        "ts": time.time(),
        "source": source,
        "session_id": None,
        "session_type": "sdk",
        "project": project,
        "turn": None,
        "model": model,
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "cache_creation": usage.get("cache_creation_input_tokens", 0),
        "cache_5m": cache_creation.get("ephemeral_5m_input_tokens", 0)
            if isinstance(cache_creation, dict) else 0,
        "cache_1h": cache_creation.get("ephemeral_1h_input_tokens", 0)
            if isinstance(cache_creation, dict) else 0,
        "service_tier": usage.get("service_tier", "unknown"),
        "streaming": streaming,
    }, separators=(",", ":")) + "\n"

    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)

    # Also write to the real-time stream so SSE gauges can see SDK calls
    STREAM = "/tmp/token-metrics-stream.jsonl"
    try:
        sfd = os.open(STREAM, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(sfd, line.encode())
        finally:
            os.close(sfd)
    except OSError:
        pass  # stream absent = mitmproxy not running, not critical
