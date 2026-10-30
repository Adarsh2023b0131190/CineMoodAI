import re
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix

from app.schemas.recommendations import Mood
from app.services.catalog import CatalogMovie, MovieCatalog

# Transparent preference presets, not learned or verified emotional labels.
MOOD_GENRES: dict[Mood, tuple[str, ...]] = {
    Mood.HAPPY: ("Comedy", "Animation", "Musical"),
    Mood.SAD: ("Drama", "Romance"),
    Mood.STRESSED: ("Comedy", "Animation", "Musical"),
    Mood.RELAXED: ("Comedy", "Romance", "Documentary"),
    Mood.ROMANTIC: ("Romance",),
    Mood.BORED: ("Adventure", "Action", "Mystery"),
    Mood.EXCITED: ("Action", "Adventure", "Thriller"),
    Mood.CURIOUS: ("Documentary", "Mystery", "Sci-Fi"),
    Mood.MOTIVATED: ("Drama", "Adventure"),
    Mood.THOUGHTFUL: ("Drama", "Mystery", "Film-Noir", "Documentary"),
    Mood.SUSPENSEFUL: ("Thriller", "Mystery", "Crime", "Horror"),
    Mood.ADVENTUROUS: ("Adventure", "Action", "Fantasy"),
}

MOOD_ALIASES = {
    "happy": Mood.HAPPY,
    "cheerful": Mood.HAPPY,
    "sad": Mood.SAD,
    "stressed": Mood.STRESSED,
    "stressful": Mood.STRESSED,
    "relaxed": Mood.RELAXED,
    "relaxing": Mood.RELAXED,
    "romantic": Mood.ROMANTIC,
    "bored": Mood.BORED,
    "excited": Mood.EXCITED,
    "curious": Mood.CURIOUS,
    "motivated": Mood.MOTIVATED,
    "thoughtful": Mood.THOUGHTFUL,
    "suspenseful": Mood.SUSPENSEFUL,
    "adventurous": Mood.ADVENTUROUS,
}

GENRE_ALIASES = {
    "sci fi": "Sci-Fi",
    "science fiction": "Sci-Fi",
    "scifi": "Sci-Fi",
    "film noir": "Film-Noir",
    "noir": "Film-Noir",
    "children's": "Children",
    "kids": "Children",
}
MAX_CANDIDATES = 5_000


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    mood: Mood | None
    genres: tuple[str, ...]
    excluded_genres: tuple[str, ...]
    remaining_text: str
    interpretation: tuple[str, ...]
    source: str = "all"


