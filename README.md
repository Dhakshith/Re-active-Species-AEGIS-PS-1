# 🛡️ AEGIS-CONTROL: Out-of-Band Governance Control Plane & Live NVIDIA NIM Agent Fleet

**AEGIS-CONTROL** is a 100% domain-agnostic Out-of-Band Security & Resilience Control Plane (`aegis_core/`) paired with a Plug-and-Play Developer SDK (`aegis_sdk/`), a live NVIDIA NIM Multi-Agent Corporate Treasury & SWIFT Settlement Fleet (`examples/treasury_case_study/`), and a real-time Streamlit Command Center (`ui/`).

---

## 🔍 Honest "Live vs. Mocked / Simulated" Breakdown

| Subsystem / Layer | Live or Simulated? | Implementation Details |
| :--- | :--- | :--- |
| **Agent Reasoning (`NimBrain`)** | **100% Live HTTP (`integrate.api.nvidia.com`)** | Real HTTPS `POST /v1/chat/completions` (`temperature=0.2`, `max_tokens=400`) outputting JSON action decisions (`CALL_TOOL`, `SPAWN_CHILD`, `SEND_A2A`, `REQUEST_HUMAN`, `COMPLETE`). |
| **Cryptographic Passports (`C1_IAM_Engine`)** | **100% Live Cryptography** | Real `cryptography` Ed25519 keypair generated at boot; real `PyJWT` (`EdDSA`) signing, $O(1)$ JTI revocation set, and strict parent $\rightarrow$ child scope/budget attenuation. |
| **HTTP Proxy & Auth (`aegis_core/server.py`)** | **100% Live FastAPI + ASGI HTTP** | Real `Authorization: Bearer <Ed25519 JWT>` extraction and verification via `Depends(verify_bearer_token)` on every tool call, A2A message, and child token mint. |
| **Pre-Commit Gates L1–L4 (`C2` & `C4`)** | **100% Live Enforcement** | Real Pydantic schema validation (L1), HMAC-SHA256 provenance verification (L2), mathematical conservation invariants (L3), and parameter bounds + taint gating (L4). |
| **Causal Lineage & BFS Rollback (`C4` & `C5`)** | **100% Live DAG + SQLite WAL** | Real vector clocks, BFS forward taint propagation ($T_{\text{fault}}$), and per-session SQLite checkpoint persistence (`runs/<session_id>/snapshots.db`). |
| **Kill-Switch (`C3_Task_Registry`)** | **100% Live Asyncio + Socket Abort** | Real `asyncio.Task.cancel()`, `httpx.AsyncClient.aclose()`, and `asyncio.Event` branch freeze (`SOFT_FREEZE`) completing in $< 5\text{ ms}$. |
| **External Banking / ERP Endpoints (`feeds.py`)** | **Simulated Local Feed Functions** | SAP S/4HANA (`erp:read`), Bloomberg (`fx:quote`), Reuters (`fx:quote_reuters`), SWIFT MT103 (`swift:wire`), and OFAC (`kyc:check`) use local Python handlers with bounded live random-walk feeds (`live_fx_rate()`, `live_risk_score()`, `live_erp_amount()`). |

---

## ⚙️ Key Setup (`.env`)

Copy `.env.example` to `.env` and set your NVIDIA NIM API key:

```bash
cp .env.example .env
```

```ini
NVIDIA_API_KEY=nvapi-your-api-key-here
AEGIS_LLM_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
```
*(Note: `ui/config.py` loads `.env` automatically without overwriting existing shell variables and raises a clear banner error if `NVIDIA_API_KEY` is missing; any active NIM chat model works; `meta/llama-3.3-70b-instruct` is a supported alternative (auto-fallback on HTTP 410).)*
*(Architecture Note: `aegis_core/` includes additive runtime fault-management methods such as `remove_chaos_fault` and `C6_Chaos_Monkey` rule management (`remove_rule`, `list_active_rules`), while legacy pre-refactor code is preserved in `Archive/`.)*

---

## 🚀 Quickstart: Test & Launch

Run the single-command launcher:
```bash
./setup_and_run.sh
```

Or run each step manually:
```bash
# 1. Run full pytest suite (SDK + Treasury Scenarios + LLMFleet MockTransport tests)
./venv/bin/pytest -v -s

# 2. Start FastAPI Control Plane on port 8000
./venv/bin/uvicorn aegis_core.server:app --host 0.0.0.0 --port 8000

# 3. Start Streamlit Live Command Center on port 8501
./venv/bin/streamlit run ui/app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
```

---

## 💬 Using the Live Chat, Governor Queue & Chaos Console

Open `http://localhost:8501` in your browser:

1. **💬 Chat (Live NIM Fleet)**:
   - Type any novel treasury goal (or click one of the quick-prompt buttons).
   - Every user prompt is signed by `Human_Operator` and recorded in the C4 Lineage Graph.
   - Watch each agent turn appear with its cryptographic receipt (`trace_id`, `tool`, `HTTP status code`, `HMAC-SHA256 prefix`, and `overridden` flag).
2. **🕸️ Live DAG & Event Log**:
   - Auto-refreshes every `1.0s` (`@st.fragment(run_every=1.0)`) to show live agent statuses, delegation edges, A2A edges, vector clocks, and proxy events.
3. **⚖️ Governor Review Queue**:
   - When an agent executes `REQUEST_HUMAN` (or when you click **Pause Branch (`SOFT_FREEZE`)**), inspect the proposed settlement payload side-by-side against the C5 SQLite checkpoint, edit the EUR amount / FX rate, and click **Approve & Resume with Override** to stamp `overridden=True` in the receipt.
4. **💥 Live Chaos Fault Console & Kill-Switch (Sidebar)**:
   - Arm `CORRUPTED_OUTPUT`, `SERVICE_UNAVAILABLE`, or `PRIVILEGE_ESCALATION` faults mid-run, or fire the **Global Hard Kill-Switch** (`< 50ms`).
   - Expand **Scripts (A/B Comparison Scenarios)** in the sidebar to run the 5 deterministic benchmark scenarios at any time.
5. **📜 Journal Viewer**:
   - Inspect and replay per-session JSONL audit logs saved under `runs/<session_id>/journal.jsonl`.
