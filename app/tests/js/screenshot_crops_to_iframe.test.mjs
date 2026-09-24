// Tests for cropping the screenshot to the preview iframe
// (ui/ScreenshotAnnotator.js).
//
// getDisplayMedia hands back the whole tab, so an uncropped capture also
// contains the chat panel on the left. Two ways out, and the properties that
// matter are which one runs: Region Capture (the browser crops the stream, so
// we draw the frame whole), a manual pixel crop for a plain tab capture, and
// NO crop at all when the captured surface isn't the viewport — a window or
// screen capture has browser chrome around the page, so page coordinates
// would cut out the wrong region.

import assert from 'node:assert/strict';
import test from 'node:test';

import { ScreenshotAnnotator } from '../../frontend/chat/ui/ScreenshotAnnotator.js';

const VIEWPORT = { width: 1000, height: 800 };
// The preview iframe: right-hand 60% of the viewport, below the tab strip.
const IFRAME_RECT = { left: 400, top: 40, width: 600, height: 760 };

function harness({ displaySurface = 'browser', regionCapture = false, captureTarget = 'iframe' } = {}) {
  const calls = { cropTo: [], drawImage: [], stopped: 0 };

  const track = {
    getSettings: () => ({ displaySurface }),
    stop() { calls.stopped += 1; },
  };
  if (regionCapture) {
    track.cropTo = async (cropTarget) => { calls.cropTo.push(cropTarget); };
  }

  const stream = { getVideoTracks: () => [track], getTracks: () => [track] };

  const target = {
    getBoundingClientRect: () => ({ ...IFRAME_RECT }),
  };

  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({
      drawImage: (...args) => calls.drawImage.push(args),
    }),
    toDataURL: () => 'data:image/png;base64,FAKE',
  };

  globalThis.window = { innerWidth: VIEWPORT.width, innerHeight: VIEWPORT.height };
  globalThis.navigator = { mediaDevices: { getDisplayMedia: async () => stream } };
  globalThis.CropTarget = regionCapture
    ? { fromElement: async (el) => ({ element: el }) }
    : undefined;

  globalThis.document = {
    createElement(tag) {
      if (tag === 'canvas') return canvas;
      // Fake <video>: the real one fires onloadedmetadata once the stream is
      // attached, so assigning the handler is what resolves the capture.
      const video = {
        videoWidth: VIEWPORT.width * 2,   // 2x device pixel ratio
        videoHeight: VIEWPORT.height * 2,
        muted: false,
        srcObject: null,
        play() {},
      };
      Object.defineProperty(video, 'onloadedmetadata', {
        set(fn) { setTimeout(fn, 0); },
      });
      return video;
    },
  };

  const annotator = new ScreenshotAnnotator({
    getCaptureTarget: () => (captureTarget === 'iframe' ? target : null),
  });

  return { annotator, calls, canvas, target };
}

test('Region Capture crops the stream, so the frame is drawn whole', async () => {
  const { annotator, calls, canvas, target } = harness({ regionCapture: true });

  const dataUrl = await annotator.captureScreen();

  assert.equal(dataUrl, 'data:image/png;base64,FAKE');
  assert.equal(calls.cropTo.length, 1, 'the track should be cropped to the iframe');
  assert.equal(calls.cropTo[0].element, target);
  // Already cropped upstream: no source rectangle, and the canvas is the
  // size of the (cropped) video.
  assert.equal(calls.drawImage[0].length, 3);
  assert.equal(canvas.width, VIEWPORT.width * 2);
});

test('without Region Capture, a tab capture is cropped to the iframe rect', async () => {
  const { annotator, calls, canvas } = harness({ regionCapture: false });

  await annotator.captureScreen();

  const [, sx, sy, sw, sh] = calls.drawImage[0];
  assert.equal(calls.drawImage[0].length, 9, 'should draw a sub-rectangle of the frame');
  assert.deepEqual([sx, sy, sw, sh], [
    IFRAME_RECT.left * 2,
    IFRAME_RECT.top * 2,
    IFRAME_RECT.width * 2,
    IFRAME_RECT.height * 2,
  ]);
  // The chat panel sits left of the iframe and must not be in the output.
  assert.ok(sx > 0, 'crop should start right of the chat panel');
  assert.equal(canvas.width, IFRAME_RECT.width * 2);
  assert.equal(canvas.height, IFRAME_RECT.height * 2);
});

test('a window/screen capture is left uncropped (page coordinates would miss)', async () => {
  const { annotator, calls, canvas } = harness({ displaySurface: 'monitor' });

  await annotator.captureScreen();

  assert.equal(calls.drawImage[0].length, 3, 'no pixel crop off a non-tab capture');
  assert.equal(canvas.width, VIEWPORT.width * 2);
});

test('no capture target means no crop, and the stream is always stopped', async () => {
  const { annotator, calls } = harness({ captureTarget: null });

  await annotator.captureScreen();

  assert.equal(calls.drawImage[0].length, 3);
  assert.equal(calls.stopped, 1);
});
