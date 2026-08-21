/** popup.js - signup / login / trial status / upgrade UI. */

import {
  ApiError,
  checkEntitlement,
  forgotPassword,
  getCachedEntitlement,
  isSignedIn,
  login,
  logout,
  me,
  openBillingPortal,
  signup,
  startCheckout,
} from './api.js';

const $ = (id) => document.getElementById(id);
let mode = 'login';

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
function showAuth() {
  $('auth-view').classList.remove('hidden');
  $('account-view').classList.add('hidden');
}

function showAccount() {
  $('auth-view').classList.add('hidden');
  $('account-view').classList.remove('hidden');
}

function setMode(next) {
  mode = next;
  const isSignup = next === 'signup';
  $('tab-login').classList.toggle('active', !isSignup);
  $('tab-signup').classList.toggle('active', isSignup);
  $('name-field').classList.toggle('hidden', !isSignup);
  $('signup-hint').classList.toggle('hidden', !isSignup);
  $('auth-submit').textContent = isSignup ? 'Create account' : 'Sign in';
  $('password').setAttribute(
    'autocomplete',
    isSignup ? 'new-password' : 'current-password'
  );
  $('auth-error').textContent = '';
}

$('tab-login').addEventListener('click', () => setMode('login'));
$('tab-signup').addEventListener('click', () => setMode('signup'));

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
$('auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('auth-submit');
  const errorEl = $('auth-error');
  errorEl.textContent = '';
  btn.disabled = true;

  const email = $('email').value.trim();
  const password = $('password').value;
  const fullName = $('fullName').value.trim();

  try {
    if (mode === 'signup') {
      await signup({ email, password, fullName });
    } else {
      await login({ email, password });
    }
    await renderAccount();
    showAccount();
  } catch (err) {
    errorEl.textContent = describe(err);
  } finally {
    btn.disabled = false;
  }
});

$('forgot').addEventListener('click', async () => {
  const email = $('email').value.trim();
  if (!email) {
    $('auth-error').textContent = 'Enter your email first.';
    return;
  }
  try {
    const res = await forgotPassword(email);
    $('auth-error').textContent = res.detail;
  } catch (err) {
    $('auth-error').textContent = describe(err);
  }
});

$('signout').addEventListener('click', async () => {
  await logout();
  showAuth();
  setMode('login');
});

// ---------------------------------------------------------------------------
// Account / entitlement
// ---------------------------------------------------------------------------
async function renderAccount() {
  // Paint the cached value first so the popup never looks blank.
  const cached = await getCachedEntitlement();
  if (cached) paint(cached);

  const [user, entitlement] = await Promise.all([
    me().catch(() => null),
    checkEntitlement().catch(() => null),
  ]);

  if (user) $('email-line').textContent = user.email;
  if (entitlement) paint(entitlement);
}

function paint(ent) {
  const isPro = ent.plan === 'subscription';
  const remaining = Math.min(ent.trials_remaining, ent.device_trials_remaining);
  const used = ent.trials_limit - remaining;

  $('plan-badge').textContent = isPro ? 'Pro' : 'Free';
  $('plan-badge').classList.toggle('pro', isPro);
  $('upgrade').classList.toggle('hidden', isPro);
  $('manage').classList.toggle('hidden', !isPro);

  if (isPro) {
    $('status-line').textContent = 'Subscription active';
    $('meter-fill').style.width = '100%';
    const end = ent.current_period_end
      ? new Date(ent.current_period_end).toLocaleDateString()
      : null;
    $('detail-line').textContent = end ? `Renews ${end}` : 'Billed $5/month';
    return;
  }

  $('status-line').textContent =
    remaining > 0
      ? `${remaining} of ${ent.trials_limit} free runs left`
      : 'Free trials used up';
  $('meter-fill').style.width = `${(used / ent.trials_limit) * 100}%`;

  const notes = {
    device_trial_exhausted:
      'The free runs for this device have already been used.',
    machine_trial_exhausted: 'This computer has used up its free runs.',
    email_verification_required: 'Verify your email to start your free runs.',
    account_disabled: 'This account is disabled.',
    device_blocked: 'This device has been blocked.',
  };
  $('detail-line').textContent =
    notes[ent.reason] || 'Subscribe for unlimited runs at $5/month.';
}

$('upgrade').addEventListener('click', async () => {
  $('upgrade').disabled = true;
  $('account-error').textContent = '';
  try {
    await startCheckout();
    window.close();
  } catch (err) {
    $('account-error').textContent = describe(err);
  } finally {
    $('upgrade').disabled = false;
  }
});

$('manage').addEventListener('click', async () => {
  $('account-error').textContent = '';
  try {
    await openBillingPortal();
    window.close();
  } catch (err) {
    $('account-error').textContent =
      err instanceof ApiError && err.status === 404
        ? 'No billing portal available for this account yet.'
        : describe(err);
  }
});

function describe(err) {
  if (err instanceof ApiError) {
    if (err.status === 409) return err.message;
    if (err.status === 429) return 'Too many attempts. Please wait a few minutes.';
    if (err.status === 422) return err.message;
    return err.message;
  }
  return 'Cannot reach the server. Check your connection.';
}

// ---------------------------------------------------------------------------
(async function boot() {
  setMode('login');
  if (await isSignedIn()) {
    showAccount();
    await renderAccount();
  } else {
    showAuth();
  }
})();
