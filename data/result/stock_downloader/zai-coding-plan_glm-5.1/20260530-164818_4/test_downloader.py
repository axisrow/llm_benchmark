"""Тесты для downloader.py — мокаем yfinance_cache, чтобы не ходить в сеть."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import downloader


def _make_price_df(rows: int = 5) -> pd.DataFrame:
    """Создаёт датафрейм с колонками yfinance-cache (включая служебные)."""
    dates = pd.date_range("2024-01-01", periods=rows, freq="D")
    df = pd.DataFrame(
        {
            "Open": range(100, 100 + rows),
            "High": range(101, 101 + rows),
            "Low": range(99, 99 + rows),
            "Close": range(100, 100 + rows, dtype=float),
            "Volume": [1_000_000] * rows,
            "Dividends": [0.0] * rows,
            "Stock Splits": [0.0] * rows,
            "Repaired?": [False] * rows,
            "Final?": [True] * rows,
            "FetchDate": pd.Timestamp("2024-01-10"),
        },
        index=dates,
    )
    df.index.name = "Date"
    return df


def _run_main(
    tmp_path: Path,
    config: dict,
    mock_tickers: dict[str, MagicMock],
) -> Path:
    """Создаёт временный config.json, подменяет __file__ и мокает yfc.Ticker."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")

    output_path = Path(config["output_file"])

    def _ticker_side_effect(symbol: str):
        if symbol in mock_tickers:
            return mock_tickers[symbol]
        raise ValueError(f"Unexpected ticker: {symbol}")

    with (
        patch.object(downloader, "__file__", str(config_file)),
        patch("downloader.yfc.Ticker", side_effect=_ticker_side_effect),
    ):
        downloader.main()

    return output_path


# ── Тест 1: успешная загрузка ────────────────────────────────────────────────


class TestSuccessfulDownload:
    """Проверяем, что parquet создаётся, Ticker добавлен, служебные столбцы удалены."""

    def test_parquet_created(self, tmp_path: Path) -> None:
        mock_msft = MagicMock()
        mock_msft.history.return_value = _make_price_df(rows=5)

        mock_aapl = MagicMock()
        mock_aapl.history.return_value = _make_price_df(rows=5)

        output = tmp_path / "out" / "data.parquet"
        config = {
            "tickers": ["MSFT", "AAPL"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": str(output),
        }
        result_path = _run_main(
            tmp_path,
            config,
            {"MSFT": mock_msft, "AAPL": mock_aapl},
        )

        assert result_path.exists(), "Parquet файл не создан"

        df = pd.read_parquet(result_path)

        # Служебные столбцы yfinance-cache должны быть удалены
        for col in ("Repaired?", "Final?", "FetchDate"):
            assert col not in df.columns, f"Столбец {col} не удалён"

        # 7 колонок yfinance + Ticker = 8
        assert len(df.columns) == 8, f"Ожидалось 8 колонок, получено {len(df.columns)}"
        assert "Ticker" in df.columns

        # 5 строк × 2 тикера = 10
        assert len(df) == 10, f"Ожидалось 10 строк, получено {len(df)}"


# ── Тест 2: пустой ответ ────────────────────────────────────────────────────


class TestEmptyResponse:
    """Если history() возвращает пустой DataFrame — parquet не создаётся."""

    def test_no_parquet_on_empty(self, tmp_path: Path) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()

        output = tmp_path / "out" / "data.parquet"
        config = {
            "tickers": ["MSFT"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": str(output),
        }
        _run_main(tmp_path, config, {"MSFT": mock_ticker})

        assert not output.exists(), "Parquet не должен был создаться"


# ── Тест 3: обработка ошибок ────────────────────────────────────────────────


class TestErrorHandling:
    """Первый тикер бросает Exception — продолжаем со вторым."""

    def test_continues_on_error(self, tmp_path: Path) -> None:
        mock_bad = MagicMock()
        mock_bad.history.side_effect = Exception("Network error")

        mock_good = MagicMock()
        mock_good.history.return_value = _make_price_df(rows=5)

        output = tmp_path / "out" / "data.parquet"
        config = {
            "tickers": ["BAD", "GOOD"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": str(output),
        }
        _run_main(
            tmp_path,
            config,
            {"BAD": mock_bad, "GOOD": mock_good},
        )

        assert output.exists(), "Parquet должен был создаться для GOOD"

        df = pd.read_parquet(output)
        assert len(df) == 5, f"Ожидалось 5 строк, получено {len(df)}"
        assert set(df["Ticker"].unique()) == {"GOOD"}


# ── Тест 4: параметры из конфига ────────────────────────────────────────────


class TestConfigReading:
    """Параметры из config.json передаются в history() корректно."""

    def test_params_passed_to_history(self, tmp_path: Path) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_price_df(rows=3)

        output = tmp_path / "out" / "data.parquet"
        config = {
            "tickers": ["TEST"],
            "period": "6mo",
            "interval": "1wk",
            "max_age": "2h",
            "output_file": str(output),
        }
        _run_main(tmp_path, config, {"TEST": mock_ticker})

        mock_ticker.history.assert_called_once_with(
            period="6mo",
            interval="1wk",
            max_age="2h",
        )
