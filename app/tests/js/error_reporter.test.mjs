// Guardrails for the frontend error reporter. It runs inside a page that is
// already misbehaving, so the important properties are: it dedupes, it caps, and
// it never throws or cascades.

import assert from 'node:assert/strict';
import test from 'node:test';

import { ErrorReporter } from '../../frontend/chat/utils/ErrorReporter.js';

function harness({ fetchImpl } = {}) {
  const posts = [];
  globalThis.window = { addEventListener() {}, chatApp: {} };
  globalThis.document = { querySelector: () => null };
  globalThis.fetch = fetchImpl || ((url, opts) => {
    posts.push({ url, body: JSON.parse(opts.body) });
    return Promise.resolve({ ok: true });
  });
  const reporter = new ErrorReporter({ getThreadId: () => 'thread-7', getAgentConfig: () => ({ name: 'rails_agent' }) });
  return { reporter, posts };
}

test('posts to the same-origin endpoint with the triage fields', () => {
  const { reporter, posts } = harness();

  reporter.report('FrontendConnectionLost', 'Lost connection mid-run', 'stack here');

  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/frontend-error');
  assert.equal(posts[0].body.error_class, 'FrontendConnectionLost');
  assert.equal(posts[0].body.error_message, 'Lost connection mid-run');
  assert.equal(posts[0].body.thread_id, 'thread-7');
  assert.equal(posts[0].body.agent_mode, 'rails_agent');
  assert.ok(posts[0].body.fingerprint, 'a fingerprint should always be sent');
});

test('dedupes identical errors within a page load', () => {
  const { reporter, posts } = harness();

  for (let i = 0; i < 5; i++) {
    reporter.report('FrontendError', 'the same thing keeps happening');
  }

  assert.equal(posts.length, 1, 'a repeating error must not hammer the box');
});

test('distinct errors still get through', () => {
  const { reporter, posts } = harness();

  reporter.report('FrontendError', 'first problem');
  reporter.report('FrontendError', 'second problem');
  reporter.report('FrontendUnhandledRejection', 'third problem');

  assert.equal(posts.length, 3);
});

test('caps total reports per page load', () => {
  const { reporter, posts } = harness();

  for (let i = 0; i < 100; i++) {
    reporter.report('FrontendError', `unique failure ${i}`);
  }

  assert.equal(posts.length, 20, 'a runaway page must not fire unbounded reports');
});

test('truncates oversized messages and stacks', () => {
  const { reporter, posts } = harness();

  reporter.report('FrontendError', 'm'.repeat(9000), 's'.repeat(20000));

  assert.equal(posts[0].body.error_message.length, 2000);
  assert.equal(posts[0].body.stack.length, 5000);
});

test('a failing fetch never throws or rejects', () => {
  const { reporter } = harness({ fetchImpl: () => Promise.reject(new Error('network down')) });

  assert.doesNotThrow(() => reporter.report('FrontendError', 'something'));
});

test('a throwing fetch never escapes the reporter', () => {
  const { reporter } = harness({ fetchImpl: () => { throw new Error('fetch exploded'); } });

  assert.doesNotThrow(() => reporter.report('FrontendError', 'something'));
});

test('a broken appState does not break reporting', () => {
  const { posts } = harness();
  const reporter = new ErrorReporter({
    getThreadId() { throw new Error('state is gone'); },
    getAgentConfig() { throw new Error('state is gone'); },
  });

  assert.doesNotThrow(() => reporter.report('FrontendError', 'still reportable'));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].body.thread_id, null);
});
