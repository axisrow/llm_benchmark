# Stock Downloader

Загрузчик исторических данных акций с локальным кэшированием через
[yfinance-cache](https://pypi.org/project/yfinance-cache/).

## Установка

```bash
pip install yfinance-cache
```

> `pandas` устанавливается автоматически как зависимость `yfinance-cache`.

## Запуск

```bash
python downloader.py
```

Скрипт читает `config.json` из своей директории и сохраняет результат в
`data/data.parquet`.

## Параметры config.json

| Поле         | Тип     | По умолчанию        | Описание                               |
|--------------|---------|---------------------|----------------------------------------|
| `tickers`    | list    | —                   | Список тикеров                         |
| `period`     | string  | `"1y"`              | Период истории                         |
| `interval`   | string  | `"1d"`              | Интервал свечей                        |
| `max_age`    | string  | `"1h"`              | Максимальный возраст кэша              |
| `output_file`| string  | `"data/data.parquet"` | Путь к выходному файлу (Parquet)     |

## Формат данных

Parquet-файл содержит колонки: `Open`, `High`, `Low`, `Close`, `Volume`,
`Dividends`, `Stock Splits`, `Ticker`. Служебные колонки yfinance-cache
(`Repaired?`, `Final?`, `FetchDate`) удаляются автоматически.
