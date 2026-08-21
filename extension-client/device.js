/**
 * device.js - builds the device fingerprint the backend uses for trial limits.
 *
 * WHY NOT THE MAC ADDRESS?
 * A Chrome extension runs in the browser sandbox, which exposes no network
 * hardware API. There is no way to read a MAC address from extension JS -
 * chrome.system.network is ChromeOS-kiosk only, and even there it is gated.
 * So we build the strongest identifier the platform allows:
 *
 *   1. installation_id  - a random UUID persisted in chrome.storage.local
 *   2. stable hardware traits - platform, screen geometry, CPU cores, RAM,
 *      GPU renderer string, timezone, language
 *
 * The backend HMACs these together. Clearing extension storage alone does not
 * mint a new device because the hardware traits stay the same; a determined
 * user can still change them, which is exactly why the backend ALSO caps
 * accounts per device and never trusts anything this file reports.
 *
 * If you later ship a native messaging helper that can read the real MAC,
 * set `mac_address` on the returned object and the backend will key off that
 * instead - no other change needed.
 */

const STORAGE_KEY = 'tg_device';

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, (c) =>
    (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16)
  );
}

/** GPU renderer string - stable per machine, unavailable in a worker. */
function readGpu() {
  try {
    if (typeof document === 'undefined') return null;
    const canvas = document.createElement('canvas');
    const gl =
      canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) return null;
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    if (!ext) return null;
    return String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)).slice(0, 200);
  } catch {
    return null;
  }
}

function readScreen() {
  if (typeof screen === 'undefined') return null;
  return `${screen.width}x${screen.height}x${screen.colorDepth}`;
}

function readBrand() {
  const brands = navigator.userAgentData?.brands;
  if (Array.isArray(brands)) {
    return brands.map((b) => `${b.brand} ${b.version}`).join('|').slice(0, 200);
  }
  return (navigator.userAgent || '').slice(0, 200);
}

async function loadStored() {
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  return stored[STORAGE_KEY] || null;
}

/**
 * Ask the optional native helper for the real MAC address.
 * Returns null when the helper is not installed - which is the normal case.
 * Requires the "nativeMessaging" permission in the manifest.
 */
const NATIVE_HOST = 'com.trialguard.host';

async function tryNativeMac() {
  if (!chrome.runtime?.sendNativeMessage) return null;
  try {
    const res = await chrome.runtime.sendNativeMessage(NATIVE_HOST, {});
    return res?.ok && res.mac_address ? res.mac_address : null;
  } catch {
    return null; // helper not installed
  }
}

/**
 * Returns the fingerprint object to send with every API call.
 * Cached in chrome.storage.local so it stays stable across restarts.
 */
export async function getDeviceFingerprint() {
  let record = await loadStored();

  if (!record || !record.installation_id) {
    record = { installation_id: uuid(), created_at: Date.now() };
  }

  // Cached because the native round-trip is slow; re-checked once a day.
  const macStale = !record.mac_checked_at || Date.now() - record.mac_checked_at > 864e5;
  if (macStale) {
    const mac = await tryNativeMac();
    record.mac_checked_at = Date.now();
    if (mac) record.mac_address = mac;
  }

  const fingerprint = {
    installation_id: record.installation_id,
    platform: navigator.platform || navigator.userAgentData?.platform || null,
    user_agent_brand: readBrand(),
    screen: readScreen(),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
    language: navigator.language || null,
    hardware_concurrency: navigator.hardwareConcurrency || null,
    device_memory: navigator.deviceMemory || null,
    gpu: record.gpu || readGpu(),
    // Present only when the optional native helper is installed. When set, the
    // backend keys the device off this alone.
    mac_address: record.mac_address || null,
    extension_version: chrome.runtime.getManifest().version,
    label: record.label || null,
  };

  // Persist the GPU string: it can only be read from a document context, so
  // cache it the first time the popup runs and reuse it in the worker.
  if (fingerprint.gpu && fingerprint.gpu !== record.gpu) {
    record.gpu = fingerprint.gpu;
  }
  await chrome.storage.local.set({ [STORAGE_KEY]: record });

  // Drop nulls - the server ignores empty fields when hashing.
  return Object.fromEntries(
    Object.entries(fingerprint).filter(([, v]) => v !== null && v !== '')
  );
}

/** Optional: label this device so the user recognises it in account settings. */
export async function setDeviceLabel(label) {
  const record = (await loadStored()) || { installation_id: uuid() };
  record.label = String(label).slice(0, 120);
  await chrome.storage.local.set({ [STORAGE_KEY]: record });
}
