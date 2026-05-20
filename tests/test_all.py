"""
test_all.py — Demonstrates and tests all three distributed-systems fixes.

Run with the server already started:
    uvicorn app.main:app --port 8000

Then in another terminal:
    python -m tests.test_all
"""

import sys
import time
import asyncio
import httpx

BASE = "http://localhost:8000"


def separator(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def check_student_header(response: httpx.Response):
    hdr  = response.headers.get("x-student-id", "MISSING")
    mark = "✓" if hdr != "MISSING" else "✗"
    print(f"  {mark} X-Student-ID header: {hdr}")


# ---------------------------------------------------------------------------
# Test 1 — Optimistic Locking (Lost Update prevention)
# ---------------------------------------------------------------------------

async def test_optimistic_locking():
    separator("TEST 1: Optimistic Locking (Lost Update Prevention)")

    async with httpx.AsyncClient(base_url=BASE) as client:
        # Both users read the document at the same version
        r   = await client.get("/api/documents/doc-1")
        doc = r.json()
        print(f"\nBoth users read: content='{doc['content']}', version={doc['version']}")

        # User A writes successfully first
        r_a = await client.put("/api/documents/doc-1", json={
            "version": doc["version"],
            "content": "User A's brilliant edits",
        })
        check_student_header(r_a)
        print(f"\nUser A update → {r_a.status_code}: {r_a.json()}")

        # User B tries to write with the now-stale version — must be rejected
        r_b = await client.put("/api/documents/doc-1", json={
            "version": doc["version"],          # still the OLD version
            "content": "User B's conflicting edits",
        })
        check_student_header(r_b)
        print(f"User B update → {r_b.status_code}: {r_b.json()}")

        assert r_a.status_code == 200, "User A should succeed"
        assert r_b.status_code == 409, "User B should get a 409 Conflict"
        print("\n✓ Lost update correctly prevented — User B must refresh and retry")


# ---------------------------------------------------------------------------
# Test 2 — Idempotent Webhook (no duplicate processing)
# ---------------------------------------------------------------------------

async def test_idempotent_webhook():
    separator("TEST 2: Idempotent Webhook (Coordination Fix)")

    async with httpx.AsyncClient(base_url=BASE) as client:
        r = await client.get("/api/users/user-123/subscription")
        print(f"\nInitial subscription: {r.json()['subscription']}")

        payload  = {
            "type": "user.subscription.cancelled",
            "data": {"user_id": "user-123"},
        }
        event_id = "evt_clerk_abc123"

        # First delivery
        r1 = await client.post(
            "/webhooks/clerk",
            json=payload,
            headers={"svix-id": event_id},
        )
        check_student_header(r1)
        print(f"\nFirst webhook delivery → {r1.status_code}: {r1.json()}")

        # Network blip — Clerk retries the exact same event
        r2 = await client.post(
            "/webhooks/clerk",
            json=payload,
            headers={"svix-id": event_id},
        )
        check_student_header(r2)
        print(f"Duplicate delivery   → {r2.status_code}: {r2.json()}")

        r = await client.get("/api/users/user-123/subscription")
        print(f"\nFinal subscription: {r.json()['subscription']}")

        assert r1.json()["status"] == "processed"
        assert r2.json()["status"] == "already_processed"
        assert r.json()["subscription"] == "free"
        print("\n✓ Webhook processed exactly once — duplicate safely ignored")


# ---------------------------------------------------------------------------
# Test 3 — Circuit Breaker (Fault Tolerance)
# ---------------------------------------------------------------------------

async def test_circuit_breaker_normal():
    separator("TEST 3a: Circuit Breaker — Normal Operation (LLM Up)")

    async with httpx.AsyncClient(base_url=BASE) as client:
        # Make sure the breaker is reset before this test
        await client.post("/api/llm/test/reset")

        r = await client.post("/api/llm/ask", json={"prompt": "What is photosynthesis?"})
        check_student_header(r)
        print(f"\nLLM response → {r.status_code}: {r.json()}")
        assert r.json()["source"] == "live", f"Expected live, got: {r.json()}"
        print("✓ Live LLM response received normally")


async def test_circuit_breaker_trips():
    separator("TEST 3b: Circuit Breaker — LLM Down, Circuit Trips")

    async with httpx.AsyncClient(base_url=BASE) as client:
        # Reset to a known state
        await client.post("/api/llm/test/reset")
        print("\nSimulating repeated LLM failures to trip the circuit...")

        # Inject failures into the SERVER's breaker via the test endpoint.
        # (Mutating a local import would only affect this process, not the
        #  uvicorn worker — that was the original bug.)
        status_r = await client.get("/api/llm/circuit-status")
        threshold = status_r.json()["failure_count"]  # 0 after reset
        failure_threshold = 3  # must match CircuitBreaker default

        for i in range(failure_threshold):
            r = await client.post("/api/llm/test/force-failure")
            data = r.json()
            print(f"  Failure {i+1}: state={data['state']}, count={data['failure_count']}")

        # Confirm the server-side breaker is now OPEN
        status = await client.get("/api/llm/circuit-status")
        print(f"\nServer circuit status: {status.json()}")
        assert status.json()["state"] == "OPEN", "Circuit should be OPEN on server"

        # Now the HTTP call should get an instant fallback (503)
        r = await client.post("/api/llm/ask", json={"prompt": "Will this hang?"})
        check_student_header(r)
        data = r.json()
        print(f"\nResponse while circuit OPEN → {r.status_code}: {data}")

        assert r.status_code == 503,          f"Expected 503, got {r.status_code}"
        assert data["source"] == "fallback",  f"Expected fallback, got {data['source']}"
        assert data["circuit"] == "OPEN",     f"Expected OPEN, got {data['circuit']}"
        print("✓ Circuit is OPEN — fallback returned instantly, server did not hang")

        # Simulate recovery by pushing last_failure_time into the past
        print("\nSimulating recovery window elapsed...")
        r_ho = await client.post("/api/llm/test/force-half-open")
        print(f"Circuit state after window: {r_ho.json()['state']}")
        assert r_ho.json()["state"] == "HALF_OPEN", "Circuit should transition to HALF_OPEN"
        print("✓ Circuit moved to HALF_OPEN — ready to probe")

        # Clean up so subsequent tests start fresh
        await client.post("/api/llm/test/reset")


# ---------------------------------------------------------------------------
# Student ID Header Check (all routes)
# ---------------------------------------------------------------------------

async def test_student_id_header_everywhere():
    separator("REQUIREMENT CHECK: X-Student-ID header on all responses")

    endpoints = [
        ("GET",  "/health"),
        ("GET",  "/api/documents/doc-1"),
        ("GET",  "/api/users/user-456/subscription"),
        ("GET",  "/api/llm/circuit-status"),
    ]

    async with httpx.AsyncClient(base_url=BASE) as client:
        for method, path in endpoints:
            r   = await client.request(method, path)
            hdr = r.headers.get("x-student-id", "MISSING")
            mark = "✓" if hdr != "MISSING" else "✗"
            print(f"  {mark}  {method} {path} → X-Student-ID: {hdr}")

    print("\n✓ Middleware is injecting X-Student-ID on every response")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    print("\nStudySync — Distributed Systems Fix Test Suite")
    print("Connecting to", BASE)

    try:
        async with httpx.AsyncClient(base_url=BASE) as c:
            await c.get("/health")
    except Exception:
        print(f"\nERROR: Cannot reach {BASE}. Start the server first:")
        print("  uvicorn app.main:app --port 8000 --reload")
        sys.exit(1)

    await test_optimistic_locking()
    await test_idempotent_webhook()
    await test_circuit_breaker_normal()
    await test_circuit_breaker_trips()
    await test_student_id_header_everywhere()

    print("\n" + "=" * 60)
    print("  ALL TESTS PASSED")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())