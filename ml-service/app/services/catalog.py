import csv
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

_YEAR_SUFFIX = re.compile(r"\((\d{4})\)\s*$")
_NO_GENRES = "(no genres listed)"
_BOLLYWOOD_ID_BASE = 1_000_000_000


@dataclass(frozen=True, slots=True)
class CatalogMovie:
    movie_id: int
    title: str
    genres: tuple[str, ...]
    year: int | None
    source: str = "movielens"
    metadata_text: str = ""

    def as_dict(self) -> dict:
        return {
            "movieId": self.movie_id,
            "title": self.title,
            "genres": list(self.genres),
            "year": self.year,
            "source": self.source,
        }


class MovieCatalog:
    """MovieLens catalog and a single cached sparse TF-IDF representation."""

    def __init__(self, movies: list[CatalogMovie], duplicate_rows: int = 0):
        if not movies:
            raise ValueError("The movie catalog contains no usable rows")
        self.movies = movies
        self.by_id = {movie.movie_id: index for index, movie in enumerate(movies)}
        self.duplicate_rows = duplicate_rows
        self.genres = sorted({genre for movie in movies for genre in movie.genres}, key=str.casefold)
        # Genres are repeated once to give this only structured descriptive field
        # more influence than the title. No plot or mood labels are fabricated.
        documents = [self._document(movie) for movie in movies]
        self.vectorizer = TfidfVectorizer(
            stop_words="english",
            ngram_range=(1, 2),
            max_features=50_000,
            sublinear_tf=True,
            dtype=np.float32,
        )
        self.matrix = self.vectorizer.fit_transform(documents)

    @classmethod
    def from_csv(cls, path: str | Path, bollywood_path: str | Path | None = None) -> "MovieCatalog":
        csv_path = Path(path)
        movies: list[CatalogMovie] = []
        seen: set[int] = set()
        duplicate_rows = 0
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            expected = {"movieId", "title", "genres"}
            if not reader.fieldnames or not expected.issubset(reader.fieldnames):
                raise ValueError("movies.csv must contain movieId, title, and genres columns")
            for row in reader:
                raw_id = (row.get("movieId") or "").strip()
                title = (row.get("title") or "").strip()
                try:
                    movie_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if movie_id <= 0 or not title:
                    continue
                if movie_id in seen:
                    duplicate_rows += 1
                    continue
                seen.add(movie_id)
                raw_genres = (row.get("genres") or "").strip()
                genres = tuple(
                    dict.fromkeys(
                        genre.strip()
                        for genre in raw_genres.split("|")
                        if genre.strip() and genre.strip().casefold() != _NO_GENRES
                    )
                )
                year_match = _YEAR_SUFFIX.search(title)
                year = int(year_match.group(1)) if year_match else None
                movies.append(CatalogMovie(movie_id, title, genres, year))
        if bollywood_path is not None and Path(bollywood_path).is_file():
            with Path(bollywood_path).open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                expected = {"imdbId", "title", "releaseYear", "genre"}
                if not reader.fieldnames or not expected.issubset(reader.fieldnames):
                    raise ValueError("BollywoodMovieDetail.csv must contain imdbId, title, releaseYear, and genre columns")
                seen_bollywood: set[int] = set()
                for row in reader:
                    imdb_id = (row.get("imdbId") or "").strip().casefold()
                    title = (row.get("title") or "").strip()
                    suffix = imdb_id[2:] if imdb_id.startswith("tt") else ""
                    if not suffix.isdigit() or not title:
                        continue
                    movie_id = _BOLLYWOOD_ID_BASE + int(suffix)
                    if movie_id in seen or movie_id in seen_bollywood:
                        duplicate_rows += 1
                        continue
                    seen_bollywood.add(movie_id)
                    raw_genres = (row.get("genre") or "").strip()
                    genres = tuple(dict.fromkeys(g.strip() for g in raw_genres.split("|") if g.strip() and g.strip().casefold() != _NO_GENRES))
                    try:
                        year = int((row.get("releaseYear") or "").strip())
                    except ValueError:
                        year = None
                    metadata_text = " ".join((row.get("actors") or "", row.get("directors") or "", row.get("writers") or "")).strip()
                    movies.append(CatalogMovie(movie_id, title, genres, year, "bollywood", metadata_text))
        return cls(movies, duplicate_rows=duplicate_rows)

    @staticmethod
    def _document(movie: CatalogMovie) -> str:
        genres = " ".join(movie.genres)
        return f"{movie.title} {genres} {genres} {movie.metadata_text}".strip()

    def vector_for_text(self, text: str):
        return self.vectorizer.transform([text])

    def index_for_id(self, movie_id: int) -> int | None:
        return self.by_id.get(movie_id)

    def list_movies(self, limit: int, offset: int) -> list[CatalogMovie]:
        return self.movies[offset : offset + limit]

    def stats(self) -> dict[str, int]:
        return {
            "movies": len(self.movies),
            "genres": len(self.genres),
            "moviesWithoutGenres": sum(not movie.genres for movie in self.movies),
            "duplicateRowsSkipped": self.duplicate_rows,
            "bollywoodMovies": sum(movie.source == "bollywood" for movie in self.movies),
        }
