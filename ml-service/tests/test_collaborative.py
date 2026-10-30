import csv
import json
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.catalog import MovieCatalog
from app.services.collaborative import CollaborativeRecommender
from app.services.recommender import ContentRecommender
from app.train_collaborative import main as train_collaborative


@pytest.fixture
def project_data(tmp_path):
    movies = [
        {"movieId": "1", "title": "Comet Journey (2001)", "genres": "Adventure|Sci-Fi"},
        {"movieId": "2", "title": "Comet Voyage (2002)", "genres": "Adventure|Sci-Fi"},
        {"movieId": "3", "title": "Quiet Hearts (2003)", "genres": "Drama|Romance"},
        {"movieId": "4", "title": "Untested Archive (2004)", "genres": "Mystery"},
        {"movieId": "5", "title": "Comet Echo (2005)", "genres": "Adventure|Sci-Fi"},
    ]
    with (tmp_path / "movies.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["movieId", "title", "genres"])
        writer.writeheader()
        writer.writerows(movies)

    rows = []
    profiles = [
        (5, 5, 1), (5, 5, 1), (4, 5, 2), (4, 4, 1), (5, 5, 1),
    ]
    for user_id, (one, two, three) in enumerate(profiles, start=1):
        for movie_id, rating in ((1, one), (2, two), (3, three)):
            rows.append({"userId": user_id, "movieId": movie_id, "rating": rating, "timestamp": 1})
    # A duplicate pair is averaged, and a missing rating is dropped rather than imputed.
    rows.extend([
        {"userId": 1, "movieId": 1, "rating": 3, "timestamp": 2},
        {"userId": 99, "movieId": 3, "rating": "", "timestamp": 3},
        {"userId": 100, "movieId": 999, "rating": 5, "timestamp": 4},
        {"userId": 102, "movieId": 2.5, "rating": 4, "timestamp": 6},
    ])
    with (tmp_path / "ratings.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["userId", "movieId", "rating", "timestamp"])
        writer.writeheader()
        writer.writerows(rows)
    return tmp_path


@pytest.fixture
def system(project_data):
    catalog = MovieCatalog.from_csv(project_data / "movies.csv")
    model = CollaborativeRecommender.from_csv(project_data / "ratings.csv", catalog)
    return catalog, model, ContentRecommender(catalog)


def test_sparse_preprocessing_averages_duplicates_and_drops_missing_and_unknown_movies(project_data):
    catalog = MovieCatalog.from_csv(project_data / "movies.csv")
    model = CollaborativeRecommender.from_csv(project_data / "ratings.csv", catalog)
    assert model.stats["duplicatePairsAveraged"] == 1
    assert model.stats["invalidRowsSkipped"] == 3
    assert model.stats["ratings"] == 15
    assert model.interactions_items.nnz == 15
    assert model.item_rating_counts[catalog.index_for_id(5)] == 0


def test_cached_sparse_model_reloads_with_identical_recommendations(project_data):
    catalog = MovieCatalog.from_csv(project_data / "movies.csv")
    artifacts = project_data / "artifacts"
    built = CollaborativeRecommender.from_csv(project_data / "ratings.csv", catalog, artifact_dir=artifacts)
    cached = CollaborativeRecommender.from_csv(project_data / "ratings.csv", catalog, artifact_dir=artifacts)
    profile = [{"movieId": 1, "rating": 5}]
    assert built.recommend(profile, 5) == cached.recommend(profile, 5)


def test_production_artifact_loads_without_ratings_csv_and_preserves_movie_ids(project_data):
    bollywood_path = project_data / "BollywoodMovieDetail.csv"
    with bollywood_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["imdbId", "title", "releaseYear", "genre"])
        writer.writeheader()
        writer.writerow({"imdbId": "tt0123456", "title": "Bollywood Catalog Test", "releaseYear": "2001", "genre": "Drama"})
    catalog = MovieCatalog.from_csv(project_data / "movies.csv", bollywood_path)
    built = CollaborativeRecommender.from_csv(project_data / "ratings.csv", catalog)
    profile = [{"movieId": 1, "rating": 5}]
    built_cf = built.recommend(profile, 5)
    content = ContentRecommender(catalog)
    built_hybrid = built.recommend_hybrid(profile, content, 5, content_weight=0.7)
    bundle = project_data / "bundles" / "collaborative-v1"
    built.save_production_artifacts(bundle)

    (project_data / "ratings.csv").unlink()
    loaded = CollaborativeRecommender.from_artifacts(bundle, catalog)
    assert loaded.recommend(profile, 5) == built_cf
    assert loaded.recommend_hybrid(profile, content, 5, content_weight=0.7) == built_hybrid
    assert [movie.movie_id for movie in loaded.catalog.movies] == [movie.movie_id for movie in catalog.movies]
    assert loaded.catalog.movies[-1].movie_id == 1_000_123_456

    app = create_app(data_dir=project_data, collaborative_artifact_dir=bundle)
    with TestClient(app) as client:
        assert client.get("/health").json()["collaborative"] == "loaded"
        collaborative = client.post("/recommendations/collaborative", json={"ratings": profile, "limit": 5})
        hybrid = client.post("/recommendations/hybrid", json={"ratings": profile, "contentWeight": 0.7, "limit": 5})
        assert collaborative.status_code == 200
        assert collaborative.json()["data"]["items"][0]["movieId"] == 2
        assert hybrid.status_code == 200
        assert hybrid.json()["data"]["method"] == "hybrid/content+collaborative"


def test_production_artifact_rejects_missing_corrupt_and_incompatible_bundles(project_data):
    catalog = MovieCatalog.from_csv(project_data / "movies.csv")
    model = CollaborativeRecommender.from_csv(project_data / "ratings.csv", catalog)
    bundle = project_data / "production-artifact"

    with pytest.raises(FileNotFoundError, match="manifest is missing"):
        CollaborativeRecommender.from_artifacts(bundle, catalog)
    with pytest.raises(RuntimeError, match="Could not load configured collaborative artifact.*manifest is missing"):
        with TestClient(create_app(data_dir=project_data, collaborative_artifact_dir=bundle)):
            pass

    model.save_production_artifacts(bundle)
    corrupted = bundle / "centered-items.npz"
    with corrupted.open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ValueError, match="size check failed"):
        CollaborativeRecommender.from_artifacts(bundle, catalog)

    # Rebuild into a distinct directory; bundles are immutable and never overwritten.
    compatible_bundle = project_data / "production-artifact-compatible"
    model.save_production_artifacts(compatible_bundle)
    reordered_catalog = MovieCatalog(list(reversed(catalog.movies)))
    with pytest.raises(ValueError, match="movie ID ordering"):
        CollaborativeRecommender.from_artifacts(compatible_bundle, reordered_catalog)


