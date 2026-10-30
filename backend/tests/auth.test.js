import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import mongoose from 'mongoose';
import 'dotenv/config';
import test, { after, before } from 'node:test';
import { createApp } from '../src/app.js';
import { createModels } from '../src/models.js';
import { connectDatabase } from '../src/database.js';

const sourceUri = process.env.MONGODB_URI;
if (!sourceUri) throw new Error('Set MONGODB_URI in backend/.env to run the MongoDB integration tests.');
const testDb = `cinemood_test_${process.pid}`;
const testUri = new URL(sourceUri);
testUri.pathname = `/${testDb}`;
let connection;
let models;
let apiServer;
let baseUrl;
const ownedEmails = new Set();
const receivedMlBodies = [];
const frontendOrigin = 'http://localhost:5173';

before(async () => {
  connection = await mongoose.createConnection(testUri.toString(), { serverSelectionTimeoutMS: 2500 }).asPromise();
  models = createModels(connection);
  await Promise.all(Object.values(models).map(model => model.init()));
  // The ratings route validates movies through the ML client. Point its lookup to a local stub.
  const ml = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    if (req.method === 'POST') receivedMlBodies.push({ url: req.url, body: body ? JSON.parse(body) : null });
    if (req.url.startsWith('/movies/42')) {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ data: { movieId: 42, title: 'Test Movie' } }));
    }
    if (req.url === '/recommendations/hybrid' || req.url === '/recommendations/collaborative') {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ data: { items: [{ movieId: 99, title: 'Recommendation' }], total: 1, method: req.url.includes('hybrid') ? 'hybrid/content+collaborative' : 'collaborative/item-cosine' } }));
    }
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ detail: 'not found' }));
  });
  ml.listen(0, '127.0.0.1');
  await once(ml, 'listening');
  // createApp has already created its axios client, so build another app with the active stub URL.
  const testApp = createApp({ frontendOrigin, mlServiceUrl: `http://127.0.0.1:${ml.address().port}`, mlTimeoutMs: 250, models });
  apiServer = testApp.listen(0, '127.0.0.1');
  apiServer.mlServer = ml;
  await once(apiServer, 'listening');
  baseUrl = `http://127.0.0.1:${apiServer.address().port}`;
});

after(async () => {
  if (models) {
    const users = await models.User.find({ email: { $in: [...ownedEmails] } }).select('_id').lean();
    const userIds = users.map(user => user._id);
    await Promise.all([
      models.MovieRating.deleteMany({ userId: { $in: userIds } }),
      models.Session.deleteMany({ userId: { $in: userIds } }),
      models.User.deleteMany({ _id: { $in: userIds } }),
    ]);
  }
  if (apiServer) {
    const mlServer = apiServer.mlServer;
    apiServer.close();
    mlServer.close();
    await Promise.all([once(apiServer, 'close'), once(mlServer, 'close')]);
  }
  await connection?.close();
});

function account(email = `${randomUUID()}@example.test`) {
  ownedEmails.add(email.toLowerCase());
  return { name: 'Cine Test', email, password: 'a-long-test-password-42' };
}

