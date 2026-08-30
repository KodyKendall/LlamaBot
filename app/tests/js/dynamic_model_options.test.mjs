// Config-registered models need a dropdown <option> the markup can't contain (0.7.5).
//
// OpenRouter entries live in a host-mounted JSON file, so chat.html cannot know
// their names — but the availability pass in fetchAvailableModels() only ever
// walks options that ALREADY exist. Without a creation step the backend would
// offer a model the dropdown could never show, and setModel() on it would
// silently no-op, leaving the user on the previous model with no error.
//
// The rule under test: an option is created for every backend model carrying a
// `label`, and for no others — `label` is the backend saying "I am config-driven".
// A hardcoded model missing from the markup is a build error, and inventing an
// option for it at runtime would paper over genuine frontend/backend skew.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');

/**
 * addMissingModelOptions as index.js defines it, against a minimal <select>.
 * Exercised in isolation because importing index.js pulls in the whole app.
 */
function makeSelect(existingValues) {
  const options = existingValues.map(v => ({ value: v, dataset: {}, textContent: v }));
  return {
    options,
    appendChild: (o) => options.push(o),
  };
}

function addMissingModelOptions(select, models) {
  // Mirrors the method body; `document.createElement` is stubbed to a plain object.
  const existing = new Set(Array.from(select.options).map(o => o.value));
  models.forEach(m => {
    if (!m.label || existing.has(m.value)) return;
    const option = { value: m.value, dataset: {}, textContent: '' };
    option.value = m.value;
    option.textContent = m.label;
    if (m.short_label) option.dataset.shortLabel = m.short_label;
    select.appendChild(option);
    existing.add(m.value);
  });
}

test('a labelled model absent from the markup gets an option', () => {
  const select = makeSelect(['deepseek-v4-flash']);
  addMissingModelOptions(select, [
    { value: 'deepseek-flash-0731-relace', label: 'DeepSeek V4 Flash 0731 (Relace fp4)', short_label: 'DS 0731 Relace' },
  ]);
  const added = select.options.find(o => o.value === 'deepseek-flash-0731-relace');
  assert.ok(added, 'the config-registered model never reached the dropdown');
  assert.equal(added.textContent, 'DeepSeek V4 Flash 0731 (Relace fp4)');
  assert.equal(added.dataset.shortLabel, 'DS 0731 Relace');
});

test('a model already in the markup is left alone', () => {
  // Duplicating it would give the user two entries for one model, and the
  // availability pass would then only mark one of them.
  const select = makeSelect(['deepseek-v4-flash']);
  addMissingModelOptions(select, [{ value: 'deepseek-v4-flash', label: 'Renamed' }]);
  assert.equal(select.options.length, 1);
  assert.equal(select.options[0].textContent, 'deepseek-v4-flash', 'existing markup stays authoritative');
});

test('a model with no label is never invented', () => {
  // Hardcoded models ship no label. One missing from the markup is real skew and
  // must stay visible as such, not be silently patched over at runtime.
  const select = makeSelect(['deepseek-v4-flash']);
  addMissingModelOptions(select, [{ value: 'gpt-5-codex' }]);
  assert.equal(select.options.length, 1);
});

test('a model with no short_label still gets an option', () => {
  const select = makeSelect([]);
  addMissingModelOptions(select, [{ value: 'x', label: 'X Model' }]);
  assert.equal(select.options.length, 1);
  assert.equal(select.options[0].dataset.shortLabel, undefined);
});

test('duplicates within one payload are added once', () => {
  const select = makeSelect([]);
  addMissingModelOptions(select, [
    { value: 'x', label: 'X' },
    { value: 'x', label: 'X again' },
  ]);
  assert.equal(select.options.length, 1);
});

test('options are created BEFORE the availability pass', () => {
  // Order matters: the availability loop walks existing options only, so a
  // model created after it would never be greyed out when its key is missing.
  const creationAt = INDEX_JS.indexOf('this.addMissingModelOptions(data.models');
  const availabilityAt = INDEX_JS.indexOf('const modelAvailability = new Map(');
  assert.ok(creationAt > 0, 'addMissingModelOptions is not called from the fetch');
  assert.ok(availabilityAt > 0, 'availability map not found');
  assert.ok(creationAt < availabilityAt, 'options must be created before availability is applied');
});

test('the method guards a missing select', () => {
  // fetchAvailableModels runs on pages where the dropdown may not be mounted.
  assert.match(INDEX_JS, /addMissingModelOptions\(models\) \{\s*\n\s*const select = this\.elements\.modelSelect;\s*\n\s*if \(!select\) return;/);
});
