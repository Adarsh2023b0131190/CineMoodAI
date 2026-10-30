import csv

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.recommendations import Mood
from app.services.catalog import MovieCatalog
from app.services.recommender import ContentRecommender


@pytest.fixture
def catalog_file(tmp_path):
    rows = [
        {"movieId": "1", "title": "My Funny Date (2001)", "genres": "Comedy|Romance"},
        {"movieId": "2", "title": "A Funny Summer (2005)", "genres": "Comedy"},
        {"movieId": "3", "title": "Lost Hearts (2002)", "genres": "Romance|Drama"},
        {"movieId": "4", "title": "Galactic Quest (2010)", "genres": "Sci-Fi|Adventure"},
        {"movieId": "5", "title": "Space Odyssey (2001)", "genres": "Adventure|Sci-Fi"},
        {"movieId": "6", "title": "Quiet Mystery (2003)", "genres": "Mystery|Thriller"},
        {"movieId": "7", "title": "Untyped Catalog Entry (2008)", "genres": "(no genres listed)"},
        {"movieId": "8", "title": "Quiet Mystery (2003)", "genres": "Mystery|Thriller"},
        {"movieId": "bad", "title": "Invalid ID", "genres": "Comedy"},
        {"movieId": "9", "title": "", "genres": "Comedy"},
    ]
    path = tmp_path / "movies.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["movieId", "title", "genres"])
        writer.writeheader()
        writer.writerows(rows)
        writer.writerow({"movieId": "1", "title": "Duplicate ID must be skipped", "genres": "Horror"})
    return tmp_path


@pytest.fixture
def catalog(catalog_file):
    return MovieCatalog.from_csv(catalog_file / "movies.csv")


def test_catalog_preprocessing_skips_invalid_and_duplicate_ids_and_keeps_missing_genres(catalog):
    assert len(catalog.movies) == 8
    assert catalog.duplicate_rows == 1
    assert catalog.movies[0].genres == ("Comedy", "Romance")
    assert catalog.movies[0].year == 2001
    assert catalog.movies[6].genres == ()
    assert catalog.stats()["moviesWithoutGenres"] == 1


def test_empty_movie_catalog_is_rejected(tmp_path):
    path = tmp_path / "movies.csv"
    path.write_text("movieId,title,genres\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no usable rows"):
        MovieCatalog.from_csv(path)


def test_mood_ranking_uses_catalog_genres_and_excludes_selected_movies(catalog):
    engine = ContentRecommender(catalog)
    first = engine.recommend_mood(Mood.HAPPY, 10, exclude_movie_ids=[1])
    second = engine.recommend_mood(Mood.HAPPY, 10, exclude_movie_ids=[1])
    assert first == second
    assert first["items"]
    assert all(item["movieId"] != 1 for item in first["items"])
    assert all("Comedy" in item["genres"] for item in first["items"])
    assert all("genre:Comedy" in item["matchedSignals"] for item in first["items"])


def test_similar_movies_uses_tfidf_and_never_returns_seed(catalog):
    result = ContentRecommender(catalog).recommend_similar(4, 10)
    assert result["items"]
    assert all(item["movieId"] != 4 for item in result["items"])
    assert "tfidfCosine" in result["items"][0]["matchedSignals"]


def test_query_parser_extracts_supported_mood_genres_and_exclusions(catalog):
    parsed = ContentRecommender(catalog).parse_query("I feel romantic and like sci-fi, without comedy")
    assert parsed.mood.value == "romantic"
    assert parsed.genres == ("Sci-Fi",)
    assert parsed.excluded_genres == ("Comedy",)


def test_unknown_query_returns_empty_instead_of_unrelated_movies(catalog):
    result = ContentRecommender(catalog).recommend_search("zzqv unlisted theme", 10)
    assert result["items"] == []
    assert result["total"] == 0


def test_mood_without_matching_metadata_returns_empty(catalog):
    result = ContentRecommender(catalog).recommend_mood(Mood.ROMANTIC, 10, exclude_genres=["Romance"])
    assert result["items"] == []


