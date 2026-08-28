"""Manual smoke test: drive the derivation tools the way the builder agent will.

Not part of the pytest suite — it writes into a temp data dir and prints a
transcript, so it is for eyeballing the tool contract end to end without
spending a model call.

    python3 backend/tests/smoke_derivation.py

What it proves: open -> write in waves -> verify catches a wrong step and says
which one, and the whole document renders. If this passes and the pane still
looks wrong, the bug is in the frontend, not the backend.
"""

import asyncio
import json
import os
import tempfile


def main():
    from backend import derivations as D

    D.USERS_DIR = tempfile.mkdtemp()

    import claude_agent_sdk as sdk

    import backend.tools as T

    email = "learner@example.com"
    udir = os.path.join(D.USERS_DIR, email)
    os.makedirs(udir, exist_ok=True)

    class FakeEpisodic:
        def __getattr__(self, name):
            return lambda *a, **k: None

    # Capture the decorated handlers as the factory registers them; the SDK
    # server object does not expose them for direct calls.
    captured = {}
    original = sdk.create_sdk_mcp_server

    def spy(name, version, tools):
        for t in tools:
            captured[t.name] = t.handler
        return original(name=name, version=version, tools=tools)

    T.create_sdk_mcp_server = spy
    try:
        T.create_learning_tools(
            udir, FakeEpisodic(), None,
            {"chat_session_id": "c1", "builder_session_id": "b1",
             "title": "t", "concept_id": "k"},
        )
    finally:
        T.create_sdk_mcp_server = original

    def text(result):
        return result["content"][0]["text"]

    async def run():
        r = await captured["derivation_open"](
            {"title": "Logistic gradient", "concept_id": "logistic_regression"})
        doc_id = json.loads(text(r))["doc_id"]
        print(f"open        -> {doc_id}")

        r = await captured["derivation_write"]({"doc_id": doc_id, "blocks": [
            {"kind": "text", "content": "The gradient is the error times the input."},
        ]})
        print(f"write #1    -> {text(r)}")

        r = await captured["derivation_write"]({"doc_id": doc_id, "blocks": [
            {"kind": "derivation", "title": "Steps", "steps": [
                {"expr": "(a-b)^2", "reason": "start"},
                {"expr": "a^2-2ab+b^2", "reason": "expand the square"},
                {"expr": "a^2+b^2", "reason": "DELIBERATELY WRONG"},
            ]},
        ]})
        print(f"write #2    -> {text(r)}")

        doc = D.get_doc(email, doc_id)
        block_id = next(b["id"] for b in doc["blocks"] if b["kind"] == "derivation")
        r = await captured["derivation_verify"](
            {"doc_id": doc_id, "block_id": block_id})
        report = json.loads(text(r))
        print(f"verify      -> {json.dumps(report, indent=2)}")

        assert report["wrong"], "verification failed to catch the wrong step"
        assert report["verified"] >= 1, "verification failed to confirm a good step"

        doc = D.get_doc(email, doc_id)
        print(f"\nfinal doc   -> {len(doc['blocks'])} blocks, "
              f"steps: {[s['verified'] for s in doc['blocks'][1]['steps']]}")
        print("\nOK — wrong algebra was caught and named.")

    asyncio.run(run())


if __name__ == "__main__":
    main()
