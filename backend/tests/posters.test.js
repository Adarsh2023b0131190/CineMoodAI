import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import test from 'node:test';
import { createApp } from '../src/app.js';

const sampleMovie = { movieId: 1, title: 'Toy Story (1995)', year: 1995 };

async function withPosterServer(options, handler) {
  const requests = [];
  const tmdb = createServer(async (req, res) => {
    const requestUrl = new URL(req.url, 'http://localhost');
    requests.push({ url: requestUrl, authorization: req.headers.authorization });
    if (options.respond) return options.respond(req, res, requestUrl);
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ results: [
      { title: 'Toy Story', original_title: 'Toy Story', release_date: '1995-11-22', poster_path: '/toy-story.jpg' },
      { title: 'The Pebble and the Penguin', original_title: 'The Pebble and the Penguin', release_date: '1995-04-12', poster_path: '/pebble-penguin.jpg' },
      { title: 'King of Jazz', original_title: 'King of Jazz', release_date: '1930-04-20', poster_path: null },
    ] }));
  });
  tmdb.listen(0, '127.0.0.1');
  await once(tmdb, 'listening');
  const api = createApp({
    tmdbAccessToken: options.accessToken || '',
    tmdbApiKey: options.apiKey || '',
    tmdbBaseUrl: `http://127.0.0.1:${tmdb.address().port}`,
    tmdbTimeoutMs: options.timeoutMs ?? 6000,
  }).listen(0, '127.0.0.1');
  await once(api, 'listening');
  try { await handler(`http://127.0.0.1:${api.address().port}`, requests); }
  finally {
    api.close();
    tmdb.close();
    await Promise.all([once(api, 'close'), once(tmdb, 'close')]);
  }
}

async function lookup(baseUrl, movies = [sampleMovie]) {
  return fetch(`${baseUrl}/api/posters/lookup`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ movies }),
  });
}

test('uses a Read Access Token as Bearer auth and creates the documented poster URL', async () => {
  await withPosterServer({ accessToken: 'dummy-read-access-token' }, async (baseUrl, requests) => {
    const response = await lookup(baseUrl, [
      sampleMovie,
      { movieId: 2, title: 'King of Jazz (1930)', year: 1930 },
      { movieId: 3, title: 'Toy Story 2 (1996)', year: 1996 },
    ]);
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.deepEqual(result.data.items, [
      { movieId: 1, posterUrl: 'https://image.tmdb.org/t/p/w500/toy-story.jpg' },
      { movieId: 2, posterUrl: null },
      { movieId: 3, posterUrl: null },
    ]);
    assert.equal(result.data.attribution.includes('not endorsed or certified'), true);
    assert.equal(requests[0].url.pathname, '/search/movie');
    assert.equal(requests[0].url.searchParams.get('query'), 'Toy Story');
    assert.equal(requests[0].url.searchParams.get('year'), '1995');
    assert.equal(requests[0].url.searchParams.get('include_adult'), 'false');
    assert.equal(requests[0].authorization, 'Bearer dummy-read-access-token');
    assert.equal(requests[0].url.searchParams.has('api_key'), false);
  });
});

test('uses an unambiguous exact-title match if provider and catalog years differ', async () => {
  await withPosterServer({
    apiKey: 'dummy-v3-api-key',
    respond: async (_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ results: [{ title: 'The Pebble and the Penguin', release_date: '1996-01-01', poster_path: '/pebble-penguin.jpg' }] }));
    },
  }, async baseUrl => {
    const response = await lookup(baseUrl, [{ movieId: 72261, title: 'Pebble and the Penguin, The (1995)', year: 1995 }]);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).data.items[0].posterUrl, 'https://image.tmdb.org/t/p/w500/pebble-penguin.jpg');
  });
});

test('uses a v3 API key as the api_key parameter, not as a Bearer token', async () => {
  await withPosterServer({ apiKey: 'dummy-v3-api-key' }, async (baseUrl, requests) => {
    const response = await lookup(baseUrl);
    assert.equal(response.status, 200);
    assert.equal(requests[0].url.searchParams.get('api_key'), 'dummy-v3-api-key');
    assert.equal(requests[0].authorization, undefined);
  });
});

