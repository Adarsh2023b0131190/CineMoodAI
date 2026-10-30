import assert from 'node:assert/strict';
import test from 'node:test';
import { clearSessionCookie, sessionCookie } from '../src/auth.js';

test('production cross-site session cookies are Secure, HttpOnly, and SameSite=None', () => {
  const cookie = sessionCookie('token-value', true, 'None');
  assert.match(cookie, /HttpOnly/);
  assert.match(cookie, /SameSite=None/);
  assert.match(cookie, /; Secure/);
  const cleared = clearSessionCookie(true, 'None');
  assert.match(cleared, /SameSite=None/);
  assert.match(cleared, /; Secure/);
});

test('local strict cookies remain supported and insecure cross-site cookies are rejected', () => {
  assert.match(sessionCookie('token-value', false, 'Strict'), /SameSite=Strict/);
  assert.doesNotMatch(sessionCookie('token-value', false, 'Strict'), /Secure/);
  assert.throws(() => sessionCookie('token-value', false, 'None'), /require production Secure cookies/);
  assert.throws(() => sessionCookie('token-value', true, 'invalid'), /must be Strict, Lax, or None/);
});
