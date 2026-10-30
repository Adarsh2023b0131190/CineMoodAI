# CineMood

CineMood is a mood-based movie discovery project. It supports content-based TF-IDF recommendations, item-based collaborative recommendations from MovieLens rating patterns, a configurable hybrid ranker, MongoDB-backed accounts, and persistent user ratings.

## Dataset currently present

- `movies.csv`: MovieLens movie identifiers, titles, and pipe-separated genres.
- `ratings.csv`: MovieLens explicit ratings with user, movie, score, and Unix timestamp. This file is large (about 678 MB); preserve it as a local input and do not load it into a browser or commit it.
- `BollywoodMovieDetail.csv`, `BollywoodActorRanking.csv`, `BollywoodDirectorRanking.csv`: a separate Bollywood metadata/ranking source. Its movie records use IMDb IDs and do not currently provide a verified join to MovieLens `movieId`; its `hitFlop` and ranking fields are not user ratings.

The MovieLens files do not contain plot summaries, language, runtime, mood labels, posters, or cast. Those fields must come from a licensed/authorized metadata provider (planned: TMDB, with server-side credentials and attribution) or remain unavailable. Mood-to-genre presets are explicit heuristics, not verified ground truth. See [`docs/dataset-audit.md`](docs/dataset-audit.md).

## Current implementation

- Architecture: [`docs/architecture.md`](docs/architecture.md).
- FastAPI loads the movie catalog at service startup and caches a sparse TF-IDF matrix. Collaborative filtering can use a versioned production artifact bundle loaded once at startup, without `ratings.csv`; local development retains the legacy CSV-backed cache fallback.
- Express proxies catalog and recommendation endpoints to FastAPI.
- The homepage submits mood selections and natural-language requests, supports account registration/login/logout, restores signed-in sessions, and saves account ratings to MongoDB. Signed-out visitors can still keep a local-only rating profile.
- Still not implemented: verified film metadata enrichment, deployment.

## Local setup

Requirements: Node.js 20+, Python 3.11+, and a running MongoDB server. Copy `backend/.env.example` to `backend/.env`, then set `MONGODB_URI` to your MongoDB database URI. The example intentionally leaves the value blank. Use three terminals from this folder:

```powershell
# Terminal 1
cd backend
npm install
Copy-Item .env.example .env
# Set MONGODB_URI in backend/.env before starting the API.
npm run dev

# Terminal 2
cd ml-service
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Optional offline build: prepare production collaborative artifacts before service startup
python -m app.train_collaborative
python -m uvicorn app.main:app --reload --port 8000

# Terminal 3
cd frontend
npm install
Copy-Item .env.example .env
npm run dev
```

The frontend runs at `http://localhost:5173`, Express at `http://localhost:5000`, and FastAPI at `http://localhost:8000`. The ML service defaults to the repository root for `movies.csv`; when running from `ml-service/`, `DATA_DIR=..` in `.env` resolves to that root. Express reports ML connectivity and catalog statistics via `GET /api/health`.

### Offline collaborative artifact build

For production builds, create a standalone, versioned model bundle on a machine that has the source datasets. From the repository root:

```powershell
cd ml-service
.\.venv\Scripts\Activate.ps1
python -m app.train_collaborative --data-dir .. --output-dir artifacts/collaborative-v1
```

The trainer uses the same ordered MovieLens-plus-Bollywood catalog as the service, and refuses to overwrite an existing output directory. The bundle contains checksummed sparse matrices and a manifest with the artifact/algorithm versions, ordered movie-ID fingerprint, dimensions, and training statistics. Copy the bundle through private artifact storage to the ML service and set `COLLABORATIVE_ARTIFACT_DIR` to its directory. When this variable is set, FastAPI validates and loads the model at startup; missing or incompatible artifacts fail startup with a clear error. Collaborative and hybrid inference then use the loaded matrices without opening `ratings.csv`. The service still needs `movies.csv` and, when Bollywood catalog support is enabled, `BollywoodMovieDetail.csv` for catalog loading and ID-order validation.

Do not commit the generated model bundle or source datasets to Git. The model is sparse, but loading both matrices currently requires roughly 400 MB for their uncompressed arrays, in addition to Python, SciPy, catalog, and TF-IDF memory. Benchmark peak memory on the target host before selecting an instance size.

## Zero-budget deployment configuration

The deployment must not use a paid Render plan or persistent disk. [`render.yaml`](render.yaml) therefore contains only the stateless Express API on Render Free; it takes `ML_SERVICE_URL` and `ML_SERVICE_TOKEN` as dashboard variables. Render Free sleeps after 15 minutes without requests and has no persistent disk, so it is unsuitable for the ML model.

The ML service requires about 616 MiB at startup and roughly 400 MiB for expanded sparse arrays. The viable always-free compute candidate is an Oracle Cloud Always Free Ampere A1 VM (current allowance: 2 OCPUs, 12 GB RAM); availability is not guaranteed and OCI may reclaim idle instances. OCI signup requires card details and may place a temporary verification hold. Do not start signup unless that requirement is acceptable. The model bundle can be stored in OCI's included Always Free object storage, outside GitHub.