async function request(path, { method = 'GET', body, cookie } = {}) {
  const headers = { Origin: frontendOrigin };
  if (body !== undefined) headers['content-type'] = 'application/json';
  if (cookie) headers.cookie = cookie;
  const response = await fetch(`${baseUrl}/api${path}`, { method, headers, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
  const setCookie = response.headers.get('set-cookie');
  return { response, data: await response.json(), cookie: setCookie?.split(';', 1)[0] };
}

test('registers, normalizes email, rejects duplicate email, and never returns password material', async () => {
  const details = account();
  const registered = await request('/auth/register', { method: 'POST', body: details });
  assert.equal(registered.response.status, 201);
  assert.equal(registered.data.data.user.email, details.email.toLowerCase());
  assert.match(registered.cookie, /^cinemood_session=/);
  assert.equal(JSON.stringify(registered.data).includes('passwordHash'), false);
  const stored = await models.User.findOne({ email: details.email.toLowerCase() }).select('+passwordHash');
  assert.notEqual(stored.passwordHash, details.password);
  assert.equal(stored.passwordHash.startsWith('scrypt$'), true);
  const duplicate = await request('/auth/register', { method: 'POST', body: { ...details, email: details.email.toUpperCase() } });
  assert.equal(duplicate.response.status, 409);
});

test('rejects invalid registration and wrong login credentials', async () => {
  const invalid = await request('/auth/register', { method: 'POST', body: { name: 'x', email: 'bad', password: 'short' } });
  assert.equal(invalid.response.status, 400);
  const details = account();
  await request('/auth/register', { method: 'POST', body: details });
  const wrong = await request('/auth/login', { method: 'POST', body: { email: details.email, password: 'not-the-password' } });
  assert.equal(wrong.response.status, 401);
  const valid = await request('/auth/login', { method: 'POST', body: { email: details.email.toUpperCase(), password: details.password } });
  assert.equal(valid.response.status, 200);
  assert.match(valid.cookie, /^cinemood_session=/);
});

test('restores sessions and persists ratings scoped to the authenticated user', async () => {
  const first = account();
  const second = account();
  const createdFirst = await request('/auth/register', { method: 'POST', body: first });
  const createdSecond = await request('/auth/register', { method: 'POST', body: second });
  assert.equal((await request('/ratings')).response.status, 401);
  const me = await request('/auth/me', { cookie: createdFirst.cookie });
  assert.equal(me.data.data.authenticated, true);
  const saveA = await request('/ratings/42', { method: 'PUT', cookie: createdFirst.cookie, body: { rating: 4.5 } });
  const saveB = await request('/ratings/42', { method: 'PUT', cookie: createdSecond.cookie, body: { rating: 2 } });
  assert.equal(saveA.response.status, 200);
  assert.equal(saveB.response.status, 200);
  assert.deepEqual((await request('/ratings', { cookie: createdFirst.cookie })).data.data.items, [{ movieId: 42, rating: 4.5 }]);
  assert.deepEqual((await request('/ratings', { cookie: createdSecond.cookie })).data.data.items, [{ movieId: 42, rating: 2 }]);
  const personalized = await request('/recommendations/personalized', { method: 'POST', cookie: createdFirst.cookie, body: { method: 'hybrid' } });
  assert.equal(personalized.response.status, 200);
  assert.equal(personalized.data.data.items[0].movieId, 99);
  const forwarded = receivedMlBodies.at(-1);
  assert.equal(forwarded.url, '/recommendations/hybrid');
  assert.deepEqual(forwarded.body.ratings, [{ movieId: 42, rating: 4.5 }]);
  await request('/ratings/42', { method: 'PUT', cookie: createdFirst.cookie, body: { rating: 5 } });
  const savedDocuments = await models.MovieRating.find({ userId: { $in: [createdFirst.data.data.user.id, createdSecond.data.data.user.id] } }).lean();
  assert.equal(savedDocuments.length, 2);
  assert.deepEqual((await request('/ratings', { cookie: createdFirst.cookie })).data.data.items, [{ movieId: 42, rating: 5 }]);
  const deleteSecond = await request('/ratings/42', { method: 'DELETE', cookie: createdSecond.cookie });
  assert.equal(deleteSecond.data.data.deleted, true);
  assert.deepEqual((await request('/ratings', { cookie: createdFirst.cookie })).data.data.items, [{ movieId: 42, rating: 5 }]);
  const invalid = await request('/ratings/42', { method: 'PUT', cookie: createdFirst.cookie, body: { rating: 5.25 } });
  assert.equal(invalid.response.status, 400);
  const logout = await request('/auth/logout', { method: 'POST', cookie: createdFirst.cookie });
  assert.equal(logout.response.status, 200);
  assert.equal((await request('/ratings', { cookie: createdFirst.cookie })).response.status, 401);
});

test('enforces origin checks for cookie-authenticated mutations', async () => {
  const details = account();
  const rejected = await fetch(`${baseUrl}/api/auth/register`, { method: 'POST', headers: { 'content-type': 'application/json', Origin: 'https://untrusted.example' }, body: JSON.stringify(details) });
  assert.equal(rejected.status, 403);
});

test('returns a cold-start response for accounts without stored ratings', async () => {
  const details = account();
  const registered = await request('/auth/register', { method: 'POST', body: details });
  const personalized = await request('/recommendations/personalized', { method: 'POST', cookie: registered.cookie, body: {} });
  assert.equal(personalized.response.status, 200);
  assert.deepEqual(personalized.data.data.items, []);
  assert.equal(personalized.data.data.method, 'cold-start');
});

test('reports database connection failure without exposing the URI', async () => {
  await assert.rejects(connectDatabase('mongodb://127.0.0.1:1/unreachable', { serverSelectionTimeoutMS: 100 }), error => {
    assert.match(error.message, /MongoDB could not be reached/);
    assert.equal(error.message.includes('127.0.0.1'), false);
    return true;
  });
});