test('resolves Bollywood examples with release-year verification and returns null for unrelated matches', async () => {
  const expected = {
    'Pyaar Mein Twist': '/pyaar-twist.jpg',
    'Badhaai Ho Badhaai': '/badhaai-ho.jpg',
    'Chori Chori': '/chori-chori-2003.jpg',
    'Jodi Breakers': '/jodi-breakers.jpg',
    'No Entry': '/no-entry-2005.jpg',
  };
  const years = { 'Pyaar Mein Twist': 2005, 'Badhaai Ho Badhaai': 2002, 'Chori Chori': 2003, 'Jodi Breakers': 2012, 'No Entry': 2005 };
  await withPosterServer({
    apiKey: 'dummy-v3-api-key',
    respond: async (_req, res, requestUrl) => {
      const title = requestUrl.searchParams.get('query');
      const year = Number(requestUrl.searchParams.get('year'));
      const alternatives = title === 'Chori Chori'
        ? [
          { title, release_date: '1972-01-01', poster_path: '/wrong-chori.jpg' },
          { title, release_date: '2003-08-01', poster_path: expected[title] },
        ]
        : title === 'No Entry'
          ? [
            { title, release_date: '2011-04-07', poster_path: '/wrong-no-entry.jpg' },
            { title, release_date: '2005-08-26', poster_path: expected[title] },
          ]
          : [{ title, release_date: `${years[title]}-08-01`, poster_path: expected[title] }];
      assert.equal(year, years[title]);
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ results: alternatives }));
    },
  }, async baseUrl => {
    const movies = Object.entries(years).map(([title, year], index) => ({ movieId: 1000 + index, title, year }));
    const response = await lookup(baseUrl, movies);
    assert.equal(response.status, 200);
    const { items } = (await response.json()).data;
    assert.deepEqual(items, movies.map(movie => ({ movieId: movie.movieId, posterUrl: expected[movie.title] ? `https://image.tmdb.org/t/p/w500${expected[movie.title]}` : null })));
  });
});

test('does not use a poster from a merely similar title', async () => {
  await withPosterServer({
    apiKey: 'dummy-v3-api-key',
    respond: async (_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ results: [{ title: 'Badhaai Ho', release_date: '2018-10-19', poster_path: '/wrong-badhaai.jpg' }] }));
    },
  }, async baseUrl => {
    const response = await lookup(baseUrl, [{ movieId: 1, title: 'Badhaai Ho Badhaai', year: 2002 }]);
    assert.equal(response.status, 200);
    assert.deepEqual((await response.json()).data.items, [{ movieId: 1, posterUrl: null }]);
  });
});

test('verifies alternate marketing titles against TMDB aliases before using a poster', async () => {
  await withPosterServer({
    apiKey: 'dummy-v3-api-key',
    respond: async (_req, res, requestUrl) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      if (requestUrl.pathname === '/search/movie') {
        res.end(JSON.stringify({ results: [{ id: 31364, title: 'Waqt', original_title: 'वक्त', release_date: '2005-04-22', poster_path: '/waqt.jpg' }] }));
      } else {
        assert.equal(requestUrl.pathname, '/movie/31364/alternative_titles');
        res.end(JSON.stringify({ titles: [{ title: 'Waqt The Race Against Time', iso_3166_1: 'IN' }] }));
      }
    },
  }, async baseUrl => {
    const response = await lookup(baseUrl, [{ movieId: 31364, title: 'Waqt: The Race Against Time (2005)', year: 2005 }]);
    assert.equal(response.status, 200);
    assert.deepEqual((await response.json()).data.items, [{ movieId: 31364, posterUrl: 'https://image.tmdb.org/t/p/w500/waqt.jpg' }]);
  });
});

test('rejects alternate-title candidates when the alias or release year does not match', async t => {
  for (const scenario of [
    { name: 'wrong alias', year: 2005, alias: 'Waqt Returns' },
    { name: 'wrong year', year: 2006, alias: 'Waqt The Race Against Time' },
  ]) {
    await t.test(scenario.name, async () => {
      await withPosterServer({
        apiKey: 'dummy-v3-api-key',
        respond: async (_req, res, requestUrl) => {
          res.writeHead(200, { 'content-type': 'application/json' });
          if (requestUrl.pathname === '/search/movie') {
            res.end(JSON.stringify({ results: [{ id: 31364, title: 'Waqt', original_title: 'वक्त', release_date: `${scenario.year}-04-22`, poster_path: '/waqt.jpg' }] }));
          } else res.end(JSON.stringify({ titles: [{ title: scenario.alias }] }));
        },
      }, async baseUrl => {
        const response = await lookup(baseUrl, [{ movieId: 31364, title: 'Waqt: The Race Against Time (2005)', year: 2005 }]);
        assert.equal(response.status, 200);
        assert.deepEqual((await response.json()).data.items, [{ movieId: 31364, posterUrl: null }]);
      });
    });
  }
});

