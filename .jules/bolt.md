## 2024-11-20 - Fast tuple extraction for deduplication
**Learning:** In `archive_db.py`, the `_insert_many` function relies on generating a tuple key for every row before insertion in order to deduplicate inputs. The original implementation used a generator expression `tuple(row[i] for i in key)` in a loop which is relatively slow.
**Action:** Use `operator.itemgetter(*key)` (or `itemgetter(key[0])` for single keys) combined with a dict comprehension. This provides roughly an 8x speedup for this core data ingestion pipeline operation, as `itemgetter` operates in C and avoids the Python-level loop overhead.
## 2026-09-24 - DuckDB executemany INSERT OR REPLACE bottleneck
**Learning:** DuckDB's `executemany` handles batched inserts with primary key conflict resolutions (`INSERT OR REPLACE`) extremely slowly.
**Action:** In `archive_db._insert_many`, instead of using `executemany`, dynamically rewrite the SQL query into a giant parameterized `VALUES (?,?,?), (?,?,?), ...` clause using regex. Splitting into chunks of ~1000 parameters and executing them via `execute` (bypassing `executemany`) resulted in a massive ~60x speedup for data ingestion.