def test_production_artifact_refuses_to_overwrite_existing_directory(project_data):
    catalog = MovieCatalog.from_csv(project_data / "movies.csv")
    model = CollaborativeRecommender.from_csv(project_data / "ratings.csv", catalog)
    destination = project_data / "immutable-bundle"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        model.save_production_artifacts(destination)
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_offline_training_command_builds_matching_bollywood_catalog_bundle(project_data, monkeypatch):
    bollywood_path = project_data / "BollywoodMovieDetail.csv"
    with bollywood_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["imdbId", "title", "releaseYear", "genre"])
        writer.writeheader()
        writer.writerow({"imdbId": "tt0123456", "title": "Bollywood Catalog Test", "releaseYear": "2001", "genre": "Drama"})
    output = project_data / "offline-output" / "collaborative-v1"
    monkeypatch.setattr(sys, "argv", ["train_collaborative", "--data-dir", str(project_data), "--output-dir", str(output)])

    train_collaborative()
    catalog = MovieCatalog.from_csv(project_data / "movies.csv", bollywood_path)
    loaded = CollaborativeRecommender.from_artifacts(output, catalog)
    assert loaded.catalog.movies[-1].movie_id == 1_000_123_456

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifactVersion"] += 1
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported collaborative artifact version"):
        CollaborativeRecommender.from_artifacts(output, catalog)