test('caches verified posters and genuine no-poster results but does not cache provider failures', async () => {
  let posterRequests = 0;
  await withPosterServer({
    apiKey: 'dummy-v3-api-key',
    respond: async (_req, res, requestUrl) => {
      posterRequests += 1;
      res.writeHead(200, { 'content-type': 'application/json' });
      const title = requestUrl.searchParams.get('query');
      const poster = title === 'Pyaar Mein Twist' ? '/pyaar-twist.jpg' : null;
      res.end(JSON.stringify({ results: [{ title, release_date: title === 'Pyaar Mein Twist' ? '2005-09-02' : '2002-01-01', poster_path: poster }] }));
    },
  }, async baseUrl => {
    const pyaar = { movieId: 1, title: 'Pyaar Mein Twist', year: 2005 };
    const missing = { movieId: 2, title: 'Genuine No Poster', year: 2002 };
    for (const movie of [pyaar, missing, pyaar, missing]) {
      const response = await lookup(baseUrl, [movie]);
      assert.equal(response.status, 200);
      assert.ok(Object.hasOwn((await response.json()).data.items[0], 'posterUrl'));
    }
    assert.equal(posterRequests, 2);
  });

  let failedRequests = 0;
  await withPosterServer({
    apiKey: 'dummy-v3-api-key',
    respond: async (_req, res) => {
      failedRequests += 1;
      res.writeHead(503, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ message: 'temporary provider failure' }));
    },
  }, async baseUrl => {
    const movie = { movieId: 3, title: 'Temporary Failure', year: 2020 };
    await lookup(baseUrl, [movie]);
    await lookup(baseUrl, [movie]);
    assert.equal(failedRequests, 4);
  });
});

test('retries a TMDB rate-limit response before returning verified results', async () => {
  let requests = 0;
  await withPosterServer({
    apiKey: 'dummy-v3-api-key',
    respond: async (_req, res) => {
      requests += 1;
      if (requests === 1) {
        res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '0.1' });
        res.end(JSON.stringify({ message: 'rate limited' }));
        return;
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ results: [{ title: 'Toy Story', release_date: '1995-11-22', poster_path: '/toy-story.jpg' }] }));
    },
  }, async baseUrl => {
    const response = await lookup(baseUrl);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).data.items[0].posterUrl, 'https://image.tmdb.org/t/p/w500/toy-story.jpg');
    assert.equal(requests, 2);
  });
});

test('bridges recommendation MovieLens IDs to exact title/year poster matches', async () => {
  const ml = createServer(async (_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ data: { items: [
      { movieId: 72261, title: 'Pebble and the Penguin, The (1995)', genres: ['Animation'], year: 1995, score: 0.8, explanation: 'test', matchedSignals: [] },
      { movieId: 3725, title: 'American Pop (1981)', genres: ['Animation'], year: 1981, score: 0.7, explanation: 'test', matchedSignals: [] },
      { movieId: 99999, title: 'A Catalog Movie With No Poster (1980)', genres: [], year: 1980, score: 0.6, explanation: 'test', matchedSignals: [] },
    ], total: 3, method: 'content-based/test', interpretation: [], metadataLimitations: [] } }));
  });
  const tmdb = createServer(async (req, res) => {
    const requestUrl = new URL(req.url, 'http://localhost');
    const query = requestUrl.searchParams.get('query');
    const results = query === 'The Pebble and the Penguin'
      ? [{ title: 'The Pebble and the Penguin', release_date: '1995-04-12', poster_path: '/pebble-penguin.jpg' }]
      : query === 'American Pop'
        ? [{ title: 'American Pop', release_date: '1981-02-13', poster_path: '/american-pop.jpg' }]
        : [{ title: 'A Catalog Movie With No Poster', release_date: '1980-01-01', poster_path: null }];
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ results }));
  });
  ml.listen(0, '127.0.0.1');
  tmdb.listen(0, '127.0.0.1');
  await Promise.all([once(ml, 'listening'), once(tmdb, 'listening')]);
  const api = createApp({
    mlServiceUrl: `http://127.0.0.1:${ml.address().port}`,
    tmdbBaseUrl: `http://127.0.0.1:${tmdb.address().port}`,
    tmdbApiKey: 'dummy-v3-api-key',
  }).listen(0, '127.0.0.1');
  await once(api, 'listening');
  const baseUrl = `http://127.0.0.1:${api.address().port}`;
  try {
    const recommendationResponse = await fetch(`${baseUrl}/api/recommendations/mood`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mood: 'happy', limit: 3 }),
    });
    assert.equal(recommendationResponse.status, 200);
    const recommended = (await recommendationResponse.json()).data.items;
    assert.equal(recommended[0].movieId, 72261);
    assert.equal(Object.hasOwn(recommended[0], 'posterUrl'), false);
    const posterResponse = await lookup(baseUrl, recommended.map(({ movieId, title, year }) => ({ movieId, title, year })));
    assert.equal(posterResponse.status, 200);
    const posterItems = (await posterResponse.json()).data.items;
    const postersByMovieLensId = new Map(posterItems.map(item => [item.movieId, item.posterUrl]));
    assert.equal(postersByMovieLensId.get(72261), 'https://image.tmdb.org/t/p/w500/pebble-penguin.jpg');
    assert.equal(postersByMovieLensId.get(3725), 'https://image.tmdb.org/t/p/w500/american-pop.jpg');
    assert.equal(postersByMovieLensId.get(99999), null);
  } finally {
    api.close();
    ml.close();
    tmdb.close();
    await Promise.all([once(api, 'close'), once(ml, 'close'), once(tmdb, 'close')]);
  }
});

