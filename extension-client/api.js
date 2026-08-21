/**
 * api.js - typed-ish client for the TrialGuard backend.
 *
 * Handles token storage, automatic access-token refresh (single-flight so
 * concurrent 401s do not each burn a refresh token) and the 402 "trial
 * exhausted" response.
 *
 * Nothing here decides entitlement. The server does. This file only reports
 * what the server said.
 */

import { getDeviceFingerprint } from './device.js';

export const API_BASE = 'http://localhost:8000/api/v1'; // <- change for prod

const TOKENS_KEY = 'tg_tokens';
const CACHE_KEY = 'tg_entitlement';

let refreshInFlight = null;

// ---------------------------------------------------------------------------
// Token storage
// ---------------------------------------------------------------------------
async function getTokens() {
  const s = await chrome.storage.local.get(TOKENS_KEY);
  return s[TOKENS_KEY] || null;
}

async function setTokens(tokens) {
  await chrome.storage.local.set({
    [TOKENS_KEY]: {
      access_token: tokens.access_token,
      refresh_token: tokens.refresh_token,
      // Refresh a minute early to avoid racing the expiry.
      expires_at: Date.now() + (tokens.expires_in - 60) * 1000,
    },
  });
}

async function clearTokens() {
  await chrome.storage.local.remove([TOKENS_KEY, CACHE_KEY]);
}

export async function isSignedIn() {
  return (await getTokens()) !== null;
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------
export class ApiError extends Error {
  constructor(status, detail, body) {
    super(typeof detail === 'string' ? detail : detail?.message || 'Request failed');
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
  }

  /** true when the user is out of trials and must subscribe */
  get needsSubscription() {
    return this.status === 402;
  }

  get entitlement() {
    return this.detail?.entitlement || null;
  }
}

async function request(path, { method = 'GET', body, auth = true, headers = {} } = {}) {
  const finalHeaders = { 'Content-Type': 'application/json', ...headers };

  if (auth) {
    const token = await getValidAccessToken();
    if (!token) throw new ApiError(401, 'Not signed in');
    finalHeaders.Authorization = `Bearer ${token}`;
  }

  let res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: finalHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  // One retry after a forced refresh, in case the token expired mid-flight.
  if (res.status === 401 && auth) {
    const refreshed = await refreshTokens();
    if (refreshed) {
      finalHeaders.Authorization = `Bearer ${refreshed.access_token}`;
      res = await fetch(`${API_BASE}${path}`, {
        method,
        headers: finalHeaders,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    }
  }

  const text = await res.text();
  const json = text ? safeParse(text) : null;

  if (!res.ok) {
    if (res.status === 401) await clearTokens();
    throw new ApiError(res.status, json?.detail ?? text, json);
  }
  return json;
}

function safeParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function getValidAccessToken() {
  const tokens = await getTokens();
  if (!tokens) return null;
  if (Date.now() < tokens.expires_at) return tokens.access_token;
  const refreshed = await refreshTokens();
  return refreshed?.access_token || null;
}

async function refreshTokens() {
  // Single-flight: a rotated refresh token is single-use, so parallel refreshes
  // would revoke the whole token family and sign the user out.
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    const tokens = await getTokens();
    if (!tokens?.refresh_token) return null;
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: tokens.refresh_token }),
      });
      if (!res.ok) {
        await clearTokens();
        return null;
      }
      const fresh = await res.json();
      await setTokens(fresh);
      return fresh;
    } catch {
      return null;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export async function signup({ email, password, fullName }) {
  const device = await getDeviceFingerprint();
  const data = await request('/auth/signup', {
    auth: false,
    method: 'POST',
    body: { email, password, full_name: fullName || null, device },
  });
  await setTokens(data.tokens);
  await cacheEntitlement(data.entitlement);
  return data;
}

export async function login({ email, password }) {
  const device = await getDeviceFingerprint();
  const data = await request('/auth/login', {
    auth: false,
    method: 'POST',
    body: { email, password, device },
  });
  await setTokens(data.tokens);
  await cacheEntitlement(data.entitlement);
  return data;
}

export async function logout({ allDevices = false } = {}) {
  const tokens = await getTokens();
  try {
    if (tokens) {
      await request('/auth/logout', {
        method: 'POST',
        body: { refresh_token: tokens.refresh_token, all_devices: allDevices },
      });
    }
  } finally {
    await clearTokens();
  }
}

export const me = () => request('/auth/me');
export const forgotPassword = (email) =>
  request('/auth/password/forgot', { auth: false, method: 'POST', body: { email } });

// ---------------------------------------------------------------------------
// Entitlement
// ---------------------------------------------------------------------------
async function cacheEntitlement(entitlement) {
  await chrome.storage.local.set({
    [CACHE_KEY]: { entitlement, fetched_at: Date.now() },
  });
}

/** Cheap read of the last known state - for painting UI instantly. */
export async function getCachedEntitlement() {
  const s = await chrome.storage.local.get(CACHE_KEY);
  return s[CACHE_KEY]?.entitlement || null;
}

/** Authoritative check. Does NOT spend a trial. */
export async function checkEntitlement() {
  const device = await getDeviceFingerprint();
  const entitlement = await request('/entitlement/check', {
    method: 'POST',
    body: { device },
  });
  await cacheEntitlement(entitlement);
  return entitlement;
}

/**
 * Spend one trial. Call this immediately before doing the paid work.
 * Throws ApiError with status 402 when the allowance is gone.
 */
export async function consumeUsage({ action = 'run', meta = null, idempotencyKey } = {}) {
  const device = await getDeviceFingerprint();
  const key = idempotencyKey || crypto.randomUUID();
  const result = await request('/usage/consume', {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: { device, action, meta, idempotency_key: key },
  });
  await cacheEntitlement(result.entitlement);
  return result;
}

// ---------------------------------------------------------------------------
// Billing
// ---------------------------------------------------------------------------
export const getPlans = () => request('/billing/plans', { auth: false });
export const getSubscription = () => request('/billing/subscription');

export async function startCheckout() {
  const session = await request('/billing/checkout', { method: 'POST', body: {} });
  await chrome.tabs.create({ url: session.checkout_url });
  return session;
}

/** Opens the provider's hosted portal where the user can cancel or update a card. */
export async function openBillingPortal() {
  const res = await request('/billing/portal', { method: 'POST' });
  await chrome.tabs.create({ url: res.detail });
  return res.detail;
}
