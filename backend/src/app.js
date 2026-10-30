import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import rateLimit from 'express-rate-limit';
import axios from 'axios';
import { MovieRating, Session, User } from './models.js';
import { createAuth } from './auth.js';

const SUPPORTED_MOODS = new Set([
  'happy', 'sad', 'stressed', 'relaxed', 'romantic', 'bored', 'excited',
  'curious', 'motivated', 'thoughtful', 'suspenseful', 'adventurous',
]);
const DEFAULT_ML_URL = process.env.ML_SERVICE_URL || 'http://localhost:8000';
const POSTER_CACHE_VERSION = 'v3';
const POSTER_CACHE_MAX_ENTRIES = 2_000;
const POSTER_CACHE_SUCCESS_TTL_MS = 30 * 24 * 60 * 60 * 1000;
const POSTER_CACHE_MISS_TTL_MS = 6 * 60 * 60 * 1000;
const POSTER_LOOKUP_CONCURRENCY = 1;

function errorResponse(res, status, code, message, details) {
  return res.status(status).json({ error: { code, message, ...(details ? { details } : {}) } });
}

function positiveInt(value) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function queryInt(value, fallback, min, max) {
  if (value === undefined) return fallback;
  const number = Number(value);
  return Number.isInteger(number) && number >= min && number <= max ? number : null;
}

function validateBody(body, kind) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return 'Request body must be a JSON object';
  const limit = body.limit === undefined ? 12 : Number(body.limit);
  if (!Number.isInteger(limit) || limit < 1 || limit > 30) return 'limit must be an integer from 1 to 30';
  const arrays = ['genres', 'excludeGenres', 'excludeMovieIds'];
  for (const key of arrays) {
    if (body[key] === undefined) continue;
    if (!Array.isArray(body[key]) || body[key].length > (key === 'excludeMovieIds' ? 100 : 10)) return `${key} must be a valid short list`;
    if (key === 'excludeMovieIds' && body[key].some(value => !positiveInt(value))) return 'excludeMovieIds must contain positive integer IDs';
    if (key !== 'excludeMovieIds' && body[key].some(value => typeof value !== 'string' || value.trim().length > 60)) return `${key} must contain genre names`;
  }
  if (kind === 'mood' && (typeof body.mood !== 'string' || !SUPPORTED_MOODS.has(body.mood.trim().toLowerCase()))) return 'mood must be one of the supported mood values';
  if (['mood', 'search'].includes(kind) && body.source !== undefined && !['all', 'bollywood', 'movielens'].includes(body.source)) return 'source must be all, bollywood, or movielens';
  if (kind === 'similar' && !positiveInt(body.movieId)) return 'movieId must be a positive integer';
  if (kind === 'search' && (typeof body.query !== 'string' || body.query.trim().length < 2 || body.query.length > 500)) return 'query must contain between 2 and 500 characters';
  if (kind === 'collaborative' || kind === 'hybrid') {
    if (Object.hasOwn(body, 'userId')) return 'userId is not accepted; send explicit movie ratings instead';
    if (!Array.isArray(body.ratings) || body.ratings.length < 1 || body.ratings.length > 50) return 'ratings must contain between 1 and 50 explicit movie ratings';
    const ids = new Set();
    for (const item of body.ratings) {
      if (!item || typeof item !== 'object' || !positiveInt(item.movieId)) return 'each rating must include a positive movieId';
      if (ids.has(Number(item.movieId))) return 'ratings must not repeat movie IDs';
      ids.add(Number(item.movieId));
      const rating = Number(item.rating);
      if (!Number.isFinite(rating) || rating < 0.5 || rating > 5 || !Number.isInteger(rating * 2)) return 'ratings must use half-star values from 0.5 to 5';
    }
    if (kind === 'hybrid' && body.contentWeight !== undefined) {
      const weight = Number(body.contentWeight);
      if (!Number.isFinite(weight) || weight < 0 || weight > 1) return 'contentWeight must be between 0 and 1';
    }
  }
  return null;
}

