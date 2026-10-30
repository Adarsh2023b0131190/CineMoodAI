# API contract

Express prefixes browser-facing routes with `/api`. FastAPI is an internal service; Express proxies these requests using `ML_SERVICE_URL`, a 10-second timeout for catalog/content calls and up to 120 seconds for the first collaborative model load, with sanitized upstream errors.

| Method and route | Request | Purpose |
|---|---|---|
| `POST /api/auth/register` | `{ "name": "…", "email": "…", "password": "10+ characters" }` | Create a normalized unique account and issue an HttpOnly session cookie |
| `POST /api/auth/login` | `{ "email": "…", "password": "…" }` | Verify credentials and issue a session cookie |
| `GET /api/auth/me` | cookie | Return current user or `{ "authenticated": false, "user": null }` |
| `POST /api/auth/logout` | cookie | Revoke the current session and clear its cookie |
| `GET /api/ratings` | session cookie | List ratings owned by the current user |
| `PUT /api/ratings/:movieId` | `{ "rating": 4.5 }` + session | Create or update the authenticated user's rating |
| `DELETE /api/ratings/:movieId` | session cookie | Delete only the authenticated user's rating |
| `GET /api/health` | — | Express and FastAPI health; includes loaded catalog statistics |
| `GET /api/movies?limit=24&offset=0` | query: limit 1–100, offset 0–100000 | Paginated MovieLens catalog |
| `GET /api/movies/:movieId` | positive integer path ID | One catalog item; 404 if absent |
| `POST /api/posters/lookup` | `{ "movies": [{ "movieId": 1, "title": "Toy Story (1995)", "year": 1995 }] }` | Server-side TMDB lookup; prefers exact normalized title/year matches, with a unique exact-title fallback if provider years differ |
| `POST /api/recommendations/mood` | `{ "mood": "happy", "limit": 12, "genres": [], "excludeGenres": [], "excludeMovieIds": [] }` | Rank by a supported mood preset and optional genre preferences |
| `POST /api/recommendations/similar` | `{ "movieId": 1, "limit": 12, "excludeMovieIds": [] }` | Find title/genre TF-IDF neighbors; seed is always excluded |
| `POST /api/recommendations/search` | `{ "query": "a romantic comedy", "limit": 12 }` | Extract known mood/genre cues and rank using available text |
| `POST /api/recommendations/collaborative` | `{ "ratings": [{"movieId": 1, "rating": 4.5}], "excludeMovieIds": [], "limit": 12 }` | Item-based collaborative scores from MovieLens co-rating patterns |
| `POST /api/recommendations/hybrid` | Same rating profile; optional `contentWeight` from 0 to 1 | Combine normalized title/genre and rating-pattern scores; default content weight is `HYBRID_CONTENT_WEIGHT` or `0.5` |
| `POST /api/recommendations/personalized` | session; `{ "method": "hybrid" }` or `{ "method": "collaborative" }` | Load up to 50 saved ratings from MongoDB and call the selected ML method; browser-supplied user IDs/ratings are not used |

Success responses use `{ "data": ... }`. Recommendation `data` contains `items`, `total`, `method`, `interpretation`, `metadataLimitations`, and `fallback`. Each item includes `movieId`, `title`, `genres`, `year`, `score` (0–1 ranking score, not a probability), `explanation`, and `matchedSignals`. Rating profiles accept 1–50 unique catalog movies with explicit half-star ratings from 0.5 to 5. Rated movies are automatically excluded from results. Public collaborative/hybrid endpoints accept explicit profiles but reject `userId`; the authenticated personalized endpoint instead loads the current account's ratings from MongoDB. Empty matches return HTTP 200. If the co-rating coverage is insufficient, collaborative requests return a content-based profile fallback and set `fallback: true`. Accounts with no saved ratings get an empty cold-start response with an explanation. Invalid requests return 400 from Express (422 directly from FastAPI); unknown movie IDs return 404. Upstream timeouts and failures are mapped to safe JSON errors without stack traces.

Passwords are scrypt-hashed. Session cookies are HttpOnly, SameSite=Strict, scoped to `/api`, and Secure in production. Only a hash of each random session token is persisted; sessions expire after seven days and use a MongoDB TTL index. Browser CORS credentials are enabled only for configured `FRONTEND_ORIGIN` values (with local Vite host aliases). Cookie-authenticated writes require a matching trusted Origin.

Supported moods: `happy`, `sad`, `stressed`, `relaxed`, `romantic`, `bored`, `excited`, `curious`, `motivated`, `thoughtful`, `suspenseful`, and `adventurous`. Mood presets map to available MovieLens genres as heuristics. They do not assert that a movie has a verified emotional label. See [`dataset-audit.md`](dataset-audit.md) for data coverage and limitations.

The collaborative model mean-centers observed ratings per MovieLens user, computes item cosine similarities, requires at least three co-raters, and shrinks weak overlaps. Missing rating cells are not filled. Generated sparse matrices are cached under the ignored `ml-service/artifacts/` directory. Refresh the cache by deleting those generated artifacts and running `python -m app.train_collaborative` from `ml-service/` after changing the source ratings file.
