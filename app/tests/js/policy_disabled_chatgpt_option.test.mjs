// A ChatGPT model the operator disabled must be greyed out, not "(Connect account)".
//
// 0.7.10 on a ZDR box: every other model was greyed out, but ChatGPT Luna and Sol
// stayed selectable as "(Connect account)" because the picker re-enabled any
// unavailable ChatGPT model so a not-yet-connected user could reach the sign-in
// modal. That also re-enabled models the operator had disabled, and let a user
// link an OpenAI account on a FERPA box.
//
// The rule under test: a ChatGPT option stays selectable only when policy allows
// it and it is merely not connected. Policy-disabled means disabled, like every
// other model.

import assert from 'node:assert/strict';
import test from 'node:test';

import { modelOptionState } from '../../frontend/chat/utils/modelDefaults.js';

test('a policy-disabled ChatGPT model is disabled, not a connect prompt', () => {
  assert.equal(
    modelOptionState({ available: false, requiresChatGptLogin: true, disabledByPolicy: true }),
    'disabled',
  );
});

test('an allowed but not-connected ChatGPT model is still a connect prompt', () => {
  // Base-plan boxes run ONLY these two models; this is how users connect.
  assert.equal(
    modelOptionState({ available: false, requiresChatGptLogin: true, disabledByPolicy: false }),
    'connect',
  );
});

test('an older backend without the flag keeps the old connect behaviour', () => {
  assert.equal(modelOptionState({ available: false, requiresChatGptLogin: true }), 'connect');
});

test('other unavailable models are disabled and available ones enabled', () => {
  assert.equal(modelOptionState({ available: false, disabledByPolicy: true }), 'disabled');
  assert.equal(modelOptionState({ available: false }), 'disabled');
  assert.equal(modelOptionState({ available: true }), 'enabled');
  assert.equal(modelOptionState(undefined), 'enabled');
});
