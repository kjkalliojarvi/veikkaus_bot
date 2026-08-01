# veikkaus_bot

Fetches Finnish and Swedish harness racing (ravit) data from the [Veikkaus](https://www.veikkaus.fi) open Toto REST API and saves it as JSON (with optional DuckDB storage).

## Requirements

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
```

## Usage

```bash
mkdir -p data                                          # output directory must exist
uv run veikkaus fi_se                                  # fetch today's FI + SE cards, write data/YYYY-M-D-<country>.json
uv run veikkaus load data/*.json --db veikkaus_data.duckdb # load saved dump(s) into DuckDB
```

The `fi_se` command walks the full `Card → Race → Runner` tree from the API for each country and writes one JSON file per country into `data/`. FI and SE are fetched independently, so a failure in one does not stop the other.

The `load` command creates the tables (if needed) and loads one or more saved JSON dumps into a DuckDB database (default `veikkaus_data.duckdb`). Each file is loaded independently, so a bad dump does not abort the batch.

## How it works

- `veikkaus_bot/get_data_json.py` — Pydantic models for the API resources (`Card`, `Race`, `Runner`, `Pool`, ...), the `VeikkausData` aggregator that fetches and flattens the data, and the `fi_se` entry point. Each runner also carries its statistics (`currentYear`/`previousYear`/`total`) and bet percentages (per pool type), which are collected alongside the race, runner, and previous-start records.
- `veikkaus_bot/database.py` — DuckDB storage (`Db`) for loading the saved JSON dumps into `card`, `race`, `runner`, `start`, `stat`, and `bet_percentage` tables.

Data is refreshed automatically each day by the `fi-se data load` GitHub Actions workflow (`.github/workflows/fi_se_load.yml`), which runs the fetch, loads the JSON dumps into `data/veikkaus_data.duckdb`, and uploads the resulting `data/` directory (JSON snapshots + the DuckDB database) as a build artifact.

## Development

```bash
make install       # uv sync
make run-tests     # uv run pytest tests
make dist          # build sdist + wheel
```

## License

MIT — see [LICENSE](LICENSE).
