"""Tests for downloader.py — mocked yfinance_cache, no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import downloader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_price_df(rows: int = 5) -> pd.DataFrame:
    """Create a DataFrame mimicking yfinance-cache output (with extra columns)."""
    dates = pd.date_range("2024-01-01", periods=rows, freq="D")
    df = pd.DataFrame(
        {
            "Open": range(100, 100 + rows),
            "High": range(101, 101 + rows),
            "Low": range(99, 99 + rows),
            "Close": range(100, 100 + rows),
            "Volume": [1_000_000] * rows,
            "Dividends": [0.0] * rows,
            "Stock Splits": [0.0] * rows,
            "Repaired?": [False] * rows,
            "Final?": [True] * rows,
            "FetchDate": pd.Timestamp("2024-01-10"),
        },
        index=dates,
    )
    return df


def _run_main(
    tmp_path: Path,
    config: dict,
    mock_tickers: dict[str, MagicMock],
) -> Path:
    """Write config, patch __file__ and yfc.Ticker, run main(), return output path."""
    # Override output_file to an absolute path inside tmp_path
    rel_output = config.get("output_file", "data/data.parquet")
    config = {**config, "output_file": str(tmp_path / rel_output)}

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")

    output_path = Path(config["output_file"])

    def _ticker_side_effect(name: str) -> MagicMock:
        return mock_tickers[name]

    with (
        patch.object(downloader, "__file__", str(tmp_path / "downloader.py")),
        patch("downloader.yfc.Ticker", side_effect=_ticker_side_effect),
    ):
        downloader.main()

    return output_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSuccessfulDownload:
    def test_parquet_created(self, tmp_path: Path) -> None:
        mock_msft = MagicMock()
        mock_msft.history.return_value = _make_price_df(5)

        mock_aapl = MagicMock()
        mock_aapl.history.return_value = _make_price_df(5)

        config = {
            "tickers": ["MSFT", "AAPL"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": "data/data.parquet",
        }

        output = _run_main(
            tmp_path, config, {"MSFT": mock_msft, "AAPL": mock_aapl}
        )

        assert output.exists(), "Parquet file must be created"

        df = pd.read_parquet(output)

        # Ticker column added
        assert "Ticker" in df.columns

        # yfinance-cache columns removed
        for bad_col in ("Repaired?", "Final?", "FetchDate"):
            assert bad_col not in df.columns

        # 7 yfinance columns + Ticker = 8
        assert len(df.columns) == 8

        # 5 rows × 2 tickers = 10
        assert len(df) == 10


class TestEmptyResponse:
    def test_no_parquet_on_empty(self, tmp_path: Path) -> None:
        mock_msft = MagicMock()
        mock_msft.history.return_value = pd.DataFrame()

        config = {
            "tickers": ["MSFT"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": "data/data.parquet",
        }

        output = _run_main(tmp_path, config, {"MSFT": mock_msft})

        assert not output.exists(), "Parquet must not be created on empty data"


class TestErrorHandling:
    def test_continues_on_error(self, tmp_path: Path) -> None:
        mock_bad = MagicMock()
        mock_bad.history.side_effect = Exception("network error")

        mock_good = MagicMock()
        mock_good.history.return_value = _make_price_df(5)

        config = {
            "tickers": ["BAD", "GOOD"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": "data/data.parquet",
        }

        output = _run_main(
            tmp_path, config, {"BAD": mock_bad, "GOOD": mock_good}
        )

        assert output.exists(), "Parquet must be created from the successful ticker"

        df = pd.read_parquet(output)
        assert list(df["Ticker"].unique()) == ["GOOD"]


class TestConfigReading:
    def test_params_passed_to_history(self, tmp_path: Path) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_price_df(3)

        config = {
            "tickers": ["MSFT"],
            "period": "6mo",
            "interval": "1wk",
            "max_age": "2h",
            "output_file": "data/data.parquet",
        }

        _run_main(tmp_path, config, {"MSFT": mock_ticker})

        mock_ticker.history.assert_called_once_with(
            period="6mo", interval="1wk", max_age="2h"
        )
