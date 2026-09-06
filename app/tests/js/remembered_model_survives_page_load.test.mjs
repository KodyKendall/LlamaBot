// A user's remembered model was thrown away on every page load (0.7.7).
//
// chat.html carries a static <option> for each REGISTRY model. Config-registered
// models (OpenRouter endpoints) have none — they live in a host-mounted JSON file,
// so the markup cannot know their names, and index.js injects their options at
// runtime inside fetchAvailableModels() via addMissingModelOptions().
//
// Both startup paths that honour a user's own choice validated that choice
// against the dropdown BEFORE those options existed:
//
//   loadSettingsFromCookies()  -- static options only
//   checkModelParam()          -- static options only
//   fetchAvailableModels()
//     addMissingModelOptions() -- config models appear HERE
//     if (!userChoseModel) -> select default_model
//
// So on a box whose selectable models are all config-registered, the guard always
// failed, userChoseModel stayed false, and the server default overwrote the user's
// pick. Reported 2026-09-02 from box rsb-dev: "I switched my model and it keeps auto
// switching back to GLM 5.3." The same ordering also made ?llm_model= a silent no-op,
// after it had already stripped the param from the URL.
//
// The rule under test: a remembered choice is recorded as INTENT at startup and
// resolved against the dropdown only after the config-registered options exist.
// A value this build has no option for is still discarded, so a model dropped from
// the registry falls back to the server default rather than pinning a dead id.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

import { resolveRememberedModel } from '../../frontend/chat/utils/modelDefaults.js';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');

// Read off rsb-dev (0.7.6) on 2026-09-02. Every selectable model on that box is
// config-registered, so none of the three has a static <option>.
const RELACE = 'deepseek-flash-0731-relace';
const GLM = 'glm-5.3-flash-zai';
const DEEPSEEK = 'deepseek-v4-flash';

/** The dropdown as chat.html ships it: registry models only, plus the placeholder. */
const staticOptions = [{ value: '' }, { value: DEEPSEEK }];

/** The dropdown after addMissingModelOptions() injects the config-registered models. */
const hydratedOptions = [...staticOptions, { value: RELACE }, { value: GLM }];

// --- 1. the decision itself -------------------------------------------------

test('a config-registered model resolves once its option exists', () => {
  assert.deepEqual(
    resolveRememberedModel({ options: hydratedOptions, remembered: RELACE }),
    { select: RELACE, userChoseModel: true },
  );
});

test('the same model does NOT resolve against the static markup alone', () => {
  // This is the bug. Calling the resolver too early yields "no choice", which is
  // why the ordering assertions below matter as much as this one.
  assert.deepEqual(
    resolveRememberedModel({ options: staticOptions, remembered: RELACE }),
    { select: null, userChoseModel: false },
  );
});

test('a registry model still resolves, so registry boxes do not regress', () => {
  assert.deepEqual(
    resolveRememberedModel({ options: hydratedOptions, remembered: DEEPSEEK }),
    { select: DEEPSEEK, userChoseModel: true },
  );
});

test('a model dropped from the registry is discarded, not pinned', () => {
  // No option at all means this build cannot run the id. Reporting
  // userChoseModel: false lets the server default apply instead of leaving the
  // dropdown on a value setModel() would silently no-op on.
  assert.deepEqual(
    resolveRememberedModel({ options: hydratedOptions, remembered: 'retired-model' }),
    { select: null, userChoseModel: false },
  );
});

test('no remembered value means no choice', () => {
  for (const remembered of [null, undefined, '']) {
    assert.deepEqual(
      resolveRememberedModel({ options: hydratedOptions, remembered }),
      { select: null, userChoseModel: false },
    );
  }
});

test('a DISABLED option still counts as chosen', () => {
  // Availability is applied after this runs, and the existing needsNewSelection
  // repair owns that case: it switches to the first available model, leaves the
  // llmModel cookie intact and raises showModelSubstitutionNotice(). Discarding
  // the choice here instead would destroy the cookie's meaning on a box that is
  // only temporarily missing a key.
  assert.deepEqual(
    resolveRememberedModel({
      options: [{ value: '' }, { value: GLM, disabled: true }],
      remembered: GLM,
    }),
    { select: GLM, userChoseModel: true },
  );
});

