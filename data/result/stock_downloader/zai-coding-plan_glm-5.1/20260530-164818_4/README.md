# Stock Downloader

Загрузчик исторических данных акций с кэшированием через [yfinance-cache](https://pypi.org/project/yfinance-cache/).

## Установка

```bash
pip install yfinance-cache
```

## Запуск

```bash
python downloader.py
```

Скрипт читает `config.json` из той же папки, скачивает данные по указанным тикерам
и сохраняет их в один Parquet-файл.

## Параметры config.json

| Параметр      | Описание                         | По умолчанию         |
|---------------|----------------------------------|----------------------|
| `tickers`     | Список тикеров                   | `["MSFT", "AAPL", "GOOGL"]` |
| `period`      | Период загрузки                  | `"1y"`               |
| `interval`    | Интервал свечей                  | `"1d"`               |
| `max_age`     | Максимальный возраст кэша        | `"1h"`               |
| `output_file` | Путь к выходному Parquet-файлу   | `"data/data.parquet"` |

## Выходные данные

Parquet-файл с колонками: `Open`, `High`, `Low`, `Close`, `Volume`, `Dividends`,
`Stock Splits`, `Ticker`.

## Тесты

```bash
pip install pytest
pytest test_downloader.py -v
```
