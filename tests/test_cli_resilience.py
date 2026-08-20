import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from trading_simulator.cli import (
    _is_transient_etoro_read_error,
    _latest_unresolved_intent_id,
)
from trading_simulator.etoro_demo_execution import ExecutionLedger
from trading_simulator.etoro_demo import EtoroDemoError


def test_timeout_read_error_is_transient() -> None:
    error = EtoroDemoError(
        "eToro read failed (TimeoutError); credentials were redacted"
    )
    assert _is_transient_etoro_read_error(error) is True


def test_reconciliation_error_is_never_transient() -> None:
    error = EtoroDemoError(
        "Demo portfolio and simulator position state do not reconcile"
    )
    assert _is_transient_etoro_read_error(error) is False


def test_unattempted_intent_is_latched_until_attempted() -> None:
    directory = Path(__file__).resolve().parents[1] / "outputs" / f".pytest-latch-{uuid4().hex}"
    directory.mkdir(parents=True)
    intents = directory / "asset-intents.jsonl"
    ledger_path = directory / "asset-execution.jsonl"
    intent_id = "a" * 64
    intents.write_text(json.dumps({
        "intent_id": intent_id,
        "execution_eligible": True,
    }) + "\n", encoding="utf-8")
    live_store = SimpleNamespace(
        load=lambda: SimpleNamespace(last_abandoned_intent_id=None)
    )

    try:
        assert _latest_unresolved_intent_id(
            intents, ledger_path, live_store
        ) == intent_id

        ExecutionLedger(ledger_path)._append({
            "intent_id": intent_id, "status": "attempting"
        })
        assert _latest_unresolved_intent_id(intents, ledger_path, live_store) is None
    finally:
        shutil.rmtree(directory)
