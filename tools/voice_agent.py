"""Creates the ElevenLabs agent of `pixel/voice/agent_config.py` and adds ELEVENLABS_AGENT_ID to .env, or updates it
when that is set. Its client tools are created once and updated after that, found by their names.

uv run python -m tools.voice_agent

Needs ELEVENLABS_API_KEY in .env (permissions: ElevenAgents read and write). Never prints the key."""

import copy
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from pixel.voice.agent_config import AGENT, TOOLS

API = "https://api.elevenlabs.io/v1/convai"
ENV = Path(".env")


def _call(client: httpx.Client, method: str, path: str, body: dict | None = None) -> dict:
    response = client.request(method, f"{API}{path}", json=body)
    if response.is_error:
        sys.exit(f"ElevenLabs {response.status_code} on {method} {path}: {response.text[:300]}")
    return response.json() if response.content else {}


def main() -> None:
    load_dotenv(ENV)
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        sys.exit(f"Set ELEVENLABS_API_KEY in {ENV} first.")
    with httpx.Client(headers={"xi-api-key": key}, timeout=30) as client:
        existing = {
            t.get("tool_config", {}).get("name"): t["id"] for t in _call(client, "GET", "/tools").get("tools", [])
        }
        ids = []
        for tool in TOOLS:
            if tool_id := existing.get(tool["name"]):
                _call(client, "PATCH", f"/tools/{tool_id}", {"tool_config": tool})
            else:
                tool_id = _call(client, "POST", "/tools", {"tool_config": tool})["id"]
            print(f"{'Updated' if tool['name'] in existing else 'Created'} tool {tool['name']} ({tool_id}).")
            ids.append(tool_id)
        agent = copy.deepcopy(AGENT)
        agent["conversation_config"]["agent"]["prompt"]["tool_ids"] = ids
        if agent_id := os.environ.get("ELEVENLABS_AGENT_ID", ""):
            _call(client, "PATCH", f"/agents/{agent_id}", agent)
            print(f"Updated agent {agent_id}.")
            return
        agent_id = _call(client, "POST", "/agents/create", agent)["agent_id"]
    with ENV.open("a", encoding="utf-8") as env:
        env.write(f"\nELEVENLABS_AGENT_ID={agent_id}\n")
    print(f"Created agent {agent_id} and saved it to {ENV}. Restart the app.")


if __name__ == "__main__":
    main()
