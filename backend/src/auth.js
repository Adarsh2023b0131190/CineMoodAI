import { createHash, randomBytes, scrypt as scryptCallback, timingSafeEqual } from 'node:crypto';
import { promisify } from 'node:util';
import { Session, User } from './models.js';

const scrypt = promisify(scryptCallback);
const COOKIE = 'cinemood_session';
const SESSION_DAYS = 7;

export function hashToken(token) {
  return createHash('sha256').update(token).digest('hex');
}

export async function hashPassword(password) {
  const salt = randomBytes(16);
  const derived = await scrypt(password, salt, 64);
  return `scrypt$${salt.toString('hex')}$${Buffer.from(derived).toString('hex')}`;
}

export async function verifyPassword(password, encoded) {
  const [scheme, saltHex, hashHex] = String(encoded || '').split('$');
  if (scheme !== 'scrypt' || !saltHex || !hashHex) return false;
  const expected = Buffer.from(hashHex, 'hex');
  const actual = Buffer.from(await scrypt(password, Buffer.from(saltHex, 'hex'), expected.length));
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

export function cookieName() { return COOKIE; }
export function sessionCookie(token, production = process.env.NODE_ENV === 'production', sameSite = process.env.SESSION_COOKIE_SAME_SITE || (production ? 'None' : 'Strict')) {
  const normalizedSameSite = ({ strict: 'Strict', lax: 'Lax', none: 'None' })[String(sameSite).toLowerCase()];
  if (!normalizedSameSite) throw new Error('SESSION_COOKIE_SAME_SITE must be Strict, Lax, or None');
  if (normalizedSameSite === 'None' && !production) throw new Error('SameSite=None cookies require production Secure cookies');
  return `${COOKIE}=${encodeURIComponent(token)}; HttpOnly; SameSite=${normalizedSameSite}; Path=/api; Max-Age=${SESSION_DAYS * 86400}${production ? '; Secure' : ''}`;
}
export function clearSessionCookie(production = process.env.NODE_ENV === 'production', sameSite = process.env.SESSION_COOKIE_SAME_SITE || (production ? 'None' : 'Strict')) {
  const normalizedSameSite = ({ strict: 'Strict', lax: 'Lax', none: 'None' })[String(sameSite).toLowerCase()];
  if (!normalizedSameSite) throw new Error('SESSION_COOKIE_SAME_SITE must be Strict, Lax, or None');
  if (normalizedSameSite === 'None' && !production) throw new Error('SameSite=None cookies require production Secure cookies');
  return `${COOKIE}=; HttpOnly; SameSite=${normalizedSameSite}; Path=/api; Max-Age=0${production ? '; Secure' : ''}`;
}

function readCookie(req) {
  const entry = (req.headers.cookie || '').split(';').map(part => part.trim()).find(part => part.startsWith(`${COOKIE}=`));
  if (!entry) return null;
  try { return decodeURIComponent(entry.slice(COOKIE.length + 1)); } catch { return null; }
}

export function createAuth({ UserModel = User, SessionModel = Session, cookieSameSite = process.env.SESSION_COOKIE_SAME_SITE || (process.env.NODE_ENV === 'production' ? 'None' : 'Strict') } = {}) {
  async function createSession(userId, res) {
    const token = randomBytes(32).toString('base64url');
    const expiresAt = new Date(Date.now() + SESSION_DAYS * 86400_000);
    await SessionModel.create({ tokenHash: hashToken(token), userId, expiresAt });
    res.setHeader('Set-Cookie', sessionCookie(token, process.env.NODE_ENV === 'production', cookieSameSite));
  }

  async function authenticate(req) {
    const token = readCookie(req);
    if (!token) return null;
    const session = await SessionModel.findOne({ tokenHash: hashToken(token), expiresAt: { $gt: new Date() } });
    if (!session) return null;
    return UserModel.findById(session.userId).select('-passwordHash');
  }

  return {
    authenticate,
    async register(req, res, next) {
      try {
        const name = typeof req.body?.name === 'string' ? req.body.name.trim() : '';
        const email = typeof req.body?.email === 'string' ? req.body.email.trim().toLowerCase() : '';
        const password = req.body?.password;
        if (!name || name.length > 80 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 254 || typeof password !== 'string' || password.length < 10 || password.length > 128) {
          return res.status(400).json({ error: { code: 'INVALID_REGISTRATION', message: 'Provide a name, valid email, and password of 10 to 128 characters.' } });
        }
        const user = await UserModel.create({ name, email, passwordHash: await hashPassword(password) });
        await createSession(user._id, res);
        return res.status(201).json({ data: { user: { id: String(user._id), name: user.name, email: user.email } } });
      } catch (error) {
        if (error?.code === 11000) return res.status(409).json({ error: { code: 'EMAIL_IN_USE', message: 'An account with this email already exists.' } });
        return next(error);
      }
    },
    async login(req, res, next) {
      try {
        const email = typeof req.body?.email === 'string' ? req.body.email.trim().toLowerCase() : '';
        const password = req.body?.password;
        if (!email || typeof password !== 'string' || password.length > 128) return res.status(400).json({ error: { code: 'INVALID_LOGIN', message: 'Email and password are required.' } });
        const user = await UserModel.findOne({ email }).select('+passwordHash');
        const valid = user ? await verifyPassword(password, user.passwordHash) : false;
        if (!valid) return res.status(401).json({ error: { code: 'INVALID_CREDENTIALS', message: 'Email or password is incorrect.' } });
        await createSession(user._id, res);
        return res.json({ data: { user: { id: String(user._id), name: user.name, email: user.email } } });
      } catch (error) { return next(error); }
    },
    async logout(req, res, next) {
      try {
        const token = readCookie(req);
        if (token) await SessionModel.deleteOne({ tokenHash: hashToken(token) });
        res.setHeader('Set-Cookie', clearSessionCookie(process.env.NODE_ENV === 'production', cookieSameSite));
        return res.json({ data: { loggedOut: true } });
      } catch (error) { return next(error); }
    },
    async currentUser(req, res, next) {
      try {
        const user = await authenticate(req);
        return res.json({ data: { authenticated: Boolean(user), user: user ? { id: String(user._id), name: user.name, email: user.email } : null } });
      } catch (error) { return next(error); }
    },
    async requireUser(req, res, next) {
      try {
        const user = await authenticate(req);
        if (!user) return res.status(401).json({ error: { code: 'AUTH_REQUIRED', message: 'Please sign in to access this resource.' } });
        req.user = user;
        return next();
      } catch (error) { return next(error); }
    },
  };
}