// --- 2. the ordering that caused the bug ------------------------------------
//
// The helper above is only correct if index.js calls it at the right moment.
// These read the source, because the failure was never in the decision — it was
// in running the decision before the options existed.

test('the remembered choice is resolved AFTER the config options are injected', () => {
  const inject = INDEX_JS.indexOf('this.addMissingModelOptions(');
  const apply = INDEX_JS.indexOf('this.applyPendingModelChoice(');
  assert.ok(inject > 0, 'addMissingModelOptions call site not found');
  assert.ok(apply > 0, 'applyPendingModelChoice call site not found');
  assert.ok(
    apply > inject,
    'the remembered model is resolved before the config-registered options exist',
  );
});

test('the choice is resolved BEFORE availability is read off the dropdown', () => {
  // currentValue feeds needsNewSelection, the repair path that handles a
  // remembered model this box cannot run right now. Applying the choice after
  // that read would hide a disabled pick from the repair.
  const apply = INDEX_JS.indexOf('this.applyPendingModelChoice(');
  const currentValue = INDEX_JS.indexOf('let currentValue = this.elements.modelSelect.value');
  assert.ok(currentValue > 0, 'currentValue read not found');
  assert.ok(apply < currentValue, 'the choice is applied after availability is read');
});

test('cookie restore records intent instead of gating on the dropdown', () => {
  // The exact shape of the 0.7.6 bug: userChoseModel set only inside a
  // .some(...) test against options that do not exist yet.
  const cookieBlock = INDEX_JS.slice(
    INDEX_JS.indexOf("const savedModel = getCookie('llmModel')"),
    INDEX_JS.indexOf('async fetchAvailableModels'),
  );
  assert.ok(cookieBlock.length > 0, 'cookie restore block not found');
  assert.ok(
    cookieBlock.includes('this.pendingModelChoice = savedModel'),
    'the cookie is not recorded as a pending choice',
  );
  // The dropdown test may STAY — applying a registry model immediately is what
  // keeps registry boxes behaving exactly as before. What must not come back is
  // userChoseModel depending on it, because that flag is what stops the server
  // default from overwriting the user. So: intent first, dropdown second.
  const intent = cookieBlock.indexOf('this.userChoseModel = true');
  const dropdownTest = cookieBlock.indexOf('option.value === savedModel');
  assert.ok(intent > 0, 'the cookie no longer records a user choice at all');
  assert.ok(
    dropdownTest === -1 || intent < dropdownTest,
    'userChoseModel is still gated on a dropdown that is not populated yet',
  );
});

test('?llm_model= records intent instead of silently no-opping', () => {
  const paramBlock = INDEX_JS.slice(
    INDEX_JS.indexOf('checkModelParam() {'),
    INDEX_JS.indexOf('checkAgentModeParam() {'),
  );
  assert.ok(paramBlock.length > 0, 'checkModelParam body not found');
  assert.ok(
    paramBlock.includes('this.pendingModelChoice = model'),
    'the URL pin is not recorded as a pending choice',
  );
  assert.ok(
    !/const isValid =/.test(paramBlock),
    'checkModelParam still drops an unknown-looking pin before the options exist',
  );
  const paramIntent = paramBlock.indexOf('this.userChoseModel = true');
  const paramDropdownTest = paramBlock.indexOf('option.value === model');
  assert.ok(paramIntent > 0, 'the URL pin no longer records a user choice at all');
  assert.ok(
    paramDropdownTest === -1 || paramIntent < paramDropdownTest,
    'userChoseModel is still gated on a dropdown that is not populated yet',
  );
  // The param must still be stripped, or a refresh re-applies it after a manual
  // switch. That behaviour predates this fix and is not part of it.
  assert.ok(
    paramBlock.includes("url.searchParams.delete('llm_model')"),
    'the URL param is no longer stripped',
  );
});
