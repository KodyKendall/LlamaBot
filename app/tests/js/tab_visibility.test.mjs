// The Code tab is hidden by default now, because the VS Code editor container
// is off by default. A hidden tab must not load its iframe: the editor is not
// running, so the browser would retry a dead address in the background.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

const { isTabVisible } = await import(
  resolve(APP_ROOT, 'frontend/chat/utils/tabVisibility.js')
);

test('a hidden Code tab does not count as visible', () => {
  assert.equal(isTabVisible('vsCodeFrame', ['liveSiteFrame', 'inboxFrame']), false);
});

test('an enabled Code tab counts as visible', () => {
  assert.equal(isTabVisible('vsCodeFrame', ['liveSiteFrame', 'vsCodeFrame']), true);
});

test('the App tab is visible even when the list omits it', () => {
  assert.equal(isTabVisible('liveSiteFrame', []), true);
});

test('a missing settings list shows everything', () => {
  assert.equal(isTabVisible('vsCodeFrame', undefined), true);
  assert.equal(isTabVisible('vsCodeFrame', null), true);
  assert.equal(isTabVisible('vsCodeFrame', 'vsCodeFrame'), true);
});
