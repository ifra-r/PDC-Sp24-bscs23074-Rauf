# Ifra Abdul Rauf - BSCS23074

## Building Resilient Distributed Systems

## What This Implements

This project fixes three classic distributed-systems bugs in a FastAPI + React
application (StudySync), as described in the assignment:

1. **Optimistic Locking** — prevents the Lost Update anomaly on shared documents
2. **Idempotent Webhook Handler** — ensures a dropped Clerk webhook never leaves
   a user permanently on the wrong subscription tier
3. **Circuit Breaker + Fallback** — stops a slow/dead LLM API from hanging the
   entire server for every user

Every API response includes a custom `X-Student-ID` header injected by FastAPI
middleware, as required by the submission rules.
 
---

## How to Run

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the server

```bash
uvicorn app.main:app --reload --port 8000
```

By default `STUDENT_ID` is read from an environment variable:

```bash
STUDENT_ID=i22XXXX uvicorn app.main:app --reload --port 8000
```

### 3. Run the test suite

In a separate terminal:

```bash
python -m tests.test_all
```

All tests should pass and print a summary at the end.

---

## Environment Variables

| Variable            | Default    | Purpose                                      |
|---------------------|------------|----------------------------------------------|
| `STUDENT_ID`        | `BSCSXXXXX`| Value injected into the `X-Student-ID` header |
| `SIMULATE_LLM_DOWN` | `false`    | Set to `true` to make the mock LLM hang 60 s |

---

## Manual API Exploration

With the server running, open `http://localhost:8000/docs` for the interactive
Swagger UI. You can test every endpoint from there.

### Quick curl examples

```bash
# Get a document (note the X-Student-ID header in the response)
curl -v http://localhost:8000/api/documents/doc-1

# Trigger the circuit breaker fallback
SIMULATE_LLM_DOWN=true uvicorn app.main:app --port 8001 &
curl -X POST http://localhost:8001/api/llm/ask \
     -H "Content-Type: application/json" \
     -d '{"prompt": "will this hang?"}'

# Fire a duplicate Clerk webhook
curl -X POST http://localhost:8000/webhooks/clerk \
     -H "Content-Type: application/json" \
     -H "svix-id: evt_test_001" \
     -d '{"type":"user.subscription.cancelled","data":{"user_id":"user-123"}}'
```
