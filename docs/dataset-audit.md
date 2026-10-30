# Dataset audit

Inspected on 2026-10-21 by streaming the CSV files. The 678 MB ratings file was not loaded into memory. Duplicate rating pairs were counted exactly with a temporary SQLite primary-key index, which was removed when the scan finished.

## `movies.csv`

- Size: 3,038,099 bytes.
- Header: `movieId,title,genres`.
- Rows: 62,423.
- Missing values in required columns: 0.
- Invalid/non-positive movie IDs: 0.
- Duplicate `movieId` rows: 0.
- Rows with `(no genres listed)`: 5,062. These records are retained in catalog listing, but have no genre features and therefore cannot match a genre-based mood preset.
- Example: `movieId=1`, title `Toy Story (1995)`, genres `Adventure|Animation|Children|Comedy|Fantasy`.

## `ratings.csv`

- Size: 678,260,987 bytes.
- Header: `userId,movieId,rating,timestamp`.
- Rows: 25,000,095.
- Missing values in required columns: 0.
- Invalid records under positive integer IDs and rating range 0–5: 0.
- Duplicate `(userId,movieId)` pairs: 0.
- Distinct users: 162,541; distinct rated movie IDs: 59,047.
- Rating range: 0.5–5.0; timestamp range: 789,652,009–1,574,327,703 (Unix seconds).
- Ratings reference a movie ID in `movies.csv`: all rows (0 unknown references).

## Modeling limitations

This content-based phase reads only `movies.csv`; it does not use `ratings.csv`. Catalog content consists of title and pipe-delimited genres. There are no plot descriptions, keywords, cast, crew, poster paths, language, runtime, or verified mood labels in these files. Release year is parsed only when a terminal `(YYYY)` is present in the supplied title. A mood-to-genre preset is a transparent product heuristic, not a MovieLens label or learned emotional classification.
