/**
 * gate.js - wrap any paid feature so it consumes exactly one trial.
 *
 * Usage inside your existing background service worker:
 *
 *   import { runGated } from './gate.js';
 *
 *   chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
 *     if (msg.type === 'DO_THE_THING') {
 *       runGated('do_the_thing', () => doTheThing(msg.payload))
 *         .then(sendResponse)
 *         .catch((e) => sendResponse({ ok: false, error: e.message }));
 *       return true; // keep the message channel open
 *     }
 *   });
 */

import { ApiError, checkEntitlement, consumeUsage, getCachedEntitlement } from './api.js';

/**
 * Runs `work` only if the server grants entitlement, charging one trial.
 *
 * Returns { ok: true, result, entitlement } on success, or
 *         { ok: false, reason: 'subscription_required', entitlement } when out
 *         of trials, or { ok: false, reason: 'signin_required' }.
 */
export async function runGated(action, work, { meta = null } = {}) {
  let consumed;
  try {
    consumed = await consumeUsage({ action, meta });
  } catch (err) {
    if (err instanceof ApiError && err.needsSubscription) {
      await notifyUpgrade(err.entitlement);
      return {
        ok: false,
        reason: 'subscription_required',
        entitlement: err.entitlement,
        message: err.message,
      };
    }
    if (err instanceof ApiError && err.status === 401) {
      return { ok: false, reason: 'signin_required', message: 'Please sign in.' };
    }
    // Network/server problem: fail closed, and say so honestly.
    return { ok: false, reason: 'unavailable', message: err.message };
  }

  try {
    const result = await work();
    return { ok: true, result, entitlement: consumed.entitlement };
  } catch (err) {
    // The trial was already charged. Log it so you can refund via
    // POST /admin/grant-trials if the failure was your fault.
    console.error('[TrialGuard] paid work failed after consuming a trial', err);
    throw err;
  }
}

/** Badge + notification when the free allowance runs out. */
async function notifyUpgrade(entitlement) {
  try {
    await chrome.action.setBadgeText({ text: '!' });
    await chrome.action.setBadgeBackgroundColor({ color: '#d93025' });
    if (chrome.notifications) {
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icons/icon128.png',
        title: 'Free trials used up',
        message: 'Subscribe for $5/month to keep using the extension.',
        priority: 2,
      });
    }
  } catch {
    /* notifications permission is optional */
  }
  return entitlement;
}

/** Paint the toolbar badge with the remaining trial count. */
export async function refreshBadge() {
  let ent = await getCachedEntitlement();
  try {
    ent = await checkEntitlement();
  } catch {
    /* offline - fall back to the cached value */
  }
  if (!ent) return;

  if (ent.plan === 'subscription') {
    await chrome.action.setBadgeText({ text: '' });
    return;
  }
  const remaining = Math.min(ent.trials_remaining, ent.device_trials_remaining);
  await chrome.action.setBadgeText({ text: remaining > 0 ? String(remaining) : '!' });
  await chrome.action.setBadgeBackgroundColor({
    color: remaining > 0 ? '#1a73e8' : '#d93025',
  });
}
