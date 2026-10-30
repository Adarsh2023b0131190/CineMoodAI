import React, { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { ArrowRight, Clapperboard, Compass, Film, Heart, Search } from 'lucide-react';

const moods = ['Happy', 'Sad', 'Stressed', 'Relaxed', 'Romantic', 'Bored', 'Excited', 'Curious', 'Motivated', 'Thoughtful', 'Suspenseful', 'Adventurous'];
const api = axios.create({ baseURL: import.meta.env.VITE_API_BASE_URL || '/api', withCredentials: true });

function readSavedRatings() {
  try {
    const saved = JSON.parse(localStorage.getItem('cinemood-ratings') || '[]');
    if (!Array.isArray(saved)) return [];
    const unique = new Map();
    for (const item of saved) {
      if (Number.isInteger(item?.movieId) && item.movieId > 0 && Number.isFinite(item?.rating) && item.rating >= 0.5 && item.rating <= 5 && Number.isInteger(item.rating * 2)) unique.set(item.movieId, { movieId: item.movieId, rating: item.rating });
    }
    return [...unique.values()].sort((a, b) => a.movieId - b.movieId);
  } catch {
    return [];
  }
}

export default function App() {
  const [mood, setMood] = useState('');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [health, setHealth] = useState('Checking services…');
  const [query, setQuery] = useState('');
  const [requestState, setRequestState] = useState('idle');
  const [recommendations, setRecommendations] = useState([]);
  const [resultMeta, setResultMeta] = useState(null);
  const [recommendationError, setRecommendationError] = useState('');
  const [lastRequest, setLastRequest] = useState(null);
  const [profileRatings, setProfileRatings] = useState(readSavedRatings);
  const [ratingDraft, setRatingDraft] = useState({});
  const [ratingSaveMessage, setRatingSaveMessage] = useState('');
  const [user, setUser] = useState(null);
  const [authMode, setAuthMode] = useState('');
  const [authForm, setAuthForm] = useState({ name: '', email: '', password: '' });
  const [authError, setAuthError] = useState('');
  const [authBusy, setAuthBusy] = useState(false);
  const [posterProviderConfigured, setPosterProviderConfigured] = useState(null);
  const requestLock = useRef(false);

  useEffect(() => {
    api.get('/health').then(({ data }) => setHealth(`Services connected · ${data.data.dependencies.ml.service}`)).catch(() => setHealth('API unavailable · start the backend and ML service'));
    api.get('/auth/me').then(async ({ data }) => {
      const currentUser = data.data?.user || null;
      setUser(currentUser);
      if (currentUser) {
        const response = await api.get('/ratings');
        const ratings = response.data?.data?.items || [];
        setProfileRatings(ratings);
      }
    }).catch(() => {});
  }, []);

  const submitAuth = async event => {
    event.preventDefault();
    setAuthBusy(true);
    setAuthError('');
    try {
      const endpoint = authMode === 'register' ? '/auth/register' : '/auth/login';
      const payload = authMode === 'register' ? authForm : { email: authForm.email, password: authForm.password };
      const response = await api.post(endpoint, payload);
      setUser(response.data.data.user);
      const saved = await api.get('/ratings');
      setProfileRatings(saved.data.data.items || []);
      setAuthMode('');
      setAuthForm({ name: '', email: '', password: '' });
    } catch (error) {
      setAuthError(error.response?.data?.error?.message || 'Could not sign in. Check the API and try again.');
    } finally { setAuthBusy(false); }
  };

  const signOut = async () => {
    try {
      await api.post('/auth/logout');
      setUser(null);
      setProfileRatings(readSavedRatings());
      setRatingSaveMessage('Signed out. Device ratings remain available on this browser.');
    } catch (error) { setRatingSaveMessage(error.response?.data?.error?.message || 'Could not sign out. Please retry.'); }
  };

  const requestRecommendations = async request => {
    if (requestLock.current) return;
    requestLock.current = true;
    setLastRequest(request);
    setRecommendationError('');
    setRecommendations([]);
    setResultMeta(null);
    setRequestState('loading');
    try {
      const response = await api.post(request.path, request.payload);
      const data = response.data?.data;
      if (!data || !Array.isArray(data.items)) throw new Error('The recommendation service returned an unexpected response.');
      let resultItems = data.items.map(item => ({ ...item, posterUrl: null }));
      if (resultItems.length) {
        try {
          const posters = await api.post('/posters/lookup', { movies: resultItems.map(({ movieId, title, year }) => ({ movieId, title, year })) });
          const posterById = new Map((posters.data?.data?.items || []).map(item => [item.movieId, item.posterUrl]));
          resultItems = resultItems.map(item => ({ ...item, posterUrl: posterById.get(item.movieId) || null }));
          setPosterProviderConfigured(true);
        } catch (posterError) {
          if (posterError.response?.data?.error?.code === 'POSTER_PROVIDER_NOT_CONFIGURED') setPosterProviderConfigured(false);
        }
      }
      setRecommendations(resultItems);
      setResultMeta(data);
      setRequestState('success');
    } catch (error) {
      const message = error.response?.data?.error?.message || error.message || 'Recommendations could not be loaded. Please try again.';
      setRecommendationError(message);
      setRequestState('error');
    } finally {
      requestLock.current = false;
    }
  };

  const chooseMood = item => {
    setMood(item);
    requestRecommendations({ path: '/recommendations/mood', payload: { mood: item.toLowerCase(), ...(sourceFilter === 'all' ? {} : { source: sourceFilter }) }, label: `${item}${sourceFilter === 'bollywood' ? ' · Bollywood' : ''}` });
  };

  const submitSearch = event => {
    event.preventDefault();
    const cleanQuery = query.trim();
    if (cleanQuery.length < 2) return;
    requestRecommendations({ path: '/recommendations/search', payload: { query: cleanQuery, ...(sourceFilter === 'all' ? {} : { source: sourceFilter }) }, label: 'your request' });
  };

  const saveRating = async movieId => {
    const rating = Number(ratingDraft[movieId] ?? profileRatings.find(item => item.movieId === movieId)?.rating);
    if (!Number.isFinite(rating) || rating < 0.5 || rating > 5) return;
    if (profileRatings.length >= 50 && !profileRatings.some(item => item.movieId === movieId)) {
      setRatingSaveMessage('Your profile holds up to 50 movie ratings. Update an existing rating to make room.');
      return;
    }
    if (user) {
      try {
        const response = await api.put(`/ratings/${movieId}`, { rating });
        const saved = await api.get('/ratings');
        setProfileRatings(saved.data.data.items || []);
        setRatingSaveMessage(`Saved ${response.data.data.item.rating} / 5 to your account.`);
      } catch (error) {
        setRatingSaveMessage(error.response?.data?.error?.message || 'Rating could not be saved. Please retry.');
      }
      return;
    }
    const next = [...profileRatings.filter(item => item.movieId !== movieId), { movieId, rating }];
    next.sort((a, b) => a.movieId - b.movieId);
    localStorage.setItem('cinemood-ratings', JSON.stringify(next));
    setProfileRatings(next);
    setRatingSaveMessage('Rating saved on this device. Sign in to sync it to your account.');
  };

  const useProfile = (path, label) => requestRecommendations({
    path: user ? '/recommendations/personalized' : path,
    payload: user ? { method: path.endsWith('collaborative') ? 'collaborative' : 'hybrid' } : { ratings: profileRatings },
    label,
  });

  const scoreLabel = resultMeta?.method?.startsWith('collaborative/')
    ? 'Rating pattern'
    : resultMeta?.method?.startsWith('hybrid/') ? 'Hybrid match' : 'Content match';

  return <main className="shell" id="top">
    <header className="nav"><a className="brand" href="/" aria-label="CineMood home"><span className="brand-mark"><Film size={18}/></span>Cine<span>Mood</span></a><nav aria-label="Main navigation"><a href="#discover"><Compass size={16} aria-hidden="true"/> Discover</a><a href="#saved"><Heart size={16} aria-hidden="true"/> My list</a></nav>{user ? <div className="account-nav"><span>{user.name}</span><button className="signin" type="button" onClick={signOut}>Sign out</button></div> : <button className="signin" type="button" onClick={() => { setAuthMode('login'); setAuthError(''); }}>Sign in</button>}</header>
    {authMode && <section className="auth-panel" aria-labelledby="auth-title"><div className="auth-heading"><h2 id="auth-title">{authMode === 'register' ? 'Create your account' : 'Welcome back'}</h2><button type="button" aria-label="Close account form" onClick={() => setAuthMode('')}>×</button></div><form onSubmit={submitAuth}>{authMode === 'register' && <label>Name<input required autoComplete="name" maxLength="80" value={authForm.name} onChange={event => setAuthForm({ ...authForm, name: event.target.value })}/></label>}<label>Email<input required type="email" autoComplete="email" value={authForm.email} onChange={event => setAuthForm({ ...authForm, email: event.target.value })}/></label><label>Password<input required type="password" autoComplete={authMode === 'register' ? 'new-password' : 'current-password'} minLength={authMode === 'register' ? 10 : undefined} maxLength="128" value={authForm.password} onChange={event => setAuthForm({ ...authForm, password: event.target.value })}/></label>{authError && <p role="alert">{authError}</p>}<button className="secondary-action primary-action" type="submit" disabled={authBusy}>{authBusy ? 'Please wait…' : authMode === 'register' ? 'Create account' : 'Sign in'}</button></form><p>{authMode === 'register' ? 'Already have an account?' : 'New to CineMood?'} <button type="button" className="text-button" onClick={() => { setAuthMode(authMode === 'register' ? 'login' : 'register'); setAuthError(''); }}>{authMode === 'register' ? 'Sign in' : 'Create an account'}</button></p></section>}
    <section className="hero">
      <div className="hero-copy"><div className="eyebrow"><Clapperboard size={15}/> YOUR NEXT FAVORITE FILM</div><h1>Every mood<br/>has a <em>movie.</em></h1><p>Find the story that meets you where you are — and takes you somewhere new.</p><a className="primary" href="#discover">Find your film <ArrowRight size={17}/></a></div>
      <div className="hero-art" role="img" aria-label="Illustrated cinematic sunset with a person watching the horizon"><div className="art-orb"></div><div className="art-silhouette"></div><div className="art-caption"><span>TONIGHT’S FEATURE</span><b>Made for this moment.</b></div></div>
    </section>
    <section className="discover" id="discover"><div className="section-heading"><div><div className="eyebrow">A LITTLE HELP FROM THE ALGORITHM</div><h2>How are you feeling today?</h2></div><span className="step">01 <i>/ 02</i></span></div>
      <div className="moods" role="group" aria-label="Choose your current mood">{moods.map(item => <button key={item} type="button" aria-pressed={mood === item} disabled={requestState === 'loading'} className={mood === item ? 'mood selected' : 'mood'} onClick={() => chooseMood(item)}>{item}</button>)}</div>
      <div className="moods source-filter" role="group" aria-label="Filter movies by catalog"><button type="button" aria-pressed={sourceFilter === 'all'} disabled={requestState === 'loading'} className={sourceFilter === 'all' ? 'mood selected' : 'mood'} onClick={() => setSourceFilter('all')}>All movies</button><button type="button" aria-pressed={sourceFilter === 'bollywood'} disabled={requestState === 'loading'} className={sourceFilter === 'bollywood' ? 'mood selected' : 'mood'} onClick={() => setSourceFilter('bollywood')}>Bollywood</button></div>
      <form className="search-row" onSubmit={submitSearch}><Search size={19} aria-hidden="true"/><label className="sr-only" htmlFor="mood-query">Describe the movie you want</label><input id="mood-query" name="query" type="search" minLength="2" maxLength="500" required value={query} onChange={event => setQuery(event.target.value)} placeholder="Or tell us what you’re in the mood for…"/><button type="submit" aria-label="Search" disabled={requestState === 'loading'}><ArrowRight size={19} aria-hidden="true"/></button></form>
      <div className="service-status"><span className={health.startsWith('Services') ? 'dot online' : 'dot'}></span>{health}</div>
      <div className="profile-actions">
        <div><strong>{profileRatings.length} saved {profileRatings.length === 1 ? 'rating' : 'ratings'}</strong><span>{user ? `Saved to your account, ${user.name}.` : 'Stored on this device. Sign in to save ratings to your account.'}</span>{ratingSaveMessage && <span role="status">{ratingSaveMessage}</span>}</div>
        <div className="profile-action-buttons">
          <button type="button" className="secondary-action" disabled={!profileRatings.length || requestState === 'loading'} onClick={() => useProfile('/recommendations/collaborative', 'your rating patterns')}>Collaborative picks</button>
          <button type="button" className="secondary-action primary-action" disabled={!profileRatings.length || requestState === 'loading'} onClick={() => useProfile('/recommendations/hybrid', 'your hybrid profile')}>Hybrid picks</button>
          {user && <button type="button" className="secondary-action" disabled={requestState === 'loading'} onClick={() => requestRecommendations({ path: '/recommendations/personalized', payload: {}, label: 'your account' })}>Personalized picks</button>}
        </div>
      </div>
    </section>
    {requestState !== 'idle' && <section className="recommendations" aria-live="polite" aria-busy={requestState === 'loading'}>
      <div className="section-heading"><div><div className="eyebrow">YOUR MOVIE MATCHES</div><h2>{requestState === 'loading' ? 'Finding a good fit…' : `Picked for ${lastRequest?.label || 'you'}`}</h2></div>{requestState === 'success' && <span className="result-count">{recommendations.length} {recommendations.length === 1 ? 'film' : 'films'}</span>}</div>
      {requestState === 'loading' && <div className="recommendation-message"><span className="loading-dot" aria-hidden="true"/> {lastRequest?.path?.includes('collaborative') || lastRequest?.path?.includes('hybrid') ? 'Comparing your saved ratings with MovieLens rating patterns…' : 'Comparing your request with movie titles and genres…'}</div>}
      {requestState === 'error' && <div className="recommendation-message error-message"><p>{recommendationError}</p><button className="retry-button" type="button" onClick={() => lastRequest && requestRecommendations(lastRequest)}>Try again</button></div>}
      {requestState === 'success' && recommendations.length === 0 && <div className="recommendation-message">{resultMeta?.message || 'No movies matched the supported title and genre signals in this catalog. Try a mood or genre supported by the available data.'}</div>}
      {requestState === 'success' && recommendations.length > 0 && <>
        <div className="movie-grid">{recommendations.map(movie => <article className="movie-card" key={movie.movieId}>
          <div className="movie-poster">{movie.posterUrl ? <img src={movie.posterUrl} alt={`Poster for ${movie.title}`} loading="lazy" referrerPolicy="no-referrer" onError={() => setRecommendations(current => current.map(item => item.movieId === movie.movieId ? { ...item, posterUrl: null } : item))}/> : <div className="poster-placeholder" role="img" aria-label={`Poster unavailable for ${movie.title}`}><Film size={23} aria-hidden="true"/><span>{posterProviderConfigured === false ? 'Set TMDB_API_KEY in backend/.env to load posters' : 'Artwork unavailable'}</span></div>}</div>
          <div className="movie-card-content"><div className="movie-card-heading"><h3>{movie.title}</h3>{movie.year && <span className="movie-year">{movie.year}</span>}</div>
            <p className="movie-genres">{movie.genres.length ? movie.genres.join(' · ') : 'Genres not listed'}</p>
            <p className="movie-explanation">{movie.explanation}</p>
            <span className="match-score">{scoreLabel} · {Math.round(movie.score * 100)}%</span>
            <div className="movie-rating">
              <label htmlFor={`rating-${movie.movieId}`}>Your rating</label>
              <select id={`rating-${movie.movieId}`} value={ratingDraft[movie.movieId] ?? (profileRatings.find(item => item.movieId === movie.movieId)?.rating ?? '')} onChange={event => setRatingDraft(current => ({ ...current, [movie.movieId]: event.target.value }))}>
                <option value="">Choose</option>
                {Array.from({ length: 10 }, (_, index) => (index + 1) / 2).map(value => <option key={value} value={value}>{value} / 5</option>)}
              </select>
              <button type="button" onClick={() => saveRating(movie.movieId)} disabled={ratingDraft[movie.movieId] === undefined && !profileRatings.some(item => item.movieId === movie.movieId)}>Save</button>
            </div>
          </div>
        </article>)}</div>
        {resultMeta?.metadataLimitations?.[0] && <p className="metadata-note">{resultMeta.metadataLimitations[0]}</p>}
      </>}
    </section>}
    <section className="below" id="saved"><div><span className="eyebrow">A BETTER WAY TO CHOOSE</span><h2>Less scrolling.<br/><em>More feeling.</em></h2></div><p>Recommendations will get more personal as you rate films and save the ones you love. Your picks should feel like yours.</p></section>
    <footer><a className="brand" href="/"><span className="brand-mark"><Film size={15}/></span>Cine<span>Mood</span></a><span>Good stories find their way to you.</span><span className="tmdb-credit">Poster artwork via <a href="https://www.themoviedb.org" target="_blank" rel="noreferrer">TMDB</a>. This product uses the TMDB API but is not endorsed or certified by TMDB.</span><span>© CineMood</span></footer>
  </main>;
}