def test_fastapi_response_shape_validation_and_not_found(catalog_file):
    app = create_app(data_dir=catalog_file)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["catalog"]["movies"] == 8

        movies = client.get("/movies?limit=2&offset=0")
        assert movies.status_code == 200
        assert movies.json()["data"]["items"][0]["movieId"] == 1
        assert "year" in movies.json()["data"]["items"][0]

        recommendation = client.post("/recommendations/mood", json={"mood": "happy", "limit": 2})
        assert recommendation.status_code == 200
        body = recommendation.json()["data"]
        assert body["method"] == "content-based/tfidf-cosine"
        assert body["items"][0]["movieId"] in {1, 2}
        assert "explanation" in body["items"][0]

        assert client.post("/recommendations/mood", json={"mood": "unknown"}).status_code == 422
        invalid = client.post("/recommendations/search", json={"query": "x"})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
        assert client.get("/movies/999").status_code == 404
        assert client.post("/recommendations/similar", json={"movieId": 999}).status_code == 404


def test_service_token_protects_catalog_and_recommendations_but_keeps_health_public(catalog_file):
    app = create_app(data_dir=catalog_file, service_token="test-internal-token")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/movies").status_code == 401
        assert client.post("/recommendations/mood", json={"mood": "happy"}).status_code == 401
        headers = {"x-ml-service-token": "test-internal-token"}
        assert client.get("/movies", headers=headers).status_code == 200
        assert client.post("/recommendations/mood", json={"mood": "happy"}, headers=headers).status_code == 200


def test_movies_without_feature_metadata_are_not_used_as_recommendations(catalog):
    result = ContentRecommender(catalog).recommend_mood(
        Mood.HAPPY,
        10,
    )
    assert all(item["movieId"] != 7 for item in result["items"])


def test_bollywood_dataset_is_loaded_with_disjoint_ids_and_mood_filter(catalog_file):
    bollywood_path = catalog_file / "BollywoodMovieDetail.csv"
    with bollywood_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["imdbId", "title", "releaseYear", "genre", "actors", "directors", "writers"])
        writer.writeheader()
        writer.writerow({"imdbId": "tt0123456", "title": "Bollywood Funny Date", "releaseYear": "2001", "genre": "Comedy | Romance", "actors": "Actor A", "directors": "Director B", "writers": "Writer C"})
    catalog = MovieCatalog.from_csv(catalog_file / "movies.csv", bollywood_path)
    movie = next(movie for movie in catalog.movies if movie.source == "bollywood")
    assert movie.movie_id == 1_000_123_456
    assert movie.movie_id not in {item.movie_id for item in catalog.movies if item.source == "movielens"}
    assert movie.genres == ("Comedy", "Romance")
    assert catalog.stats()["bollywoodMovies"] == 1

    engine = ContentRecommender(catalog)
    filtered = engine.recommend_mood(Mood.HAPPY, 10, source="bollywood")
    assert filtered["items"]
    assert all(item["source"] == "bollywood" for item in filtered["items"])
    natural = engine.recommend_search("Bollywood happy movie", 10)
    assert natural["items"]
    assert all(item["source"] == "bollywood" for item in natural["items"])


def test_fastapi_loads_bollywood_dataset_and_validates_source(catalog_file):
    with (catalog_file / "BollywoodMovieDetail.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["imdbId", "title", "releaseYear", "genre"])
        writer.writeheader()
        writer.writerow({"imdbId": "tt0123456", "title": "Bollywood Funny Date", "releaseYear": "2001", "genre": "Comedy | Romance"})
    app = create_app(data_dir=catalog_file)
    with TestClient(app) as client:
        assert client.get("/health").json()["catalog"]["bollywoodMovies"] == 1
        response = client.post("/recommendations/mood", json={"mood": "happy", "source": "bollywood", "limit": 10})
        assert response.status_code == 200
        assert response.json()["data"]["items"]
        assert all(item["source"] == "bollywood" for item in response.json()["data"]["items"])
        invalid = client.post("/recommendations/mood", json={"mood": "happy", "source": "tollywood"})
        assert invalid.status_code == 422
