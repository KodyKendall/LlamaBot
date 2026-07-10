import assert from 'node:assert/strict';
import test from 'node:test';

import { StallMonitor } from '../../frontend/chat/ui/StallMonitor.js';
import { MessageHandler } from '../../frontend/chat/websocket/MessageHandler.js';


test('stall monitor warns at 90 seconds and resets on inbound activity', () => {
  let now = 1_000;
  const monitor = new StallMonitor({ thresholdMs: 90_000, now: () => now });

  monitor.start();
  now += 89_999;
  assert.equal(monitor.shouldWarn(true), false);

  now += 1;
  assert.equal(monitor.shouldWarn(true), true);

  monitor.markActivity();
  assert.equal(monitor.shouldWarn(true), false);
  now += 90_000;
  assert.equal(monitor.shouldWarn(true), true);

  monitor.stop();
  assert.equal(monitor.shouldWarn(true), false);
});


test('only the visible thread resets activity and receives progress', () => {
  const dispatched = [];
  const progress = [];
  globalThis.CustomEvent = class CustomEvent {
    constructor(type, options = {}) {
      this.type = type;
      this.detail = options.detail;
    }
  };
  globalThis.window = {
    dispatchEvent: (event) => dispatched.push(event),
    chatApp: { handleDelegationProgress: (data) => progress.push(data) },
  };

  const handler = new MessageHandler(
    { getThreadId: () => 'thread-current' },
    {},
    null,
    null,
    null,
    null,
    {}
  );

  handler.handleMessage({
    type: 'delegation_progress',
    thread_id: 'thread-other',
    seq: 1,
    message: 'wrong thread',
  });
  assert.equal(dispatched.length, 0);
  assert.equal(progress.length, 0);

  const currentFrame = {
    type: 'delegation_progress',
    thread_id: 'thread-current',
    seq: 1,
    message: 'Research sub-agent is still working…',
  };
  handler.handleMessage(currentFrame);
  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].type, 'websocketActivity');
  assert.deepEqual(dispatched[0].detail, currentFrame);
  assert.deepEqual(progress, [currentFrame]);
});