Install the ML API on an eligible Ubuntu A1 host using [`deploy/oci/install-ml.sh`](deploy/oci/install-ml.sh) and [`deploy/oci/cinemood-ml.service`](deploy/oci/cinemood-ml.service). Keep the bundle URL, SHA-256 and `ML_SERVICE_TOKEN` in `/etc/cinemood/ml.env` (mode 0600). Configure Caddy from [`deploy/oci/Caddyfile.template`](deploy/oci/Caddyfile.template) with the VM's TLS-enabled hostname; it exposes the ML API under `/ml/*` while Uvicorn listens on loopback. The original `ratings.csv` is not needed by inference.

For Vercel Hobby, configure the frontend root as `frontend/` and set `BACKEND_API_URL` to the Render API HTTPS origin. Vercel proxies `/api/*` to Express, preserving same-origin HttpOnly cookies. Set Render's `FRONTEND_ORIGIN`, `MONGODB_URI`, `ML_SERVICE_URL` (the OCI HTTPS hostname plus `/ml`), and the same `ML_SERVICE_TOKEN` used on OCI. Keep all credentials in provider dashboards or the VM's protected environment file. MongoDB Atlas Free clusters provide 512 MB, which is suitable for a small CineMood account/rating dataset but not large-scale growth.

## Recommendation endpoints

See [`docs/api.md`](docs/api.md). Example PowerShell requests through Express:

```powershell
Invoke-RestMethod http://localhost:5000/api/movies?limit=5
Invoke-RestMethod http://localhost:5000/api/movies/1
Invoke-RestMethod http://localhost:5000/api/recommendations/mood -Method Post -ContentType 'application/json' -Body '{"mood":"happy","limit":5}'
Invoke-RestMethod http://localhost:5000/api/recommendations/similar -Method Post -ContentType 'application/json' -Body '{"movieId":1,"limit":5}'
Invoke-RestMethod http://localhost:5000/api/recommendations/search -Method Post -ContentType 'application/json' -Body '{"query":"a romantic comedy","limit":5}'
Invoke-RestMethod http://localhost:5000/api/recommendations/hybrid -Method Post -ContentType 'application/json' -Body '{"ratings":[{"movieId":1,"rating":4.5}],"limit":5}'
```

## Accounts and saved ratings

- `POST /api/auth/register` with `{ "name", "email", "password" }` creates an account and sets an HttpOnly session cookie. Passwords must be 10–128 characters.
- `POST /api/auth/login` with `{ "email", "password" }` starts a session; `GET /api/auth/me` restores the current user; `POST /api/auth/logout` revokes the session.
- `GET /api/ratings`, `PUT /api/ratings/:movieId` with `{ "rating": 4.5 }`, and `DELETE /api/ratings/:movieId` require a session. Rating ownership is always derived from the cookie.
- `POST /api/recommendations/personalized` requires a session and accepts `{ "method": "hybrid" }` or `{ "method": "collaborative" }`. Express loads the user's saved ratings from MongoDB before calling FastAPI. A new account receives a cold-start response until it has ratings.
- Poster images are looked up server-side through `POST /api/posters/lookup`. Set either `TMDB_API_KEY` to a TMDB v3 API key or `TMDB_ACCESS_TOKEN` to a TMDB API Read Access Token in `backend/.env`; the server uses the correct auth mechanism and neither value enters frontend code. It prefers an exact normalized title and release-year match; if TMDB's release year differs from MovieLens, it accepts only one unambiguous exact-title result. If unset, the UI tells you why the artwork is unavailable. Follow TMDB's attribution requirements before publishing.

Sessions use opaque random tokens in HttpOnly, SameSite=Strict cookies. Only a SHA-256 token hash is stored in MongoDB. Cookie-authenticated writes require an allowed `Origin`; production cookies also set `Secure`. Set `FRONTEND_ORIGIN` to the exact browser origins you use.

## Tests

```powershell
cd ml-service
.\.venv\Scripts\python.exe -m pytest -q

cd ..\backend
npm test

cd ..\frontend
npm run build
```

The ML service unit tests use small fixture catalogs; the collaborative trainer can also build and cache matrices from the full local `ratings.csv`. Ratings use a sparse matrix, user-mean-centered item cosine, minimum co-rater coverage, and score shrinkage. Backend authentication tests connect to an isolated `cinemood_test_<process>` database derived from `MONGODB_URI`; they clean only the test accounts they create and never drop a database or collection.

PowerShell execution policy may block venv activation. In that case run `.venv\Scripts\python.exe -m pip install -r requirements.txt` and `.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000` without activation.

## Data and privacy

Keep local CSVs and credentials out of version control. Copy `.env.example` to `.env` for local configuration. Missing MovieLens interactions remain unknown rather than zero. Visitor ratings are sent as an explicit profile and are never added to MovieLens training labels. No browser `userId` is accepted as authentication.