class ContentRecommender:
    def __init__(self, catalog: MovieCatalog):
        self.catalog = catalog
        self.canonical_genres = {genre.casefold(): genre for genre in catalog.genres}

    @staticmethod
    def _pattern(term: str) -> re.Pattern:
        return re.compile(r"(?<![\w])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![\w])", re.I)

    def parse_query(self, query: str) -> ParsedRequest:
        folded = query.casefold()
        selected_mood = None
        mood_span: tuple[int, int] | None = None
        for term, mood in sorted(MOOD_ALIASES.items(), key=lambda pair: -len(pair[0])):
            match = self._pattern(term).search(folded)
            if match:
                prefix = folded[max(0, match.start() - 16) : match.start()]
                if re.search(r"\b(?:not|never|without|avoid)\s+(?:feeling\s+|very\s+)?$", prefix):
                    continue
                selected_mood, mood_span = mood, match.span()
                break

        found_genres: list[tuple[int, int, str, bool]] = []
        terms = {genre.casefold(): genre for genre in self.catalog.genres}
        terms.update({key: value for key, value in GENRE_ALIASES.items() if value.casefold() in self.canonical_genres})
        for term, canonical in sorted(terms.items(), key=lambda pair: -len(pair[0])):
            for match in self._pattern(term).finditer(folded):
                prefix = folded[max(0, match.start() - 20) : match.start()]
                excluded = bool(re.search(r"\b(?:no|not|without|avoid|exclude|excluding)\s+(?:any\s+)?$", prefix))
                found_genres.append((*match.span(), self.canonical_genres.get(canonical.casefold(), canonical), excluded))
        # Keep the longest mention when aliases overlap (e.g. sci-fi / sci fi).
        found_genres.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        chosen: list[tuple[int, int, str, bool]] = []
        for item in found_genres:
            if any(item[0] < old[1] and old[0] < item[1] for old in chosen):
                continue
            chosen.append(item)
        genres = tuple(dict.fromkeys(canonical for _, _, canonical, negative in chosen if not negative))
        excluded = tuple(dict.fromkeys(canonical for _, _, canonical, negative in chosen if negative))

        source = "bollywood" if re.search(r"\bbollywood\b", folded) else "all"
        interpreted: list[str] = []
        if source != "all":
            interpreted.append("source:bollywood")
        if selected_mood:
            interpreted.append(f"mood:{selected_mood.value}")
        if genres:
            interpreted.append("genres:" + ",".join(genres))
        if excluded:
            interpreted.append("excludedGenres:" + ",".join(excluded))

        spans = [item[:2] for item in chosen]
        if mood_span:
            spans.append(mood_span)
        remaining = list(query)
        for start, end in spans:
            remaining[start:end] = " " * (end - start)
        remaining_text = re.sub(r"\b(?:movie|movies|film|films|please|recommend|recommendation|suggest|want|looking|find|show|me|for|in|a|the|with|to|watch)\b", " ", "".join(remaining), flags=re.I)
        remaining_text = " ".join(remaining_text.split())
        return ParsedRequest(selected_mood, genres, excluded, remaining_text, tuple(interpreted), source)

    def recommend_mood(
        self,
        mood: Mood,
        limit: int,
        genres: list[str] | None = None,
        exclude_genres: list[str] | None = None,
        exclude_movie_ids: list[int] | None = None,
        source: str = "all",
    ) -> dict:
        target_genres = self._canonicalize_genres((*MOOD_GENRES[mood], *(genres or [])))
        excluded = set(self._canonicalize_genres(exclude_genres or []))
        available = [genre for genre in target_genres if genre.casefold() in self.canonical_genres]
        interpretation = [f"mood:{mood.value}", "moodPresetGenres:" + ",".join(available)]
        if genres:
            interpretation.append("preferredGenres:" + ",".join(self._canonicalize_genres(genres)))
        return self._rank(
            query=" ".join(available),
            target_genres=available,
            excluded_genres=excluded,
            excluded_ids=set(exclude_movie_ids or []),
            limit=limit,
            interpretation=interpretation,
            explanation_prefix="Mood preset matched catalog genres",
            source_filter=source,
        )

    def recommend_similar(self, movie_id: int, limit: int, exclude_movie_ids: list[int] | None = None) -> dict:
        index = self.catalog.index_for_id(movie_id)
        if index is None:
            return {"notFound": True}
        source = self.catalog.movies[index]
        excluded = set(exclude_movie_ids or []) | {movie_id}
        scores = self.catalog.matrix[index].dot(self.catalog.matrix.T)
        content_scores = np.asarray(scores.toarray()).ravel()
        candidates = [i for i, score in enumerate(content_scores) if score > 0 and self.catalog.movies[i].movie_id not in excluded]
        ranked = sorted(candidates, key=lambda i: (-float(content_scores[i]), self.catalog.movies[i].title.casefold(), self.catalog.movies[i].movie_id))[:MAX_CANDIDATES]
        items = []
        for i in ranked[:limit]:
            movie = self.catalog.movies[i]
            shared = [genre for genre in movie.genres if genre in source.genres]
            items.append(self._item(movie, float(content_scores[i]), f"Similar title and catalog genres to {source.title}.", ["tfidfCosine", *(f"genre:{g}" for g in shared)]))
        return self._response(items, len(candidates), "content-based/tfidf-similarity", [f"similarTo:{movie_id}"])

    def recommend_profile(self, ratings: list[dict], limit: int, exclude_movie_ids: list[int] | None = None) -> dict:
        """Recommend catalog-near movies from titles the viewer explicitly rated."""
        rated_ids = {int(item["movieId"]) for item in ratings}
        excluded = rated_ids | set(exclude_movie_ids or [])
        seeds = []
        for item in ratings:
            index = self.catalog.index_for_id(int(item["movieId"]))
            if index is not None:
                seeds.append((index, max(float(item["rating"]) - 2.5, 0.25)))
        if not seeds:
            return self._response([], 0, "content-based/profile-fallback", ["profileHasNoCatalogMovies"])

        scores = np.zeros(len(self.catalog.movies), dtype=np.float32)
        weight_total = sum(weight for _, weight in seeds)
        for index, weight in seeds:
            row = self.catalog.matrix[index]
            scores += (np.asarray((row @ self.catalog.matrix.T).toarray()).ravel() * weight)
        scores /= weight_total

        candidates = [i for i, score in enumerate(scores) if score > 0 and self.catalog.movies[i].movie_id not in excluded]
        candidates.sort(key=lambda i: (-float(scores[i]), self.catalog.movies[i].title.casefold(), self.catalog.movies[i].movie_id))
        top = candidates[:limit]
        max_score = max((float(scores[i]) for i in top), default=1.0)
        items = []
        for index in top:
            movie = self.catalog.movies[index]
            score = float(scores[index]) / max_score
            items.append(self._item(movie, score, "Title and available catalog metadata are similar to movies you rated.", ["contentSimilarity"]))
        return self._response(items, len(candidates), "content-based/profile", [f"ratedMovies:{len(rated_ids)}"])

    def recommend_search(
        self,
        query: str,
        limit: int,
        genres: list[str] | None = None,
        exclude_genres: list[str] | None = None,
        exclude_movie_ids: list[int] | None = None,
        source: str = "all",
    ) -> dict:
        parsed = self.parse_query(query)
        source = parsed.source if source == "all" and parsed.source != "all" else source
        mood_genres = MOOD_GENRES.get(parsed.mood, ()) if parsed.mood else ()
        target_genres = self._canonicalize_genres((*mood_genres, *parsed.genres, *(genres or [])))
        excluded_genres = set(self._canonicalize_genres((*parsed.excluded_genres, *(exclude_genres or []))))
        available = [genre for genre in target_genres if genre.casefold() in self.canonical_genres]
        interpretation = list(parsed.interpretation)
        if parsed.mood:
            interpretation.append("moodPresetGenres:" + ",".join(g for g in mood_genres if g.casefold() in self.canonical_genres))
        if genres:
            interpretation.append("preferredGenres:" + ",".join(self._canonicalize_genres(genres)))

        query_text = parsed.remaining_text
        if not query_text and available:
            query_text = " ".join(available)
        return self._rank(
            query=query_text,
            target_genres=available,
            excluded_genres=excluded_genres,
            excluded_ids=set(exclude_movie_ids or []),
            limit=limit,
            interpretation=interpretation,
            explanation_prefix="Matched request",
            source_filter=source,
        )

    def _canonicalize_genres(self, genres) -> list[str]:
        canonical = []
        for genre in genres:
            raw = str(genre).strip()
            mapped = GENRE_ALIASES.get(raw.casefold(), raw)
            actual = self.canonical_genres.get(mapped.casefold())
            if actual and actual not in canonical:
                canonical.append(actual)
        return canonical

    def _rank(
        self,
        query: str,
        target_genres: list[str],
        excluded_genres: set[str],
        excluded_ids: set[int],
        limit: int,
        interpretation: list[str],
        explanation_prefix: str,
        source_filter: str = "all",
    ) -> dict:
        query_vector = self.catalog.vector_for_text(query) if query.strip() else csr_matrix((1, self.catalog.matrix.shape[1]), dtype=np.float32)
        raw_scores = np.asarray((query_vector @ self.catalog.matrix.T).toarray()).ravel()
        target_folded = {g.casefold() for g in target_genres}
        excluded_folded = {g.casefold() for g in excluded_genres}
        candidates: list[tuple[int, float, float, list[str]]] = []
        for index, movie in enumerate(self.catalog.movies):
            if source_filter != "all" and movie.source != source_filter:
                continue
            if movie.movie_id in excluded_ids:
                continue
            movie_genres = {genre.casefold() for genre in movie.genres}
            if movie_genres & excluded_folded:
                continue
            matched = sorted((self.canonical_genres[genre] for genre in movie_genres & target_folded), key=str.casefold)
            if target_folded and not matched:
                continue
            similarity = float(raw_scores[index])
            if not target_folded and similarity <= 0:
                continue
            genre_score = len(matched) / len(target_folded) if target_folded else 0.0
            score = (0.55 * genre_score + 0.45 * similarity) if target_folded else similarity
            candidates.append((index, score, similarity, matched))

        # Bound serialized results and avoid all-pairs similarity computation. A query
        # vector is compared with catalog rows once; tied scores use stable title/ID order.
        candidates.sort(key=lambda row: (-row[1], -row[2], self.catalog.movies[row[0]].title.casefold(), self.catalog.movies[row[0]].movie_id))
        candidate_total = len(candidates)
        candidates = candidates[:MAX_CANDIDATES]
        items = []
        for index, score, similarity, matched in candidates[:limit]:
            movie = self.catalog.movies[index]
            signal_labels = ["tfidfCosine"] if similarity > 0 else []
            signal_labels.extend(f"genre:{genre}" for genre in matched)
            if matched:
                explanation = f"{explanation_prefix}: {', '.join(matched)} catalog genre(s)."
            elif similarity > 0:
                explanation = f"Title and genre text is similar to your request (TF-IDF cosine {similarity:.2f})."
            else:
                explanation = "Matches the available catalog genre preference."
            items.append(self._item(movie, score, explanation, signal_labels))
        return self._response(items, candidate_total, "content-based/tfidf-cosine", interpretation)

    @staticmethod
    def _item(movie: CatalogMovie, score: float, explanation: str, signals: list[str]) -> dict:
        return {**movie.as_dict(), "score": round(max(0.0, min(1.0, score)), 6), "explanation": explanation, "matchedSignals": signals}

    @staticmethod
    def _response(items: list[dict], total: int, method: str, interpretation: list[str]) -> dict:
        return {
            "items": items,
            "total": total,
            "method": method,
            "interpretation": interpretation,
            "metadataLimitations": ["MovieLens entries use title and genre fields; Bollywood entries also use the supplied actors, directors, and writers. Plot, runtime, language, posters, and verified mood labels are unavailable."],
        }
