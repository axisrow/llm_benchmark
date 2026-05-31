"""Тесты для downloader.py — мокаем yfinance_cache.Ticker."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import downloader


def _make_price_df(rows: int = 5) -> pd.DataFrame:
    """Создаёт тестовый датафрейм с колонками yfinance-cache."""
    dates = pd.date_range("2024-01-01", periods=rows, freq="D")
    data = {
        "Open": range(rows),
        "High": range(rows),
        "Low": range(rows),
        "Close": [float(i) for i in range(rows)],
        "Volume": [1000 + i for i in range(rows)],
        "Dividends": [0.0] * rows,
        "Stock Splits": [0.0] * rows,
        "Repaired?": [False] * rows,
        "Final?": [True] * rows,
        "FetchDate": ["2024-01-01"] * rows,
    }
    return pd.DataFrame(data, index=dates)


def _run_main(
    tmp_path: Path,
    config: dict,
    mock_tickers: dict[str, MagicMock],
) -> Path:
    """Создаёт временный конфиг, подменяет пути и мокает yfc.Ticker."""
    # Делаем output_file абсолютным путём внутри tmp_path,
    # чтобы downloader.py создал файл там, куда укажет тест.
    rel_output = config.get("output_file", "data/data.parquet")
    abs_output = str(tmp_path / rel_output)
    config = {**config, "output_file": abs_output}

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")

    output_path = Path(abs_output)

    def _side_effect(ticker: str) -> MagicMock:
        return mock_tickers[ticker]

    with (
        patch.object(downloader, "__file__", str(tmp_path / "downloader.py")),
        patch("downloader.yfc.Ticker", side_effect=_side_effect),
    ):
        downloader.main()

    return output_path


class TestSuccessfulDownload:
    """Проверка успешной загрузки двух тикеров."""

    def test_parquet_created(self, tmp_path: Path) -> None:
        df = _make_price_df(rows=5)
        mock_msft = MagicMock()
        mock_msft.history.return_value = df
        mock_aapl = MagicMock()
        mock_aapl.history.return_value = df

        output = _run_main(
            tmp_path,
            config={
                "tickers": ["MSFT", "AAPL"],
                "period": "1y",
                "interval": "1d",
                "max_age": "1h",
                "output_file": "data/data.parquet",
            },
            mock_tickers={"MSFT": mock_msft, "AAPL": mock_aapl},
        )

        assert output.exists(), "Parquet файл не создан"

        result = pd.read_parquet(output)
        # 5 строк × 2 тикера = 10
        assert len(result) == 10
        assert set(result["Ticker"].unique()) == {"MSFT", "AAPL"}

        # Убедимся, что служебные колонки удалены
        for col in ["Repaired?", "Final?", "FetchDate"]:
            assert col not in result.columns, f"Колонка {col} не удалена"

        # 7 колонок yfinance + Ticker = 8
        assert len(result.columns) == 8


class TestEmptyResponse:
    """Проверка: при пустом ответе parquet не создаётся."""

    def test_no_parquet_on_empty(self, tmp_path: Path) -> None:
        empty_df = pd.DataFrame()
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = empty_df

        output = _run_main(
            tmp_path,
            config={
                "tickers": ["MSFT"],
                "period": "1y",
                "interval": "1d",
                "max_age": "1h",
                "output_file": "data/data.parquet",
            },
            mock_tickers={"MSFT": mock_ticker},
        )

        assert not output.exists(), "Parquet не должен создаваться при пустом ответе"


class TestErrorHandling:
    """Проверка: при ошибке одного тикера загрузка продолжается."""

    def test_continues_on_error(self, tmp_path: Path) -> None:
        mock_bad = MagicMock()
        mock_bad.history.side_effect = Exception("network error")

        df = _make_price_df(rows=5)
        mock_good = MagicMock()
        mock_good.history.return_value = df

        output = _run_main(
            tmp_path,
            config={
                "tickers": ["BAD", "GOOD"],
                "period": "1y",
                "interval": "1d",
                "max_age": "1h",
                "output_file": "data/data.parquet",
            },
            mock_tickers={"BAD": mock_bad, "GOOD": mock_good},
        )

        assert output.exists()
        result = pd.read_parquet(output)
        assert list(result["Ticker"].unique()) == ["GOOD"]
        assert len(result) == 5


class TestConfigReading:
    """Проверка: параметры из конфига передаются в history()."""

    def test_params_passed_to_history(self, tmp_path: Path) -> None:
        df = _make_price_df(rows=3)
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df

        _run_main(
            tmp_path,
            config={
                "tickers": ["MSFT"],
                "period": "6mo",
                "interval": "1wk",
                "max_age": "2h",
                "output_file": "data/data.parquet",
            },
            mock_tickers={"MSFT": mock_ticker},
        )

        mock_ticker.history.assert_called_once_with(
            period="6mo", interval="1wk", max_age="2h"
        )
