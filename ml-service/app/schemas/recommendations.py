from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="forbid",
    )


class Mood(str, Enum):
    HAPPY = "happy"
    SAD = "sad"
    STRESSED = "stressed"
    RELAXED = "relaxed"
    ROMANTIC = "romantic"
    BORED = "bored"
    EXCITED = "excited"
    CURIOUS = "curious"
    MOTIVATED = "motivated"
    THOUGHTFUL = "thoughtful"
    SUSPENSEFUL = "suspenseful"
    ADVENTUROUS = "adventurous"


class Movie(ApiModel):
    movie_id: int
    title: str
    genres: list[str]
    year: int | None = None
    source: str = "movielens"


class RecommendationItem(Movie):
    score: float = Field(ge=0, le=1)
    explanation: str
    matched_signals: list[str]


class MovieListResponse(ApiModel):
    items: list[Movie]
    total: int
    limit: int
    offset: int


class RecommendationResponse(ApiModel):
    items: list[RecommendationItem]
    total: int
    method: str
    interpretation: list[str]
    metadata_limitations: list[str]
    fallback: bool = False


class MoodRecommendationRequest(ApiModel):
    mood: Mood
    genres: list[str] = Field(default_factory=list, max_length=10)
    exclude_genres: list[str] = Field(default_factory=list, max_length=10)
    exclude_movie_ids: list[int] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=12, ge=1, le=30)
    source: str = Field(default="all", pattern="^(all|bollywood|movielens)$")

    @field_validator("genres", "exclude_genres")
    @classmethod
    def clean_genres(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class SimilarMoviesRequest(ApiModel):
    movie_id: int = Field(gt=0)
    exclude_movie_ids: list[int] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=12, ge=1, le=30)


class SearchRecommendationRequest(ApiModel):
    query: str = Field(min_length=2, max_length=500)
    genres: list[str] = Field(default_factory=list, max_length=10)
    exclude_genres: list[str] = Field(default_factory=list, max_length=10)
    exclude_movie_ids: list[int] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=12, ge=1, le=30)
    source: str = Field(default="all", pattern="^(all|bollywood|movielens)$")

    @field_validator("genres", "exclude_genres")
    @classmethod
    def clean_genres(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class MovieRating(ApiModel):
    movie_id: int = Field(gt=0)
    rating: float = Field(ge=0.5, le=5, multiple_of=0.5)


class ProfileRecommendationRequest(ApiModel):
    ratings: list[MovieRating] = Field(min_length=1, max_length=50)
    exclude_movie_ids: list[int] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=12, ge=1, le=30)

    @field_validator("ratings")
    @classmethod
    def unique_rated_movies(cls, values: list[MovieRating]) -> list[MovieRating]:
        ids = [value.movie_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("ratings must not contain duplicate movie IDs")
        return values

    @field_validator("exclude_movie_ids")
    @classmethod
    def unique_exclusions(cls, values: list[int]) -> list[int]:
        return list(dict.fromkeys(values))


class HybridRecommendationRequest(ProfileRecommendationRequest):
    content_weight: float | None = Field(default=None, ge=0, le=1)


class MovieResponse(ApiModel):
    data: Movie


class MovieListEnvelope(ApiModel):
    data: MovieListResponse


class RecommendationEnvelope(ApiModel):
    data: RecommendationResponse