test('validates poster requests and reports missing provider configuration', async () => {
  const api = createApp({ tmdbAccessToken: '', tmdbApiKey: '' }).listen(0, '127.0.0.1');
  await once(api, 'listening');
  const baseUrl = `http://127.0.0.1:${api.address().port}`;
  try {
    const invalid = await lookup(baseUrl, []);
    assert.equal(invalid.status, 400);
    const missing = await lookup(baseUrl);
    assert.equal(missing.status, 503);
    assert.equal((await missing.json()).error.code, 'POSTER_PROVIDER_NOT_CONFIGURED');
  } finally {
    api.close();
    await once(api, 'close');
  }
});

test('maps TMDB authorization, endpoint, timeout, and malformed-response failures safely', async t => {
  const scenarios = [
    { name: 'upstream 401', status: 401, code: 'POSTER_PROVIDER_AUTH_FAILED' },
    { name: 'upstream 404', status: 404, code: 'POSTER_PROVIDER_ENDPOINT_ERROR' },
    { name: 'timeout', timeoutMs: 20, code: 'POSTER_SERVICE_TIMEOUT', delayMs: 100 },
    { name: 'malformed response', status: 200, code: 'POSTER_SERVICE_INVALID_RESPONSE', body: '{"notResults":[]}' },
  ];
  for (const scenario of scenarios) {
    await t.test(scenario.name, async () => {
      await withPosterServer({
        apiKey: 'dummy-v3-api-key',
        timeoutMs: scenario.timeoutMs ?? 6000,
        respond: async (_req, res) => {
          if (scenario.delayMs) await new Promise(resolve => setTimeout(resolve, scenario.delayMs));
          if (res.destroyed) return;
          res.writeHead(scenario.status ?? 200, { 'content-type': 'application/json' });
          res.end(scenario.body ?? JSON.stringify({ message: 'upstream details and credentials must stay private' }));
        },
      }, async baseUrl => {
        const response = await lookup(baseUrl);
        assert.equal(response.status, scenario.code === 'POSTER_SERVICE_TIMEOUT' ? 504 : 502);
        const body = await response.json();
        assert.equal(body.error.code, scenario.code);
        assert.equal(JSON.stringify(body).includes('dummy-v3-api-key'), false);
        assert.equal(JSON.stringify(body).includes('upstream details'), false);
      });
    });
  }
});

test('keeps successful poster matches when another movie lookup fails', async () => {
  await withPosterServer({
    apiKey: 'dummy-v3-api-key',
    respond: async (_req, res, requestUrl) => {
      if (requestUrl.searchParams.get('query') === 'Toy Story') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ results: [{ title: 'Toy Story', release_date: '1995-11-22', poster_path: '/toy-story.jpg' }] }));
        return;
      }
      res.writeHead(503, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ message: 'upstream error' }));
    },
  }, async baseUrl => {
    const response = await lookup(baseUrl, [
      sampleMovie,
      { movieId: 2, title: 'An Upstream Failure (1990)', year: 1990 },
    ]);
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.equal(result.data.partial, true);
    assert.deepEqual(result.data.items, [
      { movieId: 1, posterUrl: 'https://image.tmdb.org/t/p/w500/toy-story.jpg' },
      { movieId: 2, posterUrl: null },
    ]);
  });
});