function publicMlError(error, res) {
  if (axios.isAxiosError(error) && error.response) {
    const status = error.response.status;
    if (status === 404) return errorResponse(res, 404, 'MOVIE_NOT_FOUND', 'Movie was not found in the catalog');
    if (status === 422 || status === 400) return errorResponse(res, 422, 'INVALID_ML_REQUEST', 'The recommendation request was not accepted');
  }
  if (axios.isAxiosError(error) && (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT')) {
    return errorResponse(res, 504, 'ML_SERVICE_TIMEOUT', 'The recommendation service took too long to respond');
  }
  return errorResponse(res, 502, 'ML_SERVICE_UNAVAILABLE', 'The recommendation service is unavailable');
}

export function createApp({ mlServiceUrl = DEFAULT_ML_URL, mlTimeoutMs = 10_000, frontendOrigin = process.env.FRONTEND_ORIGIN || 'http://localhost:5173', models = {}, tmdbApiKey = process.env.TMDB_API_KEY || '', tmdbAccessToken = process.env.TMDB_ACCESS_TOKEN || '', tmdbBaseUrl = 'https://api.themoviedb.org/3', tmdbTimeoutMs = 6000 } = {}) {
  const app = express();
  const mlClient = axios.create({ baseURL: mlServiceUrl.replace(/\/$/, ''), timeout: mlTimeoutMs, headers: { Accept: 'application/json' } });
  const tmdbClient = axios.create({
    baseURL: tmdbBaseUrl,
    timeout: tmdbTimeoutMs,
    headers: { Accept: 'application/json', ...(tmdbAccessToken ? { Authorization: `Bearer ${tmdbAccessToken}` } : {}) },
  });
  const posterCache = new Map();
  const getCachedPoster = key => {
    const cached = posterCache.get(key);
    if (!cached || cached.expiresAt <= Date.now()) {
      posterCache.delete(key);
      return { hit: false };
    }
    // Refresh insertion order so frequently requested titles remain in the bounded cache.
    posterCache.delete(key);
    posterCache.set(key, cached);
    return { hit: true, posterUrl: cached.posterUrl };
  };
  const setCachedPoster = (key, posterUrl) => {
    posterCache.delete(key);
    posterCache.set(key, {
      posterUrl,
      expiresAt: Date.now() + (posterUrl ? POSTER_CACHE_SUCCESS_TTL_MS : POSTER_CACHE_MISS_TTL_MS),
    });
    while (posterCache.size > POSTER_CACHE_MAX_ENTRIES) posterCache.delete(posterCache.keys().next().value);
  };
  const allowedOrigins = new Set(String(frontendOrigin).split(',').map(origin => origin.trim()).filter(Boolean));
  const { User: UserModel = User, MovieRating: RatingModel = MovieRating, Session: SessionModel = Session } = models;
  const auth = createAuth({ UserModel, SessionModel });
  for (const origin of [...allowedOrigins]) {
    try {
      const parsed = new URL(origin);
      if (parsed.port === '5173' && parsed.hostname === 'localhost') allowedOrigins.add(`${parsed.protocol}//127.0.0.1:${parsed.port}`);
      if (parsed.port === '5173' && parsed.hostname === '127.0.0.1') allowedOrigins.add(`${parsed.protocol}//localhost:${parsed.port}`);
    } catch { /* Invalid entries simply cannot match a browser Origin. */ }
  }

  app.use(helmet());
  app.use(cors({
    origin(origin, callback) {
      if (!origin || allowedOrigins.has(origin)) return callback(null, true);
      return callback(null, false);
    },
    credentials: true,
  }));
  app.use(express.json({ limit: '32kb' }));
  app.use('/api', rateLimit({ windowMs: 60_000, limit: 120, standardHeaders: 'draft-7', legacyHeaders: false }));

  app.use('/api', (req, res, next) => {
    if (!['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method)) return next();
    if (!req.path.startsWith('/auth/') && !req.path.startsWith('/ratings')) return next();
    const origin = req.get('origin');
    if (!origin || !allowedOrigins.has(origin)) return errorResponse(res, 403, 'UNTRUSTED_ORIGIN', 'A trusted frontend origin is required');
    return next();
  });

  app.post('/api/auth/register', auth.register);
  app.post('/api/auth/login', auth.login);
  app.post('/api/auth/logout', auth.logout);
  app.get('/api/auth/me', auth.currentUser);

  app.get('/api/ratings', auth.requireUser, async (req, res, next) => {
    try {
      const ratings = await RatingModel.find({ userId: req.user._id }).select('movieId rating -_id').sort({ movieId: 1 }).lean();
      return res.json({ data: { items: ratings } });
    } catch (error) { return next(error); }
  });

  app.put('/api/ratings/:movieId', auth.requireUser, async (req, res, next) => {
    const movieId = positiveInt(req.params.movieId);
    const rating = Number(req.body?.rating);
    if (!movieId || !Number.isFinite(rating) || rating < 0.5 || rating > 5 || !Number.isInteger(rating * 2)) {
      return errorResponse(res, 400, 'INVALID_RATING', 'movieId must be positive and rating must be a half-star value from 0.5 to 5');
    }
    try {
      await mlClient.get(`/movies/${movieId}`);
      const saved = await RatingModel.findOneAndUpdate(
        { userId: req.user._id, movieId },
        { $set: { rating } },
        { returnDocument: 'after', upsert: true, runValidators: true, setDefaultsOnInsert: true },
      ).select('movieId rating -_id').lean();
      return res.json({ data: { item: saved } });
    } catch (error) {
      if (error?.response?.status === 404) return errorResponse(res, 404, 'MOVIE_NOT_FOUND', 'Movie was not found in the catalog');
      if (error?.name === 'ValidationError' || error?.name === 'CastError') return errorResponse(res, 400, 'INVALID_RATING', 'Rating data was not valid');
      if (error?.code === 11000) return errorResponse(res, 409, 'RATING_CONFLICT', 'This rating was updated concurrently; retry the request');
      if (axios.isAxiosError(error)) return publicMlError(error, res);
      return next(error);
    }
  });

  app.delete('/api/ratings/:movieId', auth.requireUser, async (req, res, next) => {
    const movieId = positiveInt(req.params.movieId);
    if (!movieId) return errorResponse(res, 400, 'INVALID_MOVIE_ID', 'movieId must be a positive integer');
    try {
      const result = await RatingModel.deleteOne({ userId: req.user._id, movieId });
      return res.json({ data: { deleted: result.deletedCount === 1 } });
    } catch (error) { return next(error); }
  });

  app.post('/api/recommendations/personalized', auth.requireUser, async (req, res, next) => {
    try {
      const method = req.body?.method || 'hybrid';
      if (!['hybrid', 'collaborative'].includes(method)) return errorResponse(res, 400, 'INVALID_REQUEST', 'method must be hybrid or collaborative');
      const limit = req.body?.limit === undefined ? 12 : Number(req.body.limit);
      if (!Number.isInteger(limit) || limit < 1 || limit > 30) return errorResponse(res, 400, 'INVALID_REQUEST', 'limit must be an integer from 1 to 30');
      const ratings = await RatingModel.find({ userId: req.user._id }).select('movieId rating -_id').sort({ updatedAt: -1 }).limit(50).lean();
      if (!ratings.length) return res.json({ data: { items: [], total: 0, method: 'cold-start', fallback: true, message: 'Rate a few movies to get personalized recommendations.' } });
      const response = await mlClient.post(`/recommendations/${method}`, { ratings, limit }, { timeout: Math.max(mlTimeoutMs, 120_000) });
      return res.json(response.data);
    } catch (error) {
      if (axios.isAxiosError(error)) return publicMlError(error, res);
      return next(error);
    }
  });

  app.get('/api/health', async (_req, res) => {
    let ml = { status: 'unavailable' };
    try {
      const response = await mlClient.get('/health', { timeout: 1500 });
      ml = response.data;
    } catch { /* Report dependency availability without crashing the API. */ }
    const status = ml.status === 'ok' ? 'ok' : 'degraded';
    res.status(status === 'ok' ? 200 : 503).json({ data: { status, service: 'cinemood-api', dependencies: { ml } } });
  });

  app.get('/api/movies', async (req, res) => {
    const limit = queryInt(req.query.limit, 24, 1, 100);
    const offset = queryInt(req.query.offset, 0, 0, 100_000);
    if (limit === null || offset === null) return errorResponse(res, 400, 'INVALID_QUERY', 'limit or offset is outside the allowed range');
    try {
      const response = await mlClient.get('/movies', { params: { limit, offset } });
      return res.json(response.data);
    } catch (error) { return publicMlError(error, res); }
  });

  app.get('/api/movies/:movieId', async (req, res) => {
    const movieId = positiveInt(req.params.movieId);
    if (!movieId) return errorResponse(res, 400, 'INVALID_MOVIE_ID', 'movieId must be a positive integer');
    try {
      const response = await mlClient.get(`/movies/${movieId}`);
      return res.json(response.data);
    } catch (error) { return publicMlError(error, res); }
  });

  app.post('/api/posters/lookup', async (req, res) => {
    const movies = req.body?.movies;
    if (!Array.isArray(movies) || movies.length < 1 || movies.length > 30) {
      return errorResponse(res, 400, 'INVALID_REQUEST', 'movies must contain between 1 and 30 catalog titles');
    }
    const ids = new Set();
    for (const movie of movies) {
      if (!movie || !positiveInt(movie.movieId) || typeof movie.title !== 'string' || !movie.title.trim() || movie.title.length > 250) {
        return errorResponse(res, 400, 'INVALID_REQUEST', 'Each movie must include a positive movieId and title');
      }
      if (ids.has(Number(movie.movieId))) return errorResponse(res, 400, 'INVALID_REQUEST', 'movies must not repeat IDs');
      ids.add(Number(movie.movieId));
      if (movie.year !== undefined && movie.year !== null && (!Number.isInteger(Number(movie.year)) || Number(movie.year) < 1888 || Number(movie.year) > new Date().getFullYear() + 5)) {
        return errorResponse(res, 400, 'INVALID_REQUEST', 'year must be a valid movie release year');
      }
    }
    if (!tmdbAccessToken && !tmdbApiKey) return errorResponse(res, 503, 'POSTER_PROVIDER_NOT_CONFIGURED', 'Set TMDB_API_KEY or TMDB_ACCESS_TOKEN in backend/.env to load movie poster images');

    const stripMovieLensYear = title => title.normalize('NFKD').replace(/\s*\(\d{4}\)\s*$/, '').trim();
    const normalizeTitle = title => {
      let value = stripMovieLensYear(title).normalize('NFKD').replace(/\p{M}/gu, '');
      const trailingArticle = value.match(/^(.*),\s*(the|a|an)$/i);
      if (trailingArticle) value = `${trailingArticle[1]} ${trailingArticle[2]}`;
      const leadingArticle = value.match(/^(the|a|an)\s+(.*)$/i);
      if (leadingArticle) value = `${leadingArticle[2]} ${leadingArticle[1]}`;
      return value.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
    };
    const searchTitle = title => {
      const value = stripMovieLensYear(title);
      const trailingArticle = value.match(/^(.*),\s*(the|a|an)$/i);
      return trailingArticle ? `${trailingArticle[2]} ${trailingArticle[1]}` : value;
    };
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
    const retryDelay = error => {
      const status = error?.response?.status;
      const retryable = status === 429 || status >= 500 || ['ECONNRESET', 'ECONNABORTED', 'ETIMEDOUT'].includes(error?.code);
      if (!retryable) return null;
      const retryAfter = Number(error?.response?.headers?.['retry-after']);
      return Number.isFinite(retryAfter) && retryAfter > 0 ? Math.min(retryAfter * 1000, 3000) : 300;
    };
    try {
      const lookupOne = async movie => {
        const title = movie.title.trim();
        const year = movie.year == null ? null : Number(movie.year);
        // Versioning avoids retaining old negative cache entries after matcher improvements.
        const cacheKey = `${POSTER_CACHE_VERSION}|${normalizeTitle(title)}|${year ?? ''}`;
        const cached = getCachedPoster(cacheKey);
        if (cached.hit) return { movieId: Number(movie.movieId), posterUrl: cached.posterUrl };
        try {
          let response;
          for (let attempt = 0; attempt < 3; attempt += 1) {
            try {
              response = await tmdbClient.get('/search/movie', { params: { query: searchTitle(title), ...(year ? { year } : {}), include_adult: false, ...(!tmdbAccessToken && tmdbApiKey ? { api_key: tmdbApiKey } : {}) } });
              break;
            } catch (error) {
              const maxAttempts = error?.code === 'ECONNRESET' ? 3 : 2;
              const delay = attempt + 1 < maxAttempts ? retryDelay(error) : null;
              if (delay === null) throw error;
              await wait(Math.max(delay, 250 * (attempt + 1)));
            }
          }
          if (!response.data || typeof response.data !== 'object' || !Array.isArray(response.data.results)) {
            const invalidResponse = new Error('TMDB returned an invalid response');
            invalidResponse.code = 'INVALID_TMDB_RESPONSE';
            throw invalidResponse;
          }
          const candidates = response.data.results;
          const exactTitleMatches = candidates.filter(candidate => {
            const titleMatches = [candidate.title, candidate.original_title].some(value => typeof value === 'string' && normalizeTitle(value) === normalizeTitle(title));
            return titleMatches;
          });
          const yearOf = candidate => /^\d{4}/.test(candidate.release_date || '') ? Number(candidate.release_date.slice(0, 4)) : null;
          const validPath = candidate => typeof candidate?.poster_path === 'string' && /^\/[A-Za-z0-9._-]+$/.test(candidate.poster_path);
          const exactYearMatches = year ? exactTitleMatches.filter(candidate => yearOf(candidate) === year) : exactTitleMatches;
          let exactMatch = null;
          if (exactYearMatches.length === 1) exactMatch = exactYearMatches[0];
          else if (exactYearMatches.length > 1) {
            const withPosters = exactYearMatches.filter(validPath);
            if (withPosters.length === 1) exactMatch = withPosters[0];
          } else if (year && exactTitleMatches.length === 1) {
            // MovieLens occasionally differs from TMDB by one release year; only accept
            // this fallback for a unique exact-title candidate with a known adjacent year.
            const only = exactTitleMatches[0];
            const candidateYear = yearOf(only);
            if (candidateYear && Math.abs(candidateYear - year) <= 1) exactMatch = only;
          } else if (!year && exactTitleMatches.length === 1) exactMatch = exactTitleMatches[0];
          // Some titles are marketed under a longer regional/English title than
          // TMDB's primary title (for example, Waqt / Waqt: The Race Against Time).
          // Ask TMDB for aliases only for same-year candidates whose primary title
          // is a strict token subset; accept only an exact alias match.
          if (!exactMatch && year && exactYearMatches.length === 0 && candidates.length <= 10) {
            const expected = normalizeTitle(title);
            const expectedTokens = new Set(expected.split(' ').filter(token => token.length > 1));
            const aliasCandidates = candidates.filter(candidate => {
              if (yearOf(candidate) !== year || !validPath(candidate) || !positiveInt(candidate.id)) return false;
              return [candidate.title, candidate.original_title].some(value => {
                if (typeof value !== 'string') return false;
                const candidateTitle = normalizeTitle(value);
                const tokens = candidateTitle.split(' ').filter(token => token.length > 1);
                return candidateTitle !== expected && tokens.length > 0 && tokens.every(token => expectedTokens.has(token));
              });
            });
            if (aliasCandidates.length > 0 && aliasCandidates.length <= 3) {
              const verified = [];
              for (const candidate of aliasCandidates) {
                const aliasResponse = await tmdbClient.get(`/movie/${candidate.id}/alternative_titles`, {
                  params: !tmdbAccessToken && tmdbApiKey ? { api_key: tmdbApiKey } : {},
                });
                const aliases = aliasResponse.data?.titles;
                if (Array.isArray(aliases) && aliases.some(alias => typeof alias?.title === 'string' && normalizeTitle(alias.title) === expected)) {
                  verified.push(candidate);
                }
              }
              if (verified.length === 1) exactMatch = verified[0];
            }
          }
          const posterPath = typeof exactMatch?.poster_path === 'string' && /^\/[A-Za-z0-9._-]+$/.test(exactMatch.poster_path) ? exactMatch.poster_path : null;
          const posterUrl = posterPath ? `https://image.tmdb.org/t/p/w500${posterPath}` : null;
          setCachedPoster(cacheKey, posterUrl);
          return { movieId: Number(movie.movieId), posterUrl };
        } catch (error) {
          return { movieId: Number(movie.movieId), posterUrl: null, lookupError: error };
        }
      };
      const results = [];
      for (let index = 0; index < movies.length; index += POSTER_LOOKUP_CONCURRENCY) {
        results.push(...await Promise.all(movies.slice(index, index + POSTER_LOOKUP_CONCURRENCY).map(lookupOne)));
      }
      const failures = results.filter(item => item.lookupError);
      if (failures.length === results.length) {
        const error = failures[0].lookupError;
        if (error?.code === 'INVALID_TMDB_RESPONSE') return errorResponse(res, 502, 'POSTER_SERVICE_INVALID_RESPONSE', 'The poster provider returned an invalid response');
        if (axios.isAxiosError(error) && (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT')) {
          return errorResponse(res, 504, 'POSTER_SERVICE_TIMEOUT', 'The poster provider took too long to respond');
        }
        if (axios.isAxiosError(error) && [401, 403].includes(error.response?.status)) {
          return errorResponse(res, 502, 'POSTER_PROVIDER_AUTH_FAILED', 'TMDB rejected the configured credential; check its type and value in backend/.env');
        }
        if (axios.isAxiosError(error) && error.response?.status === 404) {
          return errorResponse(res, 502, 'POSTER_PROVIDER_ENDPOINT_ERROR', 'The poster provider endpoint was not found');
        }
        return errorResponse(res, 502, 'POSTER_SERVICE_UNAVAILABLE', 'Movie poster images could not be loaded right now');
      }
      const items = results.map(({ movieId, posterUrl }) => ({ movieId, posterUrl }));
      return res.json({ data: { items, partial: failures.length > 0, attribution: 'This product uses the TMDB API but is not endorsed or certified by TMDB.' } });
    } catch (error) {
      if (error?.code === 'INVALID_TMDB_RESPONSE') return errorResponse(res, 502, 'POSTER_SERVICE_INVALID_RESPONSE', 'The poster provider returned an invalid response');
      if (axios.isAxiosError(error) && (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT')) {
        return errorResponse(res, 504, 'POSTER_SERVICE_TIMEOUT', 'The poster provider took too long to respond');
      }
      if (axios.isAxiosError(error) && [401, 403].includes(error.response?.status)) {
        return errorResponse(res, 502, 'POSTER_PROVIDER_AUTH_FAILED', 'TMDB rejected the configured credential; check its type and value in backend/.env');
      }
      if (axios.isAxiosError(error) && error.response?.status === 404) {
        return errorResponse(res, 502, 'POSTER_PROVIDER_ENDPOINT_ERROR', 'The poster provider endpoint was not found');
      }
      return errorResponse(res, 502, 'POSTER_SERVICE_UNAVAILABLE', 'Movie poster images could not be loaded right now');
    }
  });

  const recommendationRoute = (kind, mlPath) => async (req, res) => {
    const validationError = validateBody(req.body, kind);
    if (validationError) return errorResponse(res, 400, 'INVALID_REQUEST', validationError);
    const upstreamBody = ['mood', 'search'].includes(kind) && req.body.source === 'all'
      ? Object.fromEntries(Object.entries(req.body).filter(([key]) => key !== 'source'))
      : req.body;
    try {
      const response = await mlClient.post(mlPath, upstreamBody, {
        timeout: kind === 'collaborative' || kind === 'hybrid' ? Math.max(mlTimeoutMs, 120_000) : mlTimeoutMs,
      });
      return res.json(response.data);
    } catch (error) {
      if (req.body.source === 'bollywood' && axios.isAxiosError(error) && error.response?.status === 422) {
        return errorResponse(res, 503, 'BOLLYWOOD_CATALOG_RELOAD_REQUIRED', 'Restart the ML service so it loads the Bollywood movie catalog, then try again');
      }
      return publicMlError(error, res);
    }
  };

  app.post('/api/recommendations/mood', recommendationRoute('mood', '/recommendations/mood'));
  app.post('/api/recommendations/similar', recommendationRoute('similar', '/recommendations/similar'));
  app.post('/api/recommendations/search', recommendationRoute('search', '/recommendations/search'));
  app.post('/api/recommendations/collaborative', recommendationRoute('collaborative', '/recommendations/collaborative'));
  app.post('/api/recommendations/hybrid', recommendationRoute('hybrid', '/recommendations/hybrid'));

  app.use((_req, res) => errorResponse(res, 404, 'NOT_FOUND', 'Route not found'));
  app.use((err, _req, res, _next) => {
    if (err?.type === 'entity.parse.failed') return errorResponse(res, 400, 'INVALID_JSON', 'Request body must be valid JSON');
    if (err?.type === 'entity.too.large') return errorResponse(res, 413, 'REQUEST_TOO_LARGE', 'Request body exceeds the allowed size');
    console.error('CineMood request failed:', err?.name || 'Error');
    errorResponse(res, 500, 'INTERNAL_ERROR', 'An unexpected error occurred');
  });
  return app;
}