def test_empty_ratings_dataset_is_rejected_without_creating_scores(project_data):
    catalog = MovieCatalog.from_csv(project_data / "movies.csv")
    path = project_data / "empty-ratings.csv"
    path.write_text("userId,movieId,rating,timestamp\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no rating rows"):
        CollaborativeRecommender.from_csv(path, catalog)


def test_item_cf_ranks_rated_together_titles_and_excludes_profile_movies(system):
    _catalog, model, _content = system
    result = model.recommend([{"movieId": 1, "rating": 5}], limit=10)
    assert result["method"] == "collaborative/item-cosine"
    assert result["items"]
    assert result["items"][0]["movieId"] == 2
    assert all(item["movieId"] != 1 for item in result["items"])
    assert all("collaborativeItemCosine" in item["matchedSignals"] for item in result["items"])


def test_collaborative_excludes_explicitly_rated_and_extra_movies(system):
    _catalog, model, _content = system
    result = model.recommend([{"movieId": 1, "rating": 5}], limit=10, exclude_movie_ids=[2])
    assert all(item["movieId"] not in {1, 2} for item in result["items"])


def test_cold_start_falls_back_to_content_and_hybrid_weights_are_configurable(system):
    catalog, model, content = system
    cold = model.recommend_hybrid([{"movieId": 5, "rating": 5}], content, limit=5, content_weight=0.7)
    assert cold["method"] == "content-based/profile-fallback"
    assert cold["fallback"] is True
    assert all(item["movieId"] != 5 for item in cold["items"])

    hybrid = model.recommend_hybrid([{"movieId": 1, "rating": 5}], content, limit=5, content_weight=0.8)
    assert hybrid["method"] == "hybrid/content+collaborative"
    assert "contentWeight:0.80" in hybrid["interpretation"]
    assert "collaborativeWeight:0.20" in hybrid["interpretation"]
    assert all(item["movieId"] != 1 for item in hybrid["items"])
    assert "contentSimilarity" in hybrid["items"][0]["matchedSignals"]


def test_fastapi_collaborative_hybrid_validation_and_content_regression(project_data):
    catalog = MovieCatalog.from_csv(project_data / "movies.csv")
    model = CollaborativeRecommender.from_csv(project_data / "ratings.csv", catalog)
    app = create_app(data_dir=project_data, collaborative=model)
    with TestClient(app) as client:
        collab = client.post("/recommendations/collaborative", json={"ratings": [{"movieId": 1, "rating": 5}]})
        assert collab.status_code == 200
        assert collab.json()["data"]["items"][0]["movieId"] == 2
        assert collab.json()["data"]["method"] == "collaborative/item-cosine"

        hybrid = client.post("/recommendations/hybrid", json={"ratings": [{"movieId": 1, "rating": 5}], "contentWeight": 0.7})
        assert hybrid.status_code == 200
        assert hybrid.json()["data"]["method"] == "hybrid/content+collaborative"

        assert client.post("/recommendations/collaborative", json={"ratings": [{"movieId": 999, "rating": 5}]}).status_code == 404
        assert client.post("/recommendations/collaborative", json={"ratings": [{"movieId": 1, "rating": 5}, {"movieId": 1, "rating": 4}]}).status_code == 422
        assert client.post("/recommendations/hybrid", json={"ratings": [{"movieId": 1, "rating": 6}]}).status_code == 422

        content = client.post("/recommendations/similar", json={"movieId": 1})
        assert content.status_code == 200
        assert content.json()["data"]["method"] == "content-based/tfidf-similarity"


def test_fastapi_falls_back_when_ratings_dataset_is_unavailable(project_data):
    (project_data / "ratings.csv").unlink()
    with TestClient(create_app(data_dir=project_data)) as client:
        response = client.post("/recommendations/hybrid", json={"ratings": [{"movieId": 1, "rating": 5}]})
        assert response.status_code == 200
        assert response.json()["data"]["method"] == "content-based/profile-fallback"
        assert response.json()["data"]["fallback"] is True
        assert "collaborativeFallback:ratingDataUnavailable" in response.json()["data"]["interpretation"]
