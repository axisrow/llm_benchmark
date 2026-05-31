"""Тесты для downloader.py — мокаем yfinance_cache, сети не трогаем."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import downloader


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _make_price_df(rows: int = 5) -> pd.DataFrame:
    """Создаёт датафрейм, похожий на вывод yfinance-cache."""
    dates = pd.date_range("2025-01-01", periods=rows, freq="D")
    df = pd.DataFrame(
        {
            "Open": range(rows),
            "High": range(rows),
            "Low": range(rows),
            "Close": [float(i) for i in range(rows)],
            "Volume": [1000 * i for i in range(rows)],
            "Dividends": [0.0] * rows,
            "Stock Splits": [0.0] * rows,
            "Repaired?": [False] * rows,
            "Final?": [True] * rows,
            "FetchDate": pd.Timestamp("2025-01-01"),
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
    """Создаёт временный config.json, мокает yfc.Ticker, вызывает main()."""
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
# Тесты
# ---------------------------------------------------------------------------

class TestSuccessfulDownload:
    """Успешная загрузка нескольких тикеров."""

    def test_parquet_created(self, tmp_path: Path) -> None:
        df = _make_price_df(rows=5)

        t1 = MagicMock()
        t1.history.return_value = df.copy()
        t2 = MagicMock()
        t2.history.return_value = df.copy()

        output_path = tmp_path / "out" / "data.parquet"
        config = {
            "tickers": ["MSFT", "AAPL"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": str(output_path),
        }

        result = _run_main(tmp_path, config, {"MSFT": t1, "AAPL": t2})

        assert result.exists()
        loaded = pd.read_parquet(result)
        # 5 строк × 2 тикера = 10
        assert len(loaded) == 10
        # Колонка Ticker добавлена
        assert "Ticker" in loaded.columns
        # Служебные колонки удалены
        for col in ("Repaired?", "Final?", "FetchDate"):
            assert col not in loaded.columns
        # 7 колонок yfinance + Ticker = 8
        assert len(loaded.columns) == 8


class TestEmptyResponse:
    """history() возвращает пустой DataFrame — файла быть не должно."""

    def test_no_parquet_on_empty(self, tmp_path: Path) -> None:
        empty_df = pd.DataFrame()

        t1 = MagicMock()
        t1.history.return_value = empty_df

        output_path = tmp_path / "out" / "data.parquet"
        config = {
            "tickers": ["MSFT"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": str(output_path),
        }

        result = _run_main(tmp_path, config, {"MSFT": t1})

        assert not result.exists()


class TestErrorHandling:
    """Первый тикер бросает исключение — продолжаем со вторым."""

    def test_continues_on_error(self, tmp_path: Path) -> None:
        t_bad = MagicMock()
        t_bad.history.side_effect = RuntimeError("network error")

        good_df = _make_price_df(rows=5)
        t_good = MagicMock()
        t_good.history.return_value = good_df.copy()

        output_path = tmp_path / "out" / "data.parquet"
        config = {
            "tickers": ["BAD", "GOOD"],
            "period": "1y",
            "interval": "1d",
            "max_age": "1h",
            "output_file": str(output_path),
        }

        result = _run_main(
            tmp_path, config, {"BAD": t_bad, "GOOD": t_good}
        )

        assert result.exists()
        loaded = pd.read_parquet(result)
        # Только один тикер
        assert set(loaded["Ticker"].unique()) == {"GOOD"}
        assert len(loaded) == 5


class TestConfigReading:
    """Параметры из конфига передаются в history() верно."""

    def test_params_passed_to_history(self, tmp_path: Path) -> None:
        df = _make_price_df(rows=3)

        t = MagicMock()
        t.history.return_value = df.copy()

        output_path = tmp_path / "out" / "data.parquet"
        config = {
            "tickers": ["TEST"],
            "period": "6mo",
            "interval": "1wk",
            "max_age": "2h",
            "output_file": str(output_path),
        }

        _run_main(tmp_path, config, {"TEST": t})

        t.history.assert_called_once_with(
            period="6mo", interval="1wk", max_age="2h"
        )
