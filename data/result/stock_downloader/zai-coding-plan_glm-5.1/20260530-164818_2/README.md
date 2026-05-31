# Stock Downloader

Загрузчик исторических данных акций с кэшированием через
[yfinance-cache](https://pypi.org/project/yfinance-cache/).

Данные сохраняются в один Parquet-файл. Формат — как в yfinance (Open, High, Low,
Close, Volume, Dividends, Stock Splits) плюс колонка `Ticker`.

## Установка

```bash
pip install yfinance-cache
```

## Запуск

```bash
python downloader.py
```

## config.json

| Параметр     | Описание                        | По умолчанию        |
|-------------|---------------------------------|----------------------|
| `tickers`   | Список тикеров                  | `["MSFT","AAPL","GOOGL"]` |
| `period`    | Период истории                  | `"1y"`              |
| `interval`  | Интервал свечей                 | `"1d"`              |
| `max_age`   | Максимальный возраст кэша       | `"1h"`              |
| `output_file` | Путь к Parquet-файлу          | `"data/data.parquet"` |

## Тесты

```bash
pip install pytest
pytest test_downloader.py -v
```
