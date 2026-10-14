"""Load agent definitions from data files, never from code.

A manifest file is YAML or JSON and contains either one agent (a mapping)
or many (`agents: [...]`). Adding the 11th, 100th or 10,000th agent means
adding a file or a row, never editing Python.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from .schemas import AgentRegister

_FIELDS = set(AgentRegister.model_fields)


def parse_manifest(text: str, suffix: str = ".yaml") -> list[AgentRegister]:
    data = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
    if data is None:
        return []
    items = data.get("agents", [data]) if isinstance(data, dict) else data
    out: list[AgentRegister] = []
    for item in items:
        # Ignore extra keys (e.g. simulator hints) so one file can feed both board and swarm.
        out.append(AgentRegister(**{k: v for k, v in item.items() if k in _FIELDS}))
    return out


def load_manifest_dir(path: str | Path) -> tuple[list[AgentRegister], list[str]]:
    """Returns (agents, errors). A bad file is reported, not fatal."""
    agents: list[AgentRegister] = []
    errors: list[str] = []
    root = Path(path)
    if not root.exists():
        return agents, [f"manifest dir {root} does not exist"]
    for f in sorted(root.iterdir()):
        if f.suffix not in {".yaml", ".yml", ".json"}:
            continue
        try:
            agents.extend(parse_manifest(f.read_text(), ".json" if f.suffix == ".json" else ".yaml"))
        except (ValidationError, yaml.YAMLError, json.JSONDecodeError, AttributeError, TypeError) as e:
            errors.append(f"{f.name}: {e}")
    return agents, errors
