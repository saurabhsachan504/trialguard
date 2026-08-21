/**
 * background.example.js - how to wire the gate into your existing MV3
 * service worker. Copy the relevant parts into your own background script.
 */

import { runGated, refreshBadge } from './gate.js';
import { isSignedIn } from './api.js';

// Keep the toolbar badge showing remaining trials.
chrome.runtime.onStartup.addListener(refreshBadge);
chrome.runtime.onInstalled.addListener(async () => {
  await refreshBadge();
  // First install: open the popup so the user registers straight away.
  if (!(await isSignedIn())) {
    chrome.tabs.create({ url: chrome.runtime.getURL('popup.html') });
  }
});

/**
 * Replace `doTheActualWork` with whatever your extension already does.
 * The important part is that the network call to /usage/consume happens
 * BEFORE the work, and the work only runs if the server said yes.
 */
async function doTheActualWork(payload) {
  // ... your existing feature logic ...
  return { processed: true, payload };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== 'RUN_FEATURE') return undefined;

  runGated('run_feature', () => doTheActualWork(message.payload), {
    meta: { tabId: sender.tab?.id ?? null },
  })
    .then(async (outcome) => {
      await refreshBadge();
      sendResponse(outcome);
    })
    .catch((err) => sendResponse({ ok: false, reason: 'error', message: err.message }));

  return true; // async response
});

// Re-check entitlement periodically so a subscription bought in another tab
// (or a cancellation) is reflected without the user reopening the popup.
chrome.alarms?.create('tg-refresh', { periodInMinutes: 30 });
chrome.alarms?.onAlarm.addListener((alarm) => {
  if (alarm.name === 'tg-refresh') refreshBadge();
});
