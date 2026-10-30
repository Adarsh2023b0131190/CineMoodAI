import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import test from 'node:test';
import { createApp } from '../src/app.js';

async function withServers(handler) {
  const received = [];
  const mlServer = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null;
    received.push({ method: req.method, url: req.url, body });
    if (req.url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ status: 'ok', service: 'cinemood-ml' }));
    }
    if (req.url === '/movies/999') {
      res.writeHead(404, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ error: { code: 'MOVIE_NOT_FOUND', message: 'missing' } }));
    }
    if (req.url === '/movies?limit=2&offset=0') {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ data: { items: [{ movieId: 1, title: 'Sample', genres: ['Comedy'], year: 2000 }], total: 1, limit: 2, offset: 0 } }));
    }
    if (req.url === '/recommendations/mood') {
      if (body?.source === 'bollywood') {
        res.writeHead(422, { 'content-type': 'application/json' });
        return res.end(JSON.stringify({ error: { code: 'VALIDATION_ERROR', message: 'The running ML build does not accept source' } }));
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ data: { items: [{ movieId: 1, title: 'Sample', genres: ['Comedy'], year: 2000, score: 0.8, explanation: 'Matches Comedy.', matchedSignals: ['genre:Comedy'] }], total: 1, method: 'content-based/tfidf-cosine', interpretation: ['mood:happy'], metadataLimitations: [] } }));
    }
    if (req.url === '/recommendations/search') {
      res.writeHead(503, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ detail: 'internal stack must not reach the browser' }));
    }
    if (req.url === '/recommendations/collaborative' || req.url === '/recommendations/hybrid') {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ data: { items: [], total: 0, method: req.url.includes('hybrid') ? 'hybrid/content+collaborative' : 'collaborative/item-cosine', interpretation: [], metadataLimitations: [], fallback: false } }));
    }
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: { code: 'NOT_FOUND', message: 'missing' } }));
  });
  mlServer.listen(0, '127.0.0.1');
  await once(mlServer, 'listening');
  const mlPort = mlServer.address().port;
  const apiServer = createApp({ mlServiceUrl: `http://127.0.0.1:${mlPort}` }).listen(0, '127.0.0.1');
  await once(apiServer, 'listening');
  try {
    await handler(`http://127.0.0.1:${apiServer.address().port}`, received);
  } finally {
    apiServer.close();
    mlServer.close();
    await Promise.all([once(apiServer, 'close'), once(mlServer, 'close')]);
  }
}

test('proxies movie list using bounded query parameters and preserves the response envelope', async () => {
  await withServers(async (baseUrl, received) => {
    const response = await fetch(`${baseUrl}/api/movies?limit=2&offset=0`);
    assert.equal(response.status, 200);
    assert.deepEqual((await response.json()).data.items[0], { movieId: 1, title: 'Sample', genres: ['Comedy'], year: 2000 });
    assert.equal(received.at(-1).url, '/movies?limit=2&offset=0');
  });
});

test('validates and proxies mood requests', async () => {
  await withServers(async (baseUrl, received) => {
    const response = await fetch(`${baseUrl}/api/recommendations/mood`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mood: 'happy', limit: 4, source: 'all' }) });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.data.method, 'content-based/tfidf-cosine');
    assert.equal(body.data.items[0].movieId, 1);
    assert.deepEqual(received.at(-1).body, { mood: 'happy', limit: 4 });

    const bollywood = await fetch(`${baseUrl}/api/recommendations/mood`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mood: 'happy', source: 'bollywood' }) });
    assert.equal(bollywood.status, 503);
    assert.equal((await bollywood.json()).error.code, 'BOLLYWOOD_CATALOG_RELOAD_REQUIRED');
    assert.deepEqual(received.at(-1).body, { mood: 'happy', source: 'bollywood' });

    const invalid = await fetch(`${baseUrl}/api/recommendations/mood`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mood: 'unrecognized' }) });
    assert.equal(invalid.status, 400);

    const invalidSource = await fetch(`${baseUrl}/api/recommendations/mood`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mood: 'happy', source: 'tollywood' }) });
    assert.equal(invalidSource.status, 400);
  });
});

test('validates search and does not expose upstream service details', async () => {
  await withServers(async baseUrl => {
    const invalid = await fetch(`${baseUrl}/api/recommendations/search`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ query: 'x' }) });
    assert.equal(invalid.status, 400);

    const failed = await fetch(`${baseUrl}/api/recommendations/search`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ query: 'unmatched query' }) });
    assert.equal(failed.status, 502);
    const body = await failed.json();
    assert.equal(body.error.code, 'ML_SERVICE_UNAVAILABLE');
    assert.equal(JSON.stringify(body).includes('internal stack'), false);
  });
});

test('maps missing movie results to a safe not-found response', async () => {
  await withServers(async baseUrl => {
    const response = await fetch(`${baseUrl}/api/movies/999`);
    assert.equal(response.status, 404);
    assert.equal((await response.json()).error.code, 'MOVIE_NOT_FOUND');
  });
});

test('validates explicit rating profiles and proxies collaborative and hybrid endpoints', async () => {
  await withServers(async (baseUrl, received) => {
    const collaborative = await fetch(`${baseUrl}/api/recommendations/collaborative`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ratings: [{ movieId: 11, rating: 4.5 }], excludeMovieIds: [12] }),
    });
    assert.equal(collaborative.status, 200);
    assert.equal((await collaborative.json()).data.method, 'collaborative/item-cosine');
    assert.deepEqual(received.at(-1).body, { ratings: [{ movieId: 11, rating: 4.5 }], excludeMovieIds: [12] });

    const hybrid = await fetch(`${baseUrl}/api/recommendations/hybrid`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ratings: [{ movieId: 11, rating: 4 }], contentWeight: 0.65 }),
    });
    assert.equal(hybrid.status, 200);
    assert.equal((await hybrid.json()).data.method, 'hybrid/content+collaborative');
    assert.deepEqual(received.at(-1).body, { ratings: [{ movieId: 11, rating: 4 }], contentWeight: 0.65 });

    for (const payload of [
      { userId: 123, ratings: [{ movieId: 11, rating: 4 }] },
      { ratings: [{ movieId: 11, rating: 4 }, { movieId: 11, rating: 3 }] },
      { ratings: [{ movieId: 11, rating: 5.2 }] },
    ]) {
      const invalid = await fetch(`${baseUrl}/api/recommendations/collaborative`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload),
      });
      assert.equal(invalid.status, 400);
    }
  });
});

test('CORS allows the local Vite host aliases and rejects unrelated origins', async () => {
  await withServers(async baseUrl => {
    for (const origin of ['http://localhost:5173', 'http://127.0.0.1:5173']) {
      const response = await fetch(`${baseUrl}/api/health`, { headers: { Origin: origin } });
      assert.equal(response.headers.get('access-control-allow-origin'), origin);
    }
    const rejected = await fetch(`${baseUrl}/api/health`, { headers: { Origin: 'https://example.invalid' } });
    assert.equal(rejected.headers.get('access-control-allow-origin'), null);
  });
});

test('returns a safe client error for malformed JSON', async () => {
  await withServers(async baseUrl => {
    const response = await fetch(`${baseUrl}/api/recommendations/search`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: '{bad json',
    });
    assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { error: { code: 'INVALID_JSON', message: 'Request body must be valid JSON' } });
  });
});
