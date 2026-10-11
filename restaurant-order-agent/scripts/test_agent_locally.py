"""Talk to the agent directly from the terminal — no Twilio/ngrok needed.

Good for iterating on app/agent.py and app/tools.py in VS Code: run this,
type messages as if you were a customer, and watch which tools the agent
calls (DRY_RUN=true by default, so nothing is written to your Sheet yet).

    python scripts/test_agent_locally.py
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(level=logging.INFO)

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent

APP_NAME = "restaurant_order_agent"
# Tools now read the caller's number from the session's user_id (see
# app/tools.py::_caller_phone), so this must look like a real E.164 number —
# webhook.py sets user_id the same way, from Twilio's "From" field.
USER_ID = "+15555550123"
SESSION_ID = "test-session"


async def main():
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    print("Talking to the restaurant order agent. Type 'quit' to exit.\n")
    while True:
        message = input("You: ").strip()
        if message.lower() in {"quit", "exit"}:
            break
        content = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=content):
            if event.is_final_response() and event.content and event.content.parts:
                print(f"Agent: {event.content.parts[0].text}\n")


if __name__ == "__main__":
    asyncio.run(main())
