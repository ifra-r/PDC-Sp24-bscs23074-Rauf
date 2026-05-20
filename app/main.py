import os
import time
import asyncio
import logging
import httpx
from enum import Enum
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Circuit Breaker Implementation
# ---------------------------------------------------------------------------

class CircuitState(str, Enum):
    CLOSED   = "CLOSED"     # Normal operation — requests flow through
    OPEN     = "OPEN"       # Tripped — requests are blocked immediately
    HALF_OPEN= "HALF_OPEN"  # Trial mode — one probe request allowed


class CircuitBreaker:
    """
    A simple per-service circuit breaker.

    States:
      CLOSED    -> request passes through; on failure increment counter
      OPEN      -> immediately return fallback; re-check after recovery_timeout
      HALF_OPEN -> let one request through; success closes, failure re-opens
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        request_timeout: float = 5.0,
    ):
        self.name               = name
        self.failure_threshold  = failure_threshold
        self.recovery_timeout   = recovery_timeout
        self.request_timeout    = request_timeout

        self._state             = CircuitState.CLOSED
        self._failure_count     = 0
        self._last_failure_time: float = 0.0
        self._probe_in_flight   = False

    @property
    def state(self) -> CircuitState:
        # Auto-transition OPEN -> HALF_OPEN after recovery window
        if (
            self._state == CircuitState.OPEN
            and time.monotonic() - self._last_failure_time >= self.recovery_timeout
        ):
            logger.info("[%s] Recovery window elapsed — moving to HALF_OPEN", self.name)
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = False
        return self._state

    def _on_success(self):
        logger.info("[%s] Call succeeded — resetting to CLOSED", self.name)
        self._state         = CircuitState.CLOSED
        self._failure_count = 0
        self._probe_in_flight = False

    def _on_failure(self):
        self._failure_count    += 1
        self._last_failure_time = time.monotonic()
        logger.warning(
            "[%s] Call failed (count=%d / threshold=%d)",
            self.name, self._failure_count, self.failure_threshold,
        )
        if self._failure_count >= self.failure_threshold:
            logger.error("[%s] Threshold reached — tripping to OPEN", self.name)
            self._state = CircuitState.OPEN

    def reset(self):
        """Fully reset to CLOSED — used by the test helper endpoint."""
        self._state             = CircuitState.CLOSED
        self._failure_count     = 0
        self._last_failure_time = 0.0
        self._probe_in_flight   = False

    async def call(self, coro_factory, fallback):
        """
        Execute coro_factory() through the breaker.
        Returns fallback immediately if the circuit is OPEN.
        """
        current = self.state

        if current == CircuitState.OPEN:
            logger.warning("[%s] Circuit OPEN — returning fallback", self.name)
            return {"source": "fallback", "result": fallback, "circuit": "OPEN"}

        if current == CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                logger.warning("[%s] Probe already in-flight — returning fallback", self.name)
                return {"source": "fallback", "result": fallback, "circuit": "HALF_OPEN"}
            self._probe_in_flight = True
            logger.info("[%s] HALF_OPEN — sending probe request", self.name)

        try:
            result = await asyncio.wait_for(coro_factory(), timeout=self.request_timeout)
            self._on_success()
            return {"source": "live", "result": result, "circuit": self._state}
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error("[%s] Exception during call: %s", self.name, exc)
            self._on_failure()
            return {"source": "fallback", "result": fallback, "circuit": self._state}


# ---------------------------------------------------------------------------
# Instantiate the circuit breaker for the external LLM API
# ---------------------------------------------------------------------------

LLM_FALLBACK = (
    "The AI tutor is temporarily unavailable. "
    "Please try again in a moment or consult your study materials directly."
)

llm_breaker = CircuitBreaker(
    name="llm-api",
    failure_threshold=3,
    recovery_timeout=30.0,
    request_timeout=5.0,
)


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("StudySync backend starting up")
    yield
    logger.info("StudySync backend shutting down")


app = FastAPI(title="StudySync API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Read student ID from env (set in .env or export before running)
# ---------------------------------------------------------------------------

STUDENT_ID = os.getenv("STUDENT_ID", "BSCS23074")


# ---------------------------------------------------------------------------
# Middleware — inject X-Student-ID into every response
# ---------------------------------------------------------------------------

@app.middleware("http")
async def add_student_id_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Student-ID"] = STUDENT_ID
    return response


# ---------------------------------------------------------------------------
# Simulated in-memory store
# ---------------------------------------------------------------------------

documents: dict[str, dict] = {
    "doc-1": {"content": "Initial document content", "version": 1},
}

processed_webhook_events: set[str] = set()

user_subscriptions: dict[str, str] = {
    "user-123": "premium",
    "user-456": "free",
}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "circuit_breaker": llm_breaker.state}


# ---------------------------------------------------------------------------
# Problem 3 Fix: Circuit-broken LLM endpoint
# ---------------------------------------------------------------------------

async def _call_llm_api(prompt: str) -> str:
    """
    Simulates a call to an external LLM API.
    Set SIMULATE_LLM_DOWN=true to make it hang for 60 s (triggering the timeout).
    """
    if os.getenv("SIMULATE_LLM_DOWN", "false").lower() == "true":
        await asyncio.sleep(60)          # deliberately exceeds request_timeout

    await asyncio.sleep(0.1)
    return f"AI response for: {prompt}"


@app.post("/api/llm/ask")
async def ask_llm(request: Request):
    body   = await request.json()
    prompt = body.get("prompt", "")

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    result      = await llm_breaker.call(
        coro_factory=lambda: _call_llm_api(prompt),
        fallback=LLM_FALLBACK,
    )
    status_code = 200 if result["source"] == "live" else 503
    return JSONResponse(content=result, status_code=status_code)


@app.get("/api/llm/circuit-status")
async def circuit_status():
    return {
        "state":                    llm_breaker.state,
        "failure_count":            llm_breaker._failure_count,
        "recovery_timeout_seconds": llm_breaker.recovery_timeout,
    }


# ---------------------------------------------------------------------------
# TEST-ONLY helpers — let the test suite manipulate the breaker in-process
# ---------------------------------------------------------------------------

@app.post("/api/llm/test/force-failure")
async def test_force_failure():
    """
    Inject one failure into the circuit breaker.
    Call this `failure_threshold` times to trip it to OPEN.
    Only intended for the automated test suite — not a production endpoint.
    """
    llm_breaker._on_failure()
    return {
        "state":         llm_breaker._state,
        "failure_count": llm_breaker._failure_count,
    }


@app.post("/api/llm/test/reset")
async def test_reset_breaker():
    """Reset the circuit breaker to CLOSED. Used between test runs."""
    llm_breaker.reset()
    return {"state": llm_breaker._state}


@app.post("/api/llm/test/force-half-open")
async def test_force_half_open():
    """
    Push the last_failure_time back so the recovery window looks elapsed,
    causing the OPEN -> HALF_OPEN transition on the next state read.
    """
    llm_breaker._last_failure_time = (
        time.monotonic() - llm_breaker.recovery_timeout - 1
    )
    # Read .state to trigger the transition
    current = llm_breaker.state
    return {"state": current}


# ---------------------------------------------------------------------------
# Problem 1 Fix: Optimistic locking for shared documents
# ---------------------------------------------------------------------------

@app.get("/api/documents/{doc_id}")
async def get_document(doc_id: str):
    doc = documents.get(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"doc_id": doc_id, **doc}


@app.put("/api/documents/{doc_id}")
async def update_document(doc_id: str, request: Request):
    """
    Optimistic locking: the client must supply the version it last read.
    If the stored version has moved on, reject with 409 Conflict.
    """
    body           = await request.json()
    client_version = body.get("version")
    new_content    = body.get("content")

    if client_version is None or new_content is None:
        raise HTTPException(status_code=400, detail="version and content are required")

    doc = documents.get(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc["version"] != client_version:
        raise HTTPException(
            status_code=409,
            detail={
                "error":           "version_conflict",
                "message":         "Document was modified by another user. Please refresh and retry.",
                "current_version": doc["version"],
                "your_version":    client_version,
            },
        )

    documents[doc_id] = {"content": new_content, "version": client_version + 1}
    logger.info("Document %s updated to version %d", doc_id, client_version + 1)
    return {"doc_id": doc_id, **documents[doc_id]}


# ---------------------------------------------------------------------------
# Problem 2 Fix: Idempotent webhook handler
# ---------------------------------------------------------------------------

@app.post("/webhooks/clerk")
async def clerk_webhook(request: Request, svix_id: str = Header(None)):
    """
    Handles Clerk subscription-cancelled webhooks.

    Idempotency: every Clerk event carries a unique svix-id header.
    We store processed IDs so duplicate deliveries are harmless.
    """
    if not svix_id:
        raise HTTPException(status_code=400, detail="Missing svix-id header")

    if svix_id in processed_webhook_events:
        logger.info("Duplicate webhook %s — ignoring", svix_id)
        return {"status": "already_processed", "event_id": svix_id}

    body       = await request.json()
    event_type = body.get("type")
    data       = body.get("data", {})

    logger.info("Processing webhook %s of type %s", svix_id, event_type)

    if event_type == "user.subscription.cancelled":
        user_id = data.get("user_id")
        if user_id and user_id in user_subscriptions:
            user_subscriptions[user_id] = "free"
            logger.info("User %s downgraded to free tier", user_id)

    processed_webhook_events.add(svix_id)
    return {"status": "processed", "event_id": svix_id, "event_type": event_type}


@app.get("/api/users/{user_id}/subscription")
async def get_subscription(user_id: str):
    tier = user_subscriptions.get(user_id, "unknown")
    return {"user_id": user_id, "subscription": tier}