"""
Per-Session Isolated Background Event Loop & State Manager (`SessionState`) for Streamlit UI.
Guarantees:
1. Never uses module-global `default_kernel` or `app` in the chat path — every Streamlit
   browser session gets its own `ControlPlaneKernel(db_path="runs/<session_id>/snapshots.db")`
   and `create_aegis_app(kernel)`.
2. Runs a dedicated background daemon thread with `asyncio.new_event_loop()` so all kernel
   mutations (`submit_goal`, `inject_fault`, `remove_fault`, `trigger_kill_switch`,
   `pause_and_queue_governor_review`, `resolve_governor_item`, `stop_session`,
   `run_scripted_scenario`) execute on that single thread via `asyncio.run_coroutine_threadsafe`.
3. `snapshot(cursor=0)` is fast and read-only for `@st.fragment(run_every=1.0)` polling.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from pathlib import Path
import threading
import time
from typing import Any, Optional
from uuid import uuid4

from aegis_core.kernel import ControlPlaneKernel
from aegis_core.server import create_aegis_app
from examples.treasury_case_study.llm_brains import NimBrain
from examples.treasury_case_study.llm_fleet import LLMFleet
from examples.treasury_case_study.scenarios import TreasuryScenarioRunner
from examples.treasury_case_study.tools_and_gates import register_treasury_plugin
from ui.config import DEFAULT_NIM_MODEL, load_dotenv


class SessionState:
    """
    Isolated per-session runtime managing a dedicated `asyncio` event loop thread,
    `ControlPlaneKernel`, FastAPI ASGI app, `LLMFleet`, and `TreasuryScenarioRunner`.
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        brain: Optional[NimBrain] = None,
        feed_mode: str = "live",
    ) -> None:
        load_dotenv()
        self.session_id = session_id or f"sess_{uuid4().hex[:8]}"
        self.run_dir = Path("runs") / self.session_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = str(self.run_dir / "snapshots.db")
        self.journal_path = self.run_dir / "journal.jsonl"

        self.kernel = ControlPlaneKernel(db_path=self.db_path)
        register_treasury_plugin(self.kernel)
        self.app = create_aegis_app(self.kernel)

        self._brain = brain
        self.feed_mode = feed_mode
        self.fleet: Optional[LLMFleet] = None
        self.scenario_runner = TreasuryScenarioRunner(
            kernel=self.kernel,
            fastapi_app=self.app,
        )
        self.last_error: Optional[str] = None
        self.last_scenario_result: Optional[dict[str, Any]] = None
        self._current_goal_future: Optional[Future[Any]] = None
        self._telemetry_cache: dict[str, Any] = {}

        # Dedicated background event loop thread
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop_forever,
            name=f"aegis-loop-{self.session_id}",
            daemon=True,
        )
        self._thread.start()

        # Bootstrap fleet & Human_Operator passport on the background loop
        boot_fut = asyncio.run_coroutine_threadsafe(
            self._async_bootstrap(), self._loop
        )
        boot_fut.result(timeout=15.0)

    def _run_loop_forever(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_refresh_telemetry(self) -> dict[str, Any]:
        """Atomically snapshot kernel telemetry on the background event loop thread."""
        self._telemetry_cache = self.kernel.get_telemetry_state()
        return self._telemetry_cache

    async def _async_bootstrap(self) -> None:
        brain_instance = self._brain
        if brain_instance is None:
            try:
                brain_instance = NimBrain()
            except Exception as exc:
                self.last_error = str(exc)
                brain_instance = NimBrain(
                    api_key="MISSING_KEY", model=DEFAULT_NIM_MODEL
                )

        self.fleet = LLMFleet(
            kernel=self.kernel,
            app=self.app,
            brain=brain_instance,
            journal_path=self.journal_path,
            feed_mode=self.feed_mode,
            require_governor_on_fault=True,
        )
        await self.fleet.initialize_fleet()
        await self._async_refresh_telemetry()

    # =========================================================================
    # Thread-Safe Command Dispatchers (UI -> Background Event Loop)
    # =========================================================================
    def submit_goal(
        self,
        goal: str,
        max_turns: int = 24,
        wait: bool = False,
        timeout: float = 60.0,
    ) -> Future[dict[str, Any]]:
        """Submit a natural-language goal to run on the background fleet loop."""
        self.last_error = None

        async def _run() -> dict[str, Any]:
            assert self.fleet is not None
            try:
                return await self.fleet.run_goal(goal=goal, max_turns=max_turns)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                # Reflect error in C3 process_table and kernel event_log so DAG shows it!
                target_agent = "Chief_Treasury_Orchestrator"
                if self.fleet.transcript:
                    last_a = self.fleet.transcript[-1].get("agent_id")
                    if last_a and last_a in self.kernel.c3.process_table:
                        target_agent = last_a
                if target_agent in self.kernel.c3.process_table:
                    self.kernel.c3.process_table[target_agent]["status"] = "ERROR_LLM"
                self.kernel._log_event(
                    "LLM_OR_AGENT_ERROR_502",
                    target_agent,
                    "Root",
                    f"Agent '{target_agent}' encountered error: {self.last_error}",
                    {"error": self.last_error, "agent_id": target_agent},
                )
                self.fleet._record_turn(
                    {
                        "turn": len(self.fleet.transcript),
                        "type": "ERROR",
                        "agent_id": target_agent,
                        "role": "LLM_Adapter",
                        "branch_id": "Root",
                        "action": "ERROR",
                        "target": "NIM_API",
                        "content": f"[NIM_ERROR] {self.last_error}",
                        "receipt": {
                            "trace_id": "err",
                            "tool": "nim:chat",
                            "http_code": 502,
                            "hmac_prefix": "ERROR",
                            "overridden": False,
                            "error": self.last_error,
                        },
                    }
                )
                raise

        fut = asyncio.run_coroutine_threadsafe(_run(), self._loop)
        fut.add_done_callback(
            lambda f: f.exception() if not f.cancelled() else None
        )
        self._current_goal_future = fut
        if wait:
            fut.result(timeout=timeout)
        return fut

    def inject_fault(
        self,
        target: str,
        fault_category: str,
        probability: float = 1.0,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Inject a deterministic C6 Chaos fault rule on the background loop."""
        async def _do_inject() -> dict[str, Any]:
            assert self.fleet is not None
            res = await self.fleet.client.inject_chaos_fault(
                target=target,
                fault_category=fault_category,
                probability=probability,
                config=config or {},
            )
            self.fleet._flush_kernel_events_to_journal()
            return res

        return asyncio.run_coroutine_threadsafe(_do_inject(), self._loop).result(
            timeout=10.0
        )

    def remove_fault(
        self,
        target_or_id: str = "ALL",
        clear_agent_status: bool = True,
    ) -> dict[str, Any]:
        """Remove a specific C6 Chaos fault rule (or 'ALL') and optionally clear agent error/taint states."""
        async def _do_remove() -> dict[str, Any]:
            assert self.fleet is not None
            res = await self.fleet.client.remove_chaos_fault(
                target_or_id=target_or_id,
                clear_agent_status=clear_agent_status,
            )
            if clear_agent_status and target_or_id == "ALL":
                self.last_error = None
            self.fleet._flush_kernel_events_to_journal()
            return res

        return asyncio.run_coroutine_threadsafe(_do_remove(), self._loop).result(
            timeout=10.0
        )

    def trigger_kill_switch(
        self,
        mode: str = "GLOBAL_HARD",
        target_id: str = "ALL",
        operator_override_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Trigger `GLOBAL_HARD`, `SURGICAL`, or `SOFT_FREEZE` kill-switch on the background loop."""
        async def _do_kill() -> dict[str, Any]:
            assert self.fleet is not None
            if mode == "SOFT_FREEZE" and operator_override_state:
                self.kernel.c4.update_provenance_on_override(
                    operator_override_state
                )
            if mode == "SURGICAL" and target_id != "ALL":
                raw_targets = [t.strip() for t in target_id.split(",") if t.strip()]
                last_res: dict[str, Any] = {}
                for t in raw_targets:
                    last_res = await self.fleet.recover_and_restart_node(
                        failed_agent_id=t,
                        reason=f"Surgical kill switch triggered on '{t}'",
                    )
                self.fleet._flush_kernel_events_to_journal()
                await self._async_refresh_telemetry()
                return last_res.get("kill_receipt", last_res)

            res = await self.fleet.client.trigger_kill_switch(
                mode=mode,
                target_id=target_id,
                operator_override_state=operator_override_state,
            )
            self.fleet._flush_kernel_events_to_journal()
            await self._async_refresh_telemetry()
            return res

        return asyncio.run_coroutine_threadsafe(_do_kill(), self._loop).result(
            timeout=10.0
        )

    def pause_and_queue_governor_review(
        self,
        branch_id: str = "Branch_A",
        proposed_payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Pause `branch_id` via `SOFT_FREEZE` and immediately enqueue a pending Governor Review item."""
        async def _do_pause_queue() -> dict[str, Any]:
            assert self.fleet is not None
            item, _ = await self.fleet.enqueue_governor_review(
                branch_id=branch_id,
                agent_id="Human_Operator",
                proposed_payload=proposed_payload,
                message=f"Branch '{branch_id}' paused via SOFT_FREEZE for Human Governor review & override.",
            )
            return item

        return asyncio.run_coroutine_threadsafe(_do_pause_queue(), self._loop).result(
            timeout=10.0
        )

    def set_require_governor_on_wire(self, enabled: bool) -> None:
        if self.fleet is not None:
            self.fleet.require_governor_on_wire = bool(enabled)

    def set_step_by_step_mode(self, enabled: bool) -> None:
        """Toggle live step-by-step execution mode on the fleet."""
        if self.fleet is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.fleet.set_step_by_step_mode, bool(enabled))
        elif self.fleet is not None:
            self.fleet.set_step_by_step_mode(bool(enabled))

    def step_forward_live(self) -> None:
        """Advance one turn when the fleet is paused in live step-by-step mode."""
        if self.fleet is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.fleet.step_forward)
        elif self.fleet is not None:
            self.fleet.step_forward()

    def resolve_governor_item(
        self,
        item_id: str,
        operator_override_state: Optional[dict[str, Any]] = None,
        approved: bool = True,
    ) -> dict[str, Any]:
        """Approve (or reject) and resume a `SOFT_FREEZE` item from the Governor Review Queue."""
        async def _do_resolve() -> dict[str, Any]:
            assert self.fleet is not None
            return await self.fleet.resolve_governor_item(
                item_id=item_id,
                operator_override_state=operator_override_state,
                approved=approved,
            )

        return asyncio.run_coroutine_threadsafe(_do_resolve(), self._loop).result(
            timeout=10.0
        )

    def run_scripted_scenario(self, scenario_key: str) -> dict[str, Any]:
        """Run one of the 5 scripted A/B comparison scenarios on the session's isolated kernel."""
        async def _do_scenario() -> dict[str, Any]:
            dispatch = {
                "1": self.scenario_runner.run_scenario_1_normal,
                "2": self.scenario_runner.run_scenario_2_confused_deputy_rbac,
                "3": self.scenario_runner.run_scenario_3_corrupted_output_rollback,
                "4": self.scenario_runner.run_scenario_4_service_unavailable_failover,
                "5A": self.scenario_runner.run_scenario_5a_soft_freeze_override,
                "5B": self.scenario_runner.run_scenario_5b_global_hard_kill,
                "5": self.scenario_runner.run_scenario_5_kill_switch_and_override,
                "ALL": self.scenario_runner.run_all_scenarios,
            }
            fn = dispatch[scenario_key]
            res = await fn()
            if self.fleet is not None:
                self.fleet._flush_kernel_events_to_journal()
            return res

        timeout = 90.0 if scenario_key == "ALL" else 30.0
        res = asyncio.run_coroutine_threadsafe(_do_scenario(), self._loop).result(
            timeout=timeout
        )
        self.last_scenario_result = res
        return res

    def reset_session(self) -> None:
        """Reset kernel state and re-initialize the live fleet on the background loop."""
        async def _do_reset() -> None:
            self.kernel.reset_state(preserve_registered_tools=True)
            register_treasury_plugin(self.kernel)
            if self.fleet is not None:
                self.fleet.fleet = None
                self.fleet.live_agents.clear()
                self.fleet.transcript.clear()
                self.fleet.step_snapshots.clear()
                self.fleet.governor_queue.clear()
                self.fleet._last_logged_event_idx = 0
                await self.fleet.initialize_fleet()
            await self._async_refresh_telemetry()

        asyncio.run_coroutine_threadsafe(_do_reset(), self._loop).result(timeout=15.0)
        self.last_error = None
        self.last_scenario_result = None

    def _cancel_goal_tasks(self) -> None:
        """Cancel all running tasks on `_loop` and stop the loop cleanly."""
        current = asyncio.current_task(loop=self._loop)
        for task in asyncio.all_tasks(self._loop):
            if task is not current and not task.done():
                task.cancel()
        self._loop.stop()

    def stop_session(self) -> None:
        """Close persistent HTTP clients, cancel all loop tasks, and stop the background event loop."""
        if self._loop.is_running():
            if self.fleet is not None and self.fleet.brain is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.fleet.brain.aclose(), self._loop
                    ).result(timeout=2.0)
                except Exception:
                    pass
            self._loop.call_soon_threadsafe(self._cancel_goal_tasks)

        if self._current_goal_future is not None:
            try:
                self._current_goal_future.cancel()
                self._current_goal_future.exception(timeout=0.5)
            except Exception:
                pass

    # =========================================================================
    # Fast Read-Only Snapshot Polling (Zero Cross-Thread Race)
    # =========================================================================
    def snapshot(self, cursor: int = 0) -> dict[str, Any]:
        """
        Return a thread-safe snapshot of telemetry, chat transcript, governor queue,
        and new events since `cursor` by refreshing `_telemetry_cache` on the loop thread.
        """
        if self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._async_refresh_telemetry(), self._loop
                ).result(timeout=2.0)
            except Exception:
                pass
        telemetry = self._telemetry_cache
        event_log = telemetry.get("event_log", [])
        new_events = event_log[cursor:] if cursor < len(event_log) else []

        transcript = list(self.fleet.transcript) if self.fleet else []
        step_snapshots = list(self.fleet.step_snapshots) if self.fleet else []
        governor_queue = list(self.fleet.governor_queue) if self.fleet else []
        is_running = bool(
            (self.fleet and self.fleet.is_running)
            or (
                self._current_goal_future is not None
                and not self._current_goal_future.done()
            )
        )

        return {
            "session_id": self.session_id,
            "timestamp": time.time(),
            "is_running": is_running,
            "last_error": self.last_error,
            "cursor": len(event_log),
            "new_events": new_events,
            "telemetry": telemetry,
            "transcript": transcript,
            "step_snapshots": step_snapshots,
            "governor_queue": governor_queue,
            "journal_path": str(self.journal_path),
            "last_scenario_result": self.last_scenario_result,
            "require_governor_on_wire": bool(
                self.fleet and self.fleet.require_governor_on_wire
            ),
            "step_by_step_mode": bool(
                self.fleet and self.fleet.step_by_step_mode
            ),
            "waiting_for_step": bool(
                self.fleet and self.fleet.waiting_for_step
            ),
        }


def get_or_create_session_state(st_session_state: Any) -> SessionState:
    """Helper to fetch or initialize an isolated `SessionState` inside `st.session_state`."""
    if "aegis_session" not in st_session_state or st_session_state["aegis_session"] is None:
        st_session_state["aegis_session"] = SessionState()
    return st_session_state["aegis_session"]
