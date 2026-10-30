import os
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.recommendations import (
    MovieListEnvelope,
    MovieListResponse,
    MovieResponse,
    MoodRecommendationRequest,
    HybridRecommendationRequest,
    ProfileRecommendationRequest,
    RecommendationEnvelope,
    RecommendationResponse,
    SearchRecommendationRequest,
    SimilarMoviesRequest,
)
from app.services.catalog import MovieCatalog
from app.services.collaborative import CollaborativeRecommender
from app.services.recommender import ContentRecommender

load_dotenv()
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts"
logger = logging.getLogger("cinemood.ml")


def create_app(
    data_dir: str | Path | None = None,
    catalog: MovieCatalog | None = None,
    collaborative: CollaborativeRecommender | None = None,
    collaborative_artifact_dir: str | Path | None = None,
) -> FastAPI:
    model_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(target_app: FastAPI):
        if data_dir is None:
            configured = Path(os.getenv("DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
            configured = configured if configured.is_absolute() else (Path.cwd() / configured).resolve()
        else:
            configured = Path(data_dir).expanduser().resolve()
        loaded_catalog = catalog
        if loaded_catalog is None:
            loaded_catalog = MovieCatalog.from_csv(configured / "movies.csv", configured / "BollywoodMovieDetail.csv")
        target_app.state.catalog = loaded_catalog
        target_app.state.recommender = ContentRecommender(loaded_catalog)
        target_app.state.ratings_path = configured / "ratings.csv"
        target_app.state.collaborative_model = collaborative
        target_app.state.collaborative_load_failed = False
        configured_artifacts = Path(os.getenv("ARTIFACT_DIR", str(DEFAULT_ARTIFACT_DIR))).expanduser()
        target_app.state.collaborative_artifacts = configured_artifacts if configured_artifacts.is_absolute() else (Path.cwd() / configured_artifacts).resolve()
        explicit_artifact_dir = collaborative_artifact_dir or os.getenv("COLLABORATIVE_ARTIFACT_DIR")
        if target_app.state.collaborative_model is None:
            if explicit_artifact_dir:
                artifact_path = Path(explicit_artifact_dir).expanduser()
                if not artifact_path.is_absolute():
                    artifact_path = (Path.cwd() / artifact_path).resolve()
                try:
                    target_app.state.collaborative_model = CollaborativeRecommender.from_artifacts(artifact_path, loaded_catalog)
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"Could not load configured collaborative artifact at {artifact_path}: {exc}") from exc
            elif data_dir is None:
                default_production_artifact = target_app.state.collaborative_artifacts / "collaborative-v1"
                if (default_production_artifact / "manifest.json").is_file():
                    try:
                        target_app.state.collaborative_model = CollaborativeRecommender.from_artifacts(default_production_artifact, loaded_catalog)
                    except (OSError, ValueError) as exc:
                        raise RuntimeError(f"Could not load collaborative artifact at {default_production_artifact}: {exc}") from exc
        yield

    def get_collaborative(target_app: FastAPI) -> CollaborativeRecommender | None:
        if target_app.state.collaborative_load_failed:
            return None
        if target_app.state.collaborative_model is None:
            with model_lock:
                if target_app.state.collaborative_model is None:
                    try:
                        ratings_path = target_app.state.ratings_path
                        if not ratings_path.is_file():
                            raise FileNotFoundError("ratings.csv is not configured")
                        target_app.state.collaborative_model = CollaborativeRecommender.from_csv(
                            ratings_path,
                            target_app.state.catalog,
                            artifact_dir=target_app.state.collaborative_artifacts,
                        )
                    except Exception as exc:
                        logger.warning("Collaborative rating data is unavailable; profile requests will use content fallback: %s", type(exc).__name__)
                        target_app.state.collaborative_load_failed = True
                        return None
        return target_app.state.collaborative_model

    app = FastAPI(title="CineMood ML Service", version="0.2.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed", "details": details}})

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict):
            error = exc.detail
        else:
            error = {"code": "NOT_FOUND" if exc.status_code == 404 else "REQUEST_ERROR", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": error}, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception):
        logger.exception("Unhandled ML service error", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}})

    @app.get("/health")
    def health(request: Request) -> dict:
        return {
            "status": "ok",
            "service": "cinemood-ml",
            "catalog": request.app.state.catalog.stats(),
            "collaborative": "loaded" if request.app.state.collaborative_model is not None else ("unavailable" if request.app.state.collaborative_load_failed or not request.app.state.ratings_path.is_file() else "available"),
        }

    @app.get("/movies", response_model=MovieListEnvelope)
    def list_movies(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 24,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ):
        catalog: MovieCatalog = request.app.state.catalog
        items = [movie.as_dict() for movie in catalog.list_movies(limit, offset)]
        return {"data": {"items": items, "total": len(catalog.movies), "limit": limit, "offset": offset}}

    @app.get("/movies/{movie_id}", response_model=MovieResponse)
    def get_movie(request: Request, movie_id: Annotated[int, PathParam(gt=0)]):
        catalog: MovieCatalog = request.app.state.catalog
        index = catalog.index_for_id(movie_id)
        if index is None:
            raise HTTPException(status_code=404, detail={"code": "MOVIE_NOT_FOUND", "message": "Movie was not found in the catalog"})
        return {"data": catalog.movies[index].as_dict()}

    @app.post("/recommendations/mood", response_model=RecommendationEnvelope)
    def recommend_by_mood(request: Request, payload: MoodRecommendationRequest):
        result = request.app.state.recommender.recommend_mood(
            payload.mood,
            payload.limit,
            payload.genres,
            payload.exclude_genres,
            payload.exclude_movie_ids,
            payload.source,
        )
        return {"data": result}

    @app.post("/recommendations/similar", response_model=RecommendationEnvelope)
    def recommend_similar(request: Request, payload: SimilarMoviesRequest):
        result = request.app.state.recommender.recommend_similar(payload.movie_id, payload.limit, payload.exclude_movie_ids)
        if result.get("notFound"):
            raise HTTPException(status_code=404, detail={"code": "MOVIE_NOT_FOUND", "message": "Movie was not found in the catalog"})
        return {"data": result}

    @app.post("/recommendations/search", response_model=RecommendationEnvelope)
    def recommend_from_search(request: Request, payload: SearchRecommendationRequest):
        result = request.app.state.recommender.recommend_search(
            payload.query,
            payload.limit,
            payload.genres,
            payload.exclude_genres,
            payload.exclude_movie_ids,
            payload.source,
        )
        return {"data": result}

    def profile_dicts(target_app: FastAPI, payload):
        for rated in payload.ratings:
            if target_app.state.catalog.index_for_id(rated.movie_id) is None:
                raise HTTPException(status_code=404, detail={"code": "MOVIE_NOT_FOUND", "message": f"Rated movie {rated.movie_id} was not found in the catalog"})
        return [{"movieId": rated.movie_id, "rating": rated.rating} for rated in payload.ratings]

    @app.post("/recommendations/collaborative", response_model=RecommendationEnvelope)
    def recommend_collaborative(request: Request, payload: ProfileRecommendationRequest):
        ratings = profile_dicts(request.app, payload)
        model = get_collaborative(request.app)
        result = model.recommend(ratings, payload.limit, payload.exclude_movie_ids) if model else {"items": []}
        if "notFoundMovieIds" in result:
            raise HTTPException(status_code=404, detail={"code": "MOVIE_NOT_FOUND", "message": "A rated movie was not found in the catalog"})
        if not result["items"]:
            result = request.app.state.recommender.recommend_profile(ratings, payload.limit, payload.exclude_movie_ids)
            result["method"] = "content-based/profile-fallback"
            result["fallback"] = True
            reason = "ratingDataUnavailable" if request.app.state.collaborative_load_failed else "insufficientRatingOverlap"
            result["interpretation"].append(f"collaborativeFallback:{reason}")
        return {"data": result}

    @app.post("/recommendations/hybrid", response_model=RecommendationEnvelope)
    def recommend_hybrid(request: Request, payload: HybridRecommendationRequest):
        ratings = profile_dicts(request.app, payload)
        model = get_collaborative(request.app)
        content_weight = payload.content_weight
        if content_weight is None:
            try:
                content_weight = min(1.0, max(0.0, float(os.getenv("HYBRID_CONTENT_WEIGHT", "0.5"))))
            except ValueError:
                content_weight = 0.5
        if model:
            result = model.recommend_hybrid(ratings, request.app.state.recommender, payload.limit, content_weight, payload.exclude_movie_ids)
            if "notFoundMovieIds" in result:
                raise HTTPException(status_code=404, detail={"code": "MOVIE_NOT_FOUND", "message": "A rated movie was not found in the catalog"})
        else:
            result = request.app.state.recommender.recommend_profile(ratings, payload.limit, payload.exclude_movie_ids)
            result["method"] = "content-based/profile-fallback"
            result["fallback"] = True
            result["interpretation"].append("collaborativeFallback:ratingDataUnavailable")
        return {"data": result}

    return app


app = create_app()
