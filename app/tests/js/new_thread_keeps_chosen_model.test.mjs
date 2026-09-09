// Starting a new thread threw away the user's model AND persisted the box default
// as their preference (0.7.8).
//
// 0.7.7 fixed the page-load half of this (see remembered_model_survives_page_load).
// This is the other half, reported from the same box (rsb-dev, 2026-09-03):
// "Finally found an example of it defaulting back. You can see it is referring to
// Chat since I was using Luna but it says DeepSeek in the dropdown."
//
// The createNewThread handler called setModel(this.defaultTextModel) unconditionally,
// and setModel() writes the llmModel cookie every time. Two consequences:
//
//   1. The chosen model is discarded on every new thread.
//   2. The box default is written INTO the cookie as if the user had picked it —
//      exactly what the comment in fetchAvailableModels() says must never happen
//      ("writing it would freeze today's default onto the user forever"). One new
//      thread converts a user from "follows the fleet default" to "pinned to
//      whatever the default was that day", and later fleet changes never reach them.
//
// And the handler is not user-initiated, despite its comment. The createNewThread
// event has three dispatchers and two of them fire from the AGENT mid-conversation,
// then auto-send the user's text on the new thread:
//
//   ui/MenuManager.js               -- the New thread button      (the user)
//   websocket/MessageHandler.js     -- suggest_mode_switch        (beginner agent)
//   websocket/MessageHandler.js     -- handleImplementTicket      (ticket agent)
//
// That is the customer's screenshot exactly: in ticket mode on Luna, they accepted
// the "implement it" card, the frontend minted a new thread, reset the dropdown, and
// carried the conversation over — so the reply still refers to Chat while the
// dropdown reads DeepSeek.
//
// The rule under test: a new thread keeps a model the user chose, and a model the
// user did NOT choose is never written to the cookie.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

import { resolveNewThreadModel } from '../../frontend/chat/utils/modelDefaults.js';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');

// Read off rsb-dev on 2026-09-03. Luna has a static <option>; the box default did not
// stay still — a tab opened before 01:58 UTC carried deepseek, one opened after
// carried muse. Same defect, two values, which is why the test pins neither.
const LUNA = 'gpt-5.6-luna-chatgpt';
const DEEPSEEK = 'deepseek-v4-flash';

// --- 1. the decision itself -------------------------------------------------

test('a new thread keeps the model the user chose', () => {
  assert.deepEqual(
    resolveNewThreadModel({ userChoseModel: true, defaultTextModel: DEEPSEEK }),
    { select: null, persist: false },
  );
});

test('a new thread falls back to the box default when the user chose nothing', () => {
  assert.deepEqual(
    resolveNewThreadModel({ userChoseModel: false, defaultTextModel: DEEPSEEK }),
    { select: DEEPSEEK, persist: false },
  );
});

test('the fallback is NEVER persisted — that is the cookie-poisoning half', () => {
  const { persist } = resolveNewThreadModel({ userChoseModel: false, defaultTextModel: DEEPSEEK });
  assert.equal(persist, false, 'writing the default to the cookie pins the user to it forever');
});

test('a user choice survives even when the box has no default to fall back to', () => {
  assert.deepEqual(
    resolveNewThreadModel({ userChoseModel: true, defaultTextModel: null }),
    { select: null, persist: false },
  );
});

test('nothing is selected when there is neither a choice nor a default', () => {
  assert.deepEqual(
    resolveNewThreadModel({ userChoseModel: false, defaultTextModel: null }),
    { select: null, persist: false },
  );
});

// --- 2. the wiring ----------------------------------------------------------
//
// The helper is only correct if index.js actually routes through it. These read the
// source because importing index.js pulls in the entire UI.

test('setModel takes a persist option so a default can be applied without saving it', () => {
  const signature = INDEX_JS.match(/^\s*setModel\((.*?)\)\s*\{/m);
  assert.ok(signature, 'setModel() not found in index.js');
  assert.match(
    signature[1],
    /persist/,
    'setModel() still writes the llmModel cookie unconditionally, so every ' +
    'programmatic switch is recorded as if the user had chosen it',
  );
});

test('setModel only writes the cookie when persisting', () => {
  const body = INDEX_JS.match(/^\s*setModel\(.*?\)\s*\{([\s\S]*?)^\s{2}\}/m);
  assert.ok(body, 'setModel() body not found');
  const cookieLine = body[1].split('\n').find(l => l.includes("setCookie('llmModel'"));
  assert.ok(cookieLine, "setModel() no longer writes the llmModel cookie at all — a manual pick must still persist");
  assert.match(
    cookieLine,
    /if\s*\(persist\)/,
    'the llmModel cookie is written unconditionally inside setModel()',
  );
});

test('the createNewThread handler asks the helper instead of resetting outright', () => {
  const handler = INDEX_JS.match(/addEventListener\('createNewThread'[\s\S]*?\n {4}\}\);/);
  assert.ok(handler, "the createNewThread handler was not found");

  assert.doesNotMatch(
    handler[0],
    /this\.setModel\(this\.defaultTextModel\)\s*;/,
    'the handler still resets to the box default unconditionally, discarding the ' +
    "user's model on every new thread — including the two the AGENT dispatches",
  );
  assert.match(
    handler[0],
    /resolveNewThreadModel/,
    'the handler does not consult resolveNewThreadModel',
  );
});

test('the policy lock and the image auto-switch do not persist either', () => {
  // Neither is a user choice: the lock pins the default because switching is
  // disabled, and the image path switches because the chosen model cannot see
  // images. Persisting either one silently rewrites the user's preference.
  const programmatic = [...INDEX_JS.matchAll(/this\.setModel\(\s*(this\.defaultTextModel|imageModel)\s*(,[^)]*)?\)/g)];
  assert.ok(programmatic.length >= 3, `expected the programmatic setModel call sites, found ${programmatic.length}`);

  for (const call of programmatic) {
    assert.match(
      call[0],
      /persist:\s*false/,
      `${call[0]} persists a model the user did not choose`,
    );
  }
});
