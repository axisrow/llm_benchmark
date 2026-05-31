"""Download historical stock data via yfinance-cache and save to Parquet."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yfinance_cache as yfc


def main() -> None:
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        config: dict = json.load(f)

    tickers: list[str] = config.get("tickers", ["MSFT", "AAPL", "GOOGL"])
    period: str = config.get("period", "1y")
    interval: str = config.get("interval", "1d")
    max_age: str = config.get("max_age", "1h")
    output_file: str = config.get("output_file", "data/data.parquet")

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    frames: dict[str, pd.DataFrame] = {}

    for ticker in tickers:
        try:
            df = yfc.Ticker(ticker).history(
                period=period, interval=interval, max_age=max_age
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка при загрузке {ticker}: {exc}")
            continue

        if df.empty:
            print(f"{ticker}: пустой ответ, пропуск")
            continue

        # Удаляем служебные столбцы yfinance-cache
        for col in ("Repaired?", "Final?", "FetchDate"):
            if col in df.columns:
                df.drop(columns=[col], inplace=True)

        frames[ticker] = df
        print(
            f"{ticker}: {len(df)} строк, "
            f"период {df.index.min().date()} — {df.index.max().date()}, "
            f"последняя цена Close {df['Close'].iloc[-1]:.2f}"
        )

    if not frames:
        print("Нет данных")
        return

    for ticker, df in frames.items():
        df["Ticker"] = ticker

    all_data = pd.concat(frames.values())
    all_data.to_parquet(output_path)
    print(
        f"Сохранено в {output_path}: "
        f"{len(all_data)} строк, {len(frames)} тикеров"
    )


if __name__ == "__main__":
    main()
