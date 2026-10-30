"""Memory-bounded item-based collaborative filtering over MovieLens ratings."""

import json
import logging
import os
import csv
import hashlib
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix, load_npz, save_npz

from app.services.catalog import MovieCatalog
from app.services.recommender import ContentRecommender

logger = logging.getLogger("cinemood.ml.collaborative")
MIN_CO_RATERS = 3
MIN_ITEM_RATINGS = 3
SHRINKAGE = 10.0
ARTIFACT_FORMAT = "cinemood-collaborative"
ARTIFACT_VERSION = 1
ALGORITHM_VERSION = "user-mean-centered-item-cosine-v1"
ARTIFACT_FILES = ("centered-items.npz", "interaction-items.npz", "collaborative-matrices.npz")


class CollaborativeRecommender:
    """User-mean-centered item cosine similarity; missing ratings stay missing."""

    def __init__(self, catalog: MovieCatalog, centered_items, interactions_items, item_norms, item_rating_counts, duplicate_pairs=0, invalid_rows=0):
        self.catalog = catalog
        self.centered_items = centered_items.tocsr()
        self.interactions_items = interactions_items.tocsr()
        self.item_norms = np.asarray(item_norms, dtype=np.float64)
        self.item_rating_counts = np.asarray(item_rating_counts, dtype=np.int64)
        self.movie_ids = np.asarray([movie.movie_id for movie in catalog.movies], dtype=np.int64)
        self.movie_index = catalog.by_id
        self.duplicate_pairs = int(duplicate_pairs)
        self.invalid_rows = int(invalid_rows)
        self.stats = {
            "ratings": int(self.interactions_items.nnz),
            "users": int(self.interactions_items.shape[1]),
            "ratedMovies": int(np.count_nonzero(self.item_rating_counts)),
            "duplicatePairsAveraged": self.duplicate_pairs,
            "invalidRowsSkipped": self.invalid_rows,
        }

    @classmethod
    def from_csv(cls, path: str | Path, catalog: MovieCatalog, artifact_dir: str | Path | None = None):
        """Build sparse matrices in chunks; cache them on disk when a directory is supplied."""
        csv_path = Path(path)
        if artifact_dir:
            configured_artifacts = Path(artifact_dir)
            production_candidates = (configured_artifacts, configured_artifacts / "collaborative-v1")
            for candidate in production_candidates:
                if (candidate / "manifest.json").is_file():
                    return cls.from_artifacts(candidate, catalog)
        if not csv_path.exists():
            raise FileNotFoundError(f"Ratings dataset not found: {csv_path}")

        if artifact_dir:
            artifact = Path(artifact_dir)
            matrices_path = artifact / "collaborative-matrices.npz"
            metadata_path = artifact / "collaborative-metadata.json"
            if matrices_path.exists() and metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    signature = cls._signature(csv_path, catalog)
                    if metadata.get("signature") == signature:
                        with np.load(matrices_path, allow_pickle=False) as matrices:
                            norms = matrices["norms"].copy()
                            counts = matrices["counts"].copy()
                        centered = load_npz(artifact / "centered-items.npz")
                        interactions = load_npz(artifact / "interaction-items.npz")
                        model = cls(catalog, centered, interactions, norms, counts, metadata["duplicatePairsAveraged"], metadata["invalidRowsSkipped"])
                        logger.info("Loaded cached collaborative matrices: %s", model.stats)
                        return model
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    logger.warning("Collaborative model cache is invalid; rebuilding it")
        model = cls._build(csv_path, catalog)
        if artifact_dir:
            model._save(Path(artifact_dir), csv_path)
        return model

    @staticmethod
    def _catalog_ids_digest(catalog: MovieCatalog) -> str:
        ordered_ids = np.asarray([movie.movie_id for movie in catalog.movies], dtype="<i8")
        return hashlib.sha256(ordered_ids.tobytes(order="C")).hexdigest()

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def from_artifacts(cls, artifact_dir: str | Path, catalog: MovieCatalog):
        """Load a versioned production model without opening ratings.csv."""
        artifact = Path(artifact_dir)
        manifest_path = artifact / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Collaborative model manifest is missing: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Collaborative model manifest is unreadable or invalid JSON") from exc

        if not isinstance(manifest, dict) or manifest.get("format") != ARTIFACT_FORMAT:
            raise ValueError("Collaborative model artifact format is not recognized")
        if manifest.get("artifactVersion") != ARTIFACT_VERSION:
            raise ValueError(f"Unsupported collaborative artifact version: {manifest.get('artifactVersion')}")
        if manifest.get("algorithmVersion") != ALGORITHM_VERSION:
            raise ValueError("Collaborative artifact algorithm version is incompatible")
        expected_parameters = {"minCoRaters": MIN_CO_RATERS, "minItemRatings": MIN_ITEM_RATINGS, "shrinkage": SHRINKAGE}
        if manifest.get("parameters") != expected_parameters:
            raise ValueError("Collaborative artifact parameters do not match this service version")

        catalog_meta = manifest.get("catalog")
        if not isinstance(catalog_meta, dict) or catalog_meta.get("movieCount") != len(catalog.movies):
            raise ValueError("Collaborative artifact movie count does not match the loaded catalog")
        if catalog_meta.get("orderedMovieIdsSha256") != cls._catalog_ids_digest(catalog):
            raise ValueError("Collaborative artifact movie ID ordering does not match the loaded catalog")

        file_metadata = manifest.get("files")
        if not isinstance(file_metadata, dict) or set(file_metadata) != set(ARTIFACT_FILES):
            raise ValueError("Collaborative artifact manifest has an incomplete file list")
        for filename in ARTIFACT_FILES:
            file_path = artifact / filename
            entry = file_metadata.get(filename)
            if not isinstance(entry, dict) or not file_path.is_file():
                raise ValueError(f"Collaborative artifact file is missing: {filename}")
            if file_path.stat().st_size != entry.get("bytes"):
                raise ValueError(f"Collaborative artifact file size check failed: {filename}")
            if cls._file_digest(file_path) != entry.get("sha256"):
                raise ValueError(f"Collaborative artifact checksum failed: {filename}")

        model_meta = manifest.get("model")
        if not isinstance(model_meta, dict):
            raise ValueError("Collaborative artifact model metadata is missing")
        try:
            expected_shape = tuple(int(value) for value in model_meta["matrixShape"])
            duplicate_pairs = int(model_meta["duplicatePairsAveraged"])
            invalid_rows = int(model_meta["invalidRowsSkipped"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Collaborative artifact model metadata is invalid") from exc
        if len(expected_shape) != 2 or expected_shape[0] != len(catalog.movies) or min(expected_shape) < 1:
            raise ValueError("Collaborative artifact matrix dimensions are incompatible with the catalog")

        try:
            centered = load_npz(artifact / "centered-items.npz")
            interactions = load_npz(artifact / "interaction-items.npz")
            with np.load(artifact / "collaborative-matrices.npz", allow_pickle=False) as matrices:
                norms = matrices["norms"].copy()
                counts = matrices["counts"].copy()
        except (OSError, ValueError, KeyError, EOFError) as exc:
            raise ValueError("Collaborative matrix artifact is corrupted or unreadable") from exc

        if centered.format != "csr" or interactions.format != "csr":
            raise ValueError("Collaborative matrices must use CSR storage")
        if tuple(centered.shape) != expected_shape or tuple(interactions.shape) != expected_shape:
            raise ValueError("Collaborative matrix dimensions do not match the manifest")
        if centered.nnz != model_meta.get("matrixNnz") or interactions.nnz != model_meta.get("matrixNnz"):
            raise ValueError("Collaborative matrix entry counts do not match the manifest")
        if centered.data.dtype != np.float32 or interactions.data.dtype != np.int32:
            raise ValueError("Collaborative matrix data types are incompatible")
        if norms.shape != (expected_shape[0],) or counts.shape != (expected_shape[0],):
            raise ValueError("Collaborative per-movie statistics have incompatible dimensions")
        if not np.isfinite(norms).all() or (norms < 0).any() or (counts < 0).any():
            raise ValueError("Collaborative per-movie statistics contain invalid values")

        model = cls(catalog, centered, interactions, norms, counts, duplicate_pairs, invalid_rows)
        if model.stats["users"] != expected_shape[1] or model.stats["ratings"] != model_meta.get("matrixNnz"):
            raise ValueError("Collaborative model statistics do not match the manifest")
        logger.info("Loaded production collaborative model artifact: %s", model.stats)
        return model

    def save_production_artifacts(self, artifact_dir: str | Path):
        """Write a verified, immutable artifact bundle; never overwrite a prior bundle."""
        destination = Path(artifact_dir).resolve()
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite existing collaborative artifacts: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{destination.name}.staging-", dir=destination.parent) as staging_name:
            staging = Path(staging_name)
            save_npz(staging / "centered-items.npz", self.centered_items, compressed=True)
            save_npz(staging / "interaction-items.npz", self.interactions_items, compressed=True)
            np.savez_compressed(staging / "collaborative-matrices.npz", norms=self.item_norms, counts=self.item_rating_counts)
            files = {
                filename: {"bytes": (staging / filename).stat().st_size, "sha256": self._file_digest(staging / filename)}
                for filename in ARTIFACT_FILES
            }
            manifest = {
                "format": ARTIFACT_FORMAT,
                "artifactVersion": ARTIFACT_VERSION,
                "algorithmVersion": ALGORITHM_VERSION,
                "parameters": {"minCoRaters": MIN_CO_RATERS, "minItemRatings": MIN_ITEM_RATINGS, "shrinkage": SHRINKAGE},
                "catalog": {
                    "movieCount": len(self.catalog.movies),
                    "orderedMovieIdsSha256": self._catalog_ids_digest(self.catalog),
                },
                "model": {
                    "matrixShape": list(self.centered_items.shape),
                    "matrixNnz": int(self.interactions_items.nnz),
                    "duplicatePairsAveraged": self.duplicate_pairs,
                    "invalidRowsSkipped": self.invalid_rows,
                },
                "files": files,
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            os.rename(staging, destination)
        return destination

    @staticmethod
    def _signature(csv_path: Path, catalog: MovieCatalog) -> dict:
        stat = csv_path.stat()
        return {"size": stat.st_size, "mtimeNs": stat.st_mtime_ns, "movieIds": [movie.movie_id for movie in catalog.movies]}

    @classmethod
    def _build(cls, csv_path: Path, catalog: MovieCatalog):
        required = {"userId", "movieId", "rating", "timestamp"}
        try:
            header = pd.read_csv(csv_path, nrows=0).columns
        except Exception as exc:
            raise ValueError("Could not read the ratings CSV header") from exc
        if not required.issubset(header):
            raise ValueError("ratings.csv must contain userId, movieId, rating, and timestamp columns")

        # Use disk-backed staging arrays so the 25M-row dataset does not need to
        # exist as Python objects or a dense user-by-movie table in RAM.
        line_count = 0
        with csv_path.open("rb") as source:
            for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                line_count += block.count(b"\n")
        with csv_path.open("rb") as source:
            last_byte = source.seek(0, os.SEEK_END)
            source.seek(max(0, last_byte - 1))
            ends_newline = source.read(1) == b"\n"
        capacity = max(0, line_count - 1 + (0 if ends_newline else 1))
        if capacity == 0:
            raise ValueError("ratings.csv contains no rating rows")

        with tempfile.TemporaryDirectory(prefix="cinemood-ratings-") as temporary:
            users = np.memmap(Path(temporary) / "users.i4", dtype=np.int32, mode="w+", shape=(capacity,))
            movie_ids = np.memmap(Path(temporary) / "movies.i4", dtype=np.int32, mode="w+", shape=(capacity,))
            values = np.memmap(Path(temporary) / "ratings.f4", dtype=np.float32, mode="w+", shape=(capacity,))
            used = 0
            invalid = 0
            try:
                chunks = pd.read_csv(csv_path, usecols=["userId", "movieId", "rating"], dtype=str, chunksize=500_000, on_bad_lines="skip")
                for chunk in chunks:
                    user_values = pd.to_numeric(chunk["userId"], errors="coerce").to_numpy(dtype=np.float64)
                    movie_values = pd.to_numeric(chunk["movieId"], errors="coerce").to_numpy(dtype=np.float64)
                    rating_values = pd.to_numeric(chunk["rating"], errors="coerce").to_numpy(dtype=np.float64)
                    valid = np.isfinite(user_values) & np.isfinite(movie_values) & np.isfinite(rating_values)
                    valid &= (user_values > 0) & (movie_values > 0)
                    valid &= (user_values <= np.iinfo(np.int32).max) & (movie_values <= np.iinfo(np.int32).max)
                    valid &= (user_values == np.floor(user_values)) & (movie_values == np.floor(movie_values))
                    valid &= (rating_values >= 0.5) & (rating_values <= 5)
                    invalid += int(len(chunk) - np.count_nonzero(valid))
                    count = int(np.count_nonzero(valid))
                    if not count:
                        continue
                    if used + count > capacity:
                        raise ValueError("ratings.csv has more records than its line count permits")
                    end = used + count
                    users[used:end] = user_values[valid].astype(np.int32)
                    movie_ids[used:end] = movie_values[valid].astype(np.int32)
                    values[used:end] = rating_values[valid].astype(np.float32)
                    used = end
            except Exception as exc:
                if isinstance(exc, ValueError) and "ratings.csv" in str(exc):
                    raise
                raise ValueError("Could not parse ratings.csv") from exc
            if used == 0:
                raise ValueError("ratings.csv contains no valid rating records")

            user_keys = np.unique(users[:used])
            user_rows = np.searchsorted(user_keys, users[:used]).astype(np.int32, copy=False)
            catalog_ids = np.fromiter(catalog.by_id.keys(), dtype=np.int64, count=len(catalog.by_id))
            catalog_indices = np.fromiter(catalog.by_id.values(), dtype=np.int32, count=len(catalog.by_id))
            order = np.argsort(catalog_ids)
            sorted_ids = catalog_ids[order]
            positions = np.searchsorted(sorted_ids, movie_ids[:used])
            known = (positions < len(sorted_ids))
            known &= sorted_ids[np.minimum(positions, len(sorted_ids) - 1)] == movie_ids[:used]
            invalid += int(used - np.count_nonzero(known))
            rows = user_rows[known]
            cols = catalog_indices[order[positions[known]]].astype(np.int32, copy=False)
            rating_values = np.asarray(values[:used][known], dtype=np.float32)
            shape = (len(user_keys), len(catalog.movies))
            user_count = len(user_keys)
            del user_rows, positions, known, users, movie_ids, values, user_keys, catalog_ids, catalog_indices, order, sorted_ids

            rating_sum = coo_matrix((rating_values, (rows, cols)), shape=shape, dtype=np.float32).tocsr()
            count_matrix = coo_matrix((np.ones(len(rows), dtype=np.int32), (rows, cols)), shape=shape, dtype=np.int32).tocsr()
            duplicate_pairs = len(rows) - rating_sum.nnz
            del rows, cols, rating_values
            rating_sum.data /= count_matrix.data
            count_matrix.data[:] = 1
            user_rating_counts = np.diff(rating_sum.indptr)
            user_means = np.divide(
                np.asarray(rating_sum.sum(axis=1)).ravel(),
                user_rating_counts,
                out=np.zeros(user_count, dtype=np.float32),
                where=user_rating_counts > 0,
            )
            centered = rating_sum.copy()
            centered.data -= np.repeat(user_means, user_rating_counts)
            # Keep exact-zero residual entries: they are observed ratings, not missing values.
            del rating_sum
            centered_items = centered.T.tocsr()
            del centered
            interactions_items = count_matrix.T.tocsr()
            del count_matrix
            item_norms = np.sqrt(np.asarray(centered_items.multiply(centered_items).sum(axis=1)).ravel())
            item_counts = np.diff(interactions_items.indptr)
            return cls(catalog, centered_items, interactions_items, item_norms, item_counts, duplicate_pairs, invalid)

    def _save(self, artifact: Path, csv_path: Path):
        artifact.mkdir(parents=True, exist_ok=True)
        save_npz(artifact / "centered-items.npz", self.centered_items, compressed=True)
        save_npz(artifact / "interaction-items.npz", self.interactions_items, compressed=True)
        np.savez_compressed(artifact / "collaborative-matrices.npz", norms=self.item_norms, counts=self.item_rating_counts)
        metadata = {
            "signature": self._signature(csv_path, self.catalog),
            "duplicatePairsAveraged": self.duplicate_pairs,
            "invalidRowsSkipped": self.invalid_rows,
        }
        (artifact / "collaborative-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    def recommend(self, ratings: list[dict], limit: int, exclude_movie_ids: list[int] | None = None) -> dict:
        rated_ids = {int(item["movieId"]) for item in ratings}
        unknown = sorted(rated_ids - set(self.movie_index))
        if unknown:
            return {"notFoundMovieIds": unknown}
        excluded = rated_ids | set(exclude_movie_ids or [])
        n_movies = len(self.catalog.movies)
        accumulated = np.zeros(n_movies, dtype=np.float64)
        total_weight = 0.0
        for item in ratings:
            movie_id = int(item["movieId"])
            seed = self.movie_index[movie_id]
            preference = float(item["rating"]) - 3.0
            if self.item_rating_counts[seed] < MIN_ITEM_RATINGS or self.item_norms[seed] <= 0:
                continue
            seed_vector = self.centered_items.getrow(seed)
            seed_presence = self.interactions_items.getrow(seed)
            overlap = np.asarray((self.interactions_items @ seed_presence.T).toarray()).ravel()
            dot = np.asarray((self.centered_items @ seed_vector.T).toarray()).ravel()
            denominator = self.item_norms * self.item_norms[seed]
            similarity = np.divide(dot, denominator, out=np.zeros_like(dot), where=denominator > 0)
            reliable = overlap >= MIN_CO_RATERS
            similarity[~reliable] = 0.0
            similarity *= overlap / (overlap + SHRINKAGE)
            weight = preference if preference != 0 else 0.1
            accumulated += weight * similarity
            total_weight += abs(weight)

        if total_weight:
            accumulated /= total_weight
        accumulated = np.maximum(accumulated, 0.0)
        for movie_id in excluded:
            index = self.movie_index.get(movie_id)
            if index is not None:
                accumulated[index] = 0.0
        candidates = [index for index, score in enumerate(accumulated) if score > 0]
        candidates.sort(key=lambda index: (-float(accumulated[index]), self.catalog.movies[index].title.casefold(), self.catalog.movies[index].movie_id))
        total_candidates = len(candidates)
        candidates = candidates[:limit]
        max_score = max((float(accumulated[index]) for index in candidates), default=0.0)
        items = []
        for index in candidates:
            movie = self.catalog.movies[index]
            score = float(accumulated[index]) / max_score if max_score else 0.0
            items.append({**movie.as_dict(), "score": round(score, 6), "explanation": "Supported by rating patterns from other MovieLens users who rated similar movies.", "matchedSignals": ["collaborativeItemCosine"]})
        return {
            "items": items,
            "total": total_candidates,
            "method": "collaborative/item-cosine",
            "interpretation": [f"explicitRatings:{len(rated_ids)}", "ratingsMeanCenteredByDatasetUser", f"minimumCoRaters:{MIN_CO_RATERS}"],
            "metadataLimitations": ["Collaborative scores use MovieLens rating patterns only. Bollywood movies have no ratings in the supplied MovieLens ratings file, so they cannot receive collaborative scores; plot, runtime, posters, and verified mood labels are unavailable."],
        }

    def recommend_hybrid(self, ratings: list[dict], content: ContentRecommender, limit: int, content_weight: float = 0.5, exclude_movie_ids: list[int] | None = None) -> dict:
        content_result = content.recommend_profile(ratings, limit=30, exclude_movie_ids=exclude_movie_ids)
        collab_result = self.recommend(ratings, limit=30, exclude_movie_ids=exclude_movie_ids)
        if "notFoundMovieIds" in collab_result:
            return collab_result
        if not collab_result["items"]:
            content_result["method"] = "content-based/profile-fallback"
            content_result["interpretation"].append("collaborativeFallback:insufficientRatingOverlap")
            content_result["fallback"] = True
            return content_result
        cf_weight = 1.0 - content_weight
        content_map = {item["movieId"]: item for item in content_result["items"]}
        collab_map = {item["movieId"]: item for item in collab_result["items"]}
        ids = set(content_map) | set(collab_map)
        combined = []
        for movie_id in ids:
            left, right = content_map.get(movie_id), collab_map.get(movie_id)
            score = (content_weight * (left["score"] if left else 0.0)) + (cf_weight * (right["score"] if right else 0.0))
            if score <= 0:
                continue
            source = left or right
            signals = []
            if left:
                signals.append("contentSimilarity")
            if right:
                signals.append("collaborativeItemCosine")
            combined.append({**source, "score": round(score, 6), "explanation": "Combines title/genre similarity with MovieLens rating patterns.", "matchedSignals": signals})
        combined.sort(key=lambda item: (-item["score"], item["title"].casefold(), item["movieId"]))
        return {
            "items": combined[:limit],
            "total": len(combined),
            "method": "hybrid/content+collaborative",
            "interpretation": [f"explicitRatings:{len(ratings)}", f"contentWeight:{content_weight:.2f}", f"collaborativeWeight:{cf_weight:.2f}"],
            "metadataLimitations": ["Content signals use MovieLens titles and genres plus supplied Bollywood actors, directors, and writers. Collaborative signals use MovieLens ratings; supplied Bollywood records have no ratings in that file."],
        }
