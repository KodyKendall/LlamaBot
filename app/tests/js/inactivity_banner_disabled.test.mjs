// 0.7.4: the yellow "Session expiring soon due to inactivity." banner is turned
// OFF, not deleted. The lease machinery behind it (activity sync to the backend,
// /api/lease-status, the banner markup and its "Continue Working" button) all
// stays in place — only the UI that pops the banner is suppressed, behind a
// single flag that flips back to true when we want it again.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');
const CHAT_HTML = readFileSync(resolve(APP_ROOT, 'frontend', 'chat.html'), 'utf8');

const { INACTIVITY_WARNING_BANNER_ENABLED } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'config.js')
);

/** Grab a method body out of index.js by name, up to the closing brace column. */
function methodBody(name) {
  const start = INDEX_JS.indexOf(`\n  ${name}(`);
  assert.notEqual(start, -1, `${name}() is missing from index.js`);
  const end = INDEX_JS.indexOf('\n  }', start);
  assert.notEqual(end, -1, `could not find the end of ${name}()`);
  return INDEX_JS.slice(start, end);
}

test('the banner is disabled by a single flag', () => {
  assert.equal(INACTIVITY_WARNING_BANNER_ENABLED, false);
});

test('index.js reads the flag instead of hardcoding the behaviour', () => {
  assert.match(INDEX_JS, /INACTIVITY_WARNING_BANNER_ENABLED/);
  assert.match(
    INDEX_JS,
    /import\s*\{[^}]*INACTIVITY_WARNING_BANNER_ENABLED[^}]*\}\s*from\s*'\.\/config\.js'/,
  );
});

test('nothing shows the banner while the flag is off', () => {
  assert.match(methodBody('showTimeoutWarning'), /if\s*\(\s*!INACTIVITY_WARNING_BANNER_ENABLED\s*\)\s*return/);
});

test('the inactivity poll does not run while the flag is off', () => {
  assert.match(methodBody('startInactivityCheck'), /if\s*\(\s*!INACTIVITY_WARNING_BANNER_ENABLED\s*\)\s*return/);
});

test('the functionality is kept, not deleted', () => {
  // Banner markup + its continue button survive in the page.
  assert.match(CHAT_HTML, /data-llamabot="timeout-warning"/);
  assert.match(CHAT_HTML, /data-llamabot="continue-session"/);
  // The lease/activity plumbing is untouched.
  assert.match(INDEX_JS, /\/api\/lease-status/);
  assert.match(INDEX_JS, /\/api\/update-activity/);
  assert.match(INDEX_JS, /checkInactivityWarning\s*\(\)\s*\{/);
  assert.match(INDEX_JS, /hideTimeoutWarning\s*\(\)\s*\{/);
});
