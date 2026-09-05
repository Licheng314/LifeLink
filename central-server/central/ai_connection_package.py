"""Build the portable, one-time Life Link AI MCP connection ZIP."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_SCHEMA = "life-link-ai-mcp-connection-package/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_SCRIPT = PROJECT_ROOT / "life-link-mcp" / "life_link_mcp.py"
SKILL_FILE = PROJECT_ROOT / ".codex" / "skills" / "life-link-ai-reader" / "SKILL.md"


@dataclass(frozen=True)
class ConnectionPackage:
    filename: str
    expires_at: str
    payload: bytes


def _required_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Life Link {label} is missing") from error


def mcp_config_template() -> dict[str, Any]:
    """Return the public MCP configuration skeleton bundled in every ZIP.

    This deliberately contains only local placeholders.  Pairing material is
    kept in pairing.json inside the one-time download and is never returned by
    the WebUI JSON preview.
    """
    return {
        "mcpServers": {
            "life-link": {
                "command": "<PYTHON_COMMAND>",
                "args": [
                    "<LIFE_LINK_MCP_DIR>/life_link_mcp.py", "serve", "--package-dir",
                    "<LIFE_LINK_MCP_DIR>",
                ],
            }
        }
    }


def create_connection_package(*, store: Any, external_origin: str) -> ConnectionPackage:
    """Create an in-memory ZIP; callers must return it only as an attachment."""
    script = _required_text(MCP_SCRIPT, "MCP Python program")
    skill = _required_text(SKILL_FILE, "AI Reader Skill")
    created = store.ai_readers.create_pairing(
        claim_url=f"{external_origin}/v1/ai-readers/pairings/claim",
        central_display_name="Life Link Central",
    )
    try:
        pairing = json.loads(created.text)
    except json.JSONDecodeError as error:  # defensive: created internally
        raise ValueError("Life Link pairing data is invalid") from error
    if not isinstance(pairing, dict):
        raise ValueError("Life Link pairing data is invalid")

    profile_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    manifest = {
        "schema_version": PACKAGE_SCHEMA,
        "product": "Life Link",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": created.expires_at,
        "central_instance_id": created.central_instance_id,
        "mcp_profile_id": profile_id,
        "entrypoint": "README.md",
        "pairing_file": "pairing.json",
        "skill_file": "life-link-ai-reader/SKILL.md",
        "program": "life_link_mcp.py",
        "program_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "transport": "stdio",
    }
    mcp_config = mcp_config_template()
    reader = {
        "schema_version": "life-link-mcp-reader/v1",
        "reader": {"type": "mcp-client", "instance_id": f"mcp:{profile_id}", "display_name": "AI Companion"},
    }
    readme = f"""# Life Link AI MCP connection package

This ZIP contains one-time private pairing material. It expires at `{created.expires_at}`.

## Let the AI finish the setup

1. Extract the entire ZIP to a private, stable local directory.
2. The AI host needs Python 3.13 or 3.14. Life Link's server-side Python does not provide Python to this AI host or container. This MCP program has no extra pip dependencies.
3. Read and follow `life-link-ai-reader/SKILL.md`. If the AI can install a Skill, install it; otherwise keep it as the connection's instructions.
4. Edit `reader.json` before the first read if a stable display name or a verified same-Windows-machine process binding is needed. Do not guess a process binding.
5. Copy `mcp-config.json` into the target AI's MCP configuration format. Replace `<PYTHON_COMMAND>` with `python` or `python3`, and replace every `<LIFE_LINK_MCP_DIR>` with this extracted directory's absolute path. If the AI has permission to manage local files, it should do this itself; otherwise it must guide the user through the change.
6. Start the MCP using stdio. First `lifelink_read_context` privately claims the one-time pairing and reads context. Do not print or share `pairing.json`, Tokens, cursors, or full context.

The connection uses the configured Life Link HTTPS address even when the AI and central service are on the same machine. After a successful first claim, `pairing.json` is deleted. If pairing expires or fails, create a new package from Life Link.
"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        def write_json(name: str, value: dict[str, Any]) -> None:
            archive.writestr(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        archive.writestr("README.md", readme)
        write_json("manifest.json", manifest)
        write_json("pairing.json", pairing)
        write_json("mcp-config.json", mcp_config)
        write_json("reader.json", reader)
        archive.writestr("life_link_mcp.py", script)
        archive.writestr("life-link-ai-reader/SKILL.md", skill)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return ConnectionPackage(f"LifeLink-AI-MCP-{stamp}.zip", created.expires_at, output.getvalue())
