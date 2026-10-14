"""Thin wrapper around the `claude -p` CLI (headless Claude Code).

The key lives in CLAUDE_KEY (a Claude Code token, not an Anthropic API key).
We pass it to the CLI as CLAUDE_CODE_OAUTH_TOKEN. Every call has a timeout and
any failure returns None so callers can fall back to deterministic behaviour.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile


def claude_available() -> bool:
    return shutil.which("claude") is not None and bool(
        os.getenv("CLAUDE_KEY") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY"))


def run_claude(prompt: str, timeout: float = 60.0, model: str | None = None) -> str | None:
    if shutil.which("claude") is None:
        return None
    env = dict(os.environ)
    if env.get("CLAUDE_KEY") and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = env["CLAUDE_KEY"]
    cmd = ["claude", "-p", prompt, "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    try:
        # Run from an empty temp dir so the CLI doesn't pick up any repo context.
        with tempfile.TemporaryDirectory() as cwd:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def extract_json(text: str | None) -> dict | list | None:
    """Pull the first JSON object/array out of an LLM reply (handles ```json fences)."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidate = m.group(1) if m else text
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None
