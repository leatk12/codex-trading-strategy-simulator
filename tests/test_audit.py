import csv
import json
from pathlib import Path
from uuid import uuid4

import pytest

from trading_simulator import (
    AuditExporter,
    AuditExportError,
    Backtest,
    CsvMarketDataLoader,
    PerformanceAnalyzer,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]


@pytest.fixture
def audit_path():  # type: ignore[no-untyped-def]
    path = PROJECT_ROOT / f".audit-test-{uuid4()}"
    yield path
    if path.exists():
        for child in path.iterdir():
            child.unlink()
        path.rmdir()


def _completed_backtest():  # type: ignore[no-untyped-def]
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "basic_strategy_example.csv", profile.symbol
    ).load()
    result = Backtest(profile, data).run()
    return result, PerformanceAnalyzer(profile).analyze(result, data)


def test_export_writes_complete_reconstructable_bundle(audit_path: Path) -> None:
    result, report = _completed_backtest()
    output = audit_path

    paths = AuditExporter().export(result, report, output)

    assert {path.name for path in paths} == set(AuditExporter.FILENAMES)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format_version"] == 1
    assert manifest["strategy_version"] == result.strategy_version
    assert manifest["leverage_allowed"] is False

    with (output / "decisions.csv").open(encoding="utf-8", newline="") as file:
        decisions = list(csv.DictReader(file))
    assert len(decisions) == len(result.decisions)
    assert decisions[0]["strategy_version"] == result.strategy_version
    assert json.loads(decisions[0]["facts_json"])

    with (output / "trades.csv").open(encoding="utf-8", newline="") as file:
        trades = list(csv.DictReader(file))
    assert len(trades) == len(result.trades)
    assert trades[0]["quantity"] == str(result.trades[0].quantity)

    with (output / "equity_curve.csv").open(encoding="utf-8", newline="") as file:
        equity = list(csv.DictReader(file))
    assert len(equity) == len(report.equity_curve)
    performance = json.loads((output / "performance.json").read_text("utf-8"))
    assert performance["ending_capital"] == str(report.ending_capital)
    assert performance["leverage_allowed"] is False
    assert "equity_curve" not in performance


def test_export_refuses_to_overwrite_existing_bundle(audit_path: Path) -> None:
    result, report = _completed_backtest()
    output = audit_path
    exporter = AuditExporter()
    exporter.export(result, report, output)

    with pytest.raises(AuditExportError, match="refusing to overwrite"):
        exporter.export(result, report, output)


def test_export_rejects_file_as_output_directory(audit_path: Path) -> None:
    result, report = _completed_backtest()
    target = audit_path
    target.write_text("occupied", encoding="utf-8")

    try:
        with pytest.raises(AuditExportError, match="not a directory"):
            AuditExporter().export(result, report, target)
    finally:
        target.unlink()
