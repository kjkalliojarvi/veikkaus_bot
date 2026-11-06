## 2024-11-20 - Fast tuple extraction for deduplication
**Learning:** In `archive_db.py`, the `_insert_many` function relies on generating a tuple key for every row before insertion in order to deduplicate inputs. The original implementation used a generator expression `tuple(row[i] for i in key)` in a loop which is relatively slow.
**Action:** Use `operator.itemgetter(*key)` (or `itemgetter(key[0])` for single keys) combined with a dict comprehension. This provides roughly an 8x speedup for this core data ingestion pipeline operation, as `itemgetter` operates in C and avoids the Python-level loop overhead.
## 2026-09-24 - DuckDB executemany INSERT OR REPLACE bottleneck
**Learning:** DuckDB's `executemany` handles batched inserts with primary key conflict resolutions (`INSERT OR REPLACE`) extremely slowly.
**Action:** In `archive_db._insert_many`, instead of using `executemany`, dynamically rewrite the SQL query into a giant parameterized `VALUES (?,?,?), (?,?,?), ...` clause using regex. Splitting into chunks of ~1000 parameters and executing them via `execute` (bypassing `executemany`) resulted in a massive ~60x speedup for data ingestion.
## 2024-11-20 - Fast JSON deserialization for large payload collections
**Learning:** The project relies heavily on crawling and processing very large JSON payloads (e.g., multi-leg pools with thousands of runners) using the standard `json` library, which resulted in a parsing performance bottleneck during operations like backfilling and parsing multi-leg percentages.
**Action:** Replace `import json` with `import orjson` everywhere large API responses and gzipped archives are loaded (`crawler.py`, `fetcher.py`, `longshots.py`). Reading gzipped files directly in binary mode (`'rb'`) for `orjson.loads(f.read())` yields roughly a 2x-4x speedup over standard `json.load()` when ingesting bulk datasets.

## 2024-05-13 - datetime.strptime parsing overhead
**Learning:** `datetime.strptime()` has significant constant parsing overhead for short strings like dates.
**Action:** When parsing a huge volume of highly uniform date strings on the hot path (like `'dd.mm.yy'` in `parse_meet_date`), check for the exact expected string length and character positions. By manually slicing and falling back to `datetime.date(y, m, d)`, we can see up to a ~3x performance boost over directly invoking `datetime.strptime()`. Wait to fall back to `strptime` for inputs that don't match the fast path criteria.
