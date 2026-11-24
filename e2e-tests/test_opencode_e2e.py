import asyncio
import os
import sys
import unittest

# Ensure parent directory is in sys.path for genfoundry imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from genfoundry.base_agent import AgentOptions, MessageType
from genfoundry.opencode_agent import (
    OpenCodeAgent,
    find_opencode_cli,
    find_opencode_v2_service,
    probe_opencode_service,
)


def _opencode_available() -> bool:
    """Return True when a real CLI or a discoverable running service exists."""
    if find_opencode_cli():
        return True
    service = find_opencode_v2_service()
    return bool(service and probe_opencode_service(service["url"], service.get("password")))


class TestOpenCodeE2E(unittest.IsolatedAsyncioTestCase):
    """
    Opt-in smoke test for OpenCodeAgent using a real CLI/service.

    Set TERMMATE_OPENCODE_E2E=1 and either install/authenticate the `opencode`
    CLI or run an OpenCode service discoverable via service.json. The test is
    skipped otherwise so normal e2e runs never depend on a live provider.
    """

    async def test_real_opencode_interaction(self):
        if os.environ.get("TERMMATE_OPENCODE_E2E", "").strip().lower() not in (
            "1", "true", "yes"
        ):
            self.skipTest("set TERMMATE_OPENCODE_E2E=1 to run the OpenCode smoke test")
        if not _opencode_available():
            self.skipTest("opencode CLI/service not available")

        cli = find_opencode_cli()
        opts = AgentOptions(cwd=".", cli_path=cli)
        agent = OpenCodeAgent(options=opts)

        async def collect_reply():
            text = ""
            async for msg in agent.receive_messages():
                if msg.type == MessageType.TEXT.value:
                    text += msg.content or ""
                elif msg.type == MessageType.ERROR.value:
                    raise RuntimeError(f"OpenCode reported an error: {msg.content}")
                elif msg.type == MessageType.STOP.value:
                    return text
            return text

        try:
            await agent.connect()
            await agent.send_message("Reply with exactly: Hello from OpenCode E2E")
            response_text = await asyncio.wait_for(collect_reply(), timeout=180.0)
            self.assertIn("Hello from OpenCode E2E", response_text)
        finally:
            await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
