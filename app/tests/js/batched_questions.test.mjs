// Batched ask_user_question: 1-4 questions in one card, answered in one round-trip.
//
// The properties worth pinning are the ones the AGENT depends on and that would rot
// silently: that a batch answer maps each answer back to its question, that a
// per-question "See visual options" request names WHICH question (otherwise Leo can't
// tell which one to draw) and says the other answers still stand (otherwise Leo
// re-asks everything), and that a single question still produces the exact flat
// format it always did.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeQuestions,
  buildQuestionCardHtml,
  buildSubmission,
  isAnswered,
  uiuxRequestDirective,
} from '../../frontend/chat/messages/QuestionCard.js';

const blank = () => ({ options: [], text: '', uiux: false, skipped: false });

// Stand-ins for the MessageHandler helpers the builder takes by injection.
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const parseMarkdown = (s) => `<p>${escapeHtml(s)}</p>`;

const render = (questions, extra = {}) => buildQuestionCardHtml({
  questionId: 'q-1',
  questions,
  threadId: 't-1',
  agentName: 'rails_plan_mode_agent',
  escapeHtml,
  parseMarkdown,
  ...extra,
});

// ---------------------------------------------------------------- normalize

test('reads the questions batch off the frame', () => {
  const qs = normalizeQuestions({
    questions: [
      { question: 'Which pages?', options: ['All', 'Checkout'] },
      { question: 'What style?', ui_related: true },
    ],
  });
  assert.equal(qs.length, 2);
  assert.deepEqual(qs[0].options, ['All', 'Checkout']);
  assert.equal(qs[0].ui_related, false);
  assert.equal(qs[1].ui_related, true);
  assert.deepEqual(qs[1].options, []);
});

test('falls back to the legacy single-question fields', () => {
  // An older backend (or a replayed frame) sends no `questions` array at all.
  const qs = normalizeQuestions({ question: 'Which pages?', options: ['All'], ui_related: true });
  assert.deepEqual(qs, [{ question: 'Which pages?', options: ['All'], ui_related: true }]);
});

test('drops blank questions rather than rendering an empty block', () => {
  assert.equal(normalizeQuestions({ questions: [{ question: 'Real?' }, { question: '  ' }] }).length, 1);
});

// ---------------------------------------------------------------- markup

test('renders one block per question, numbered, in a batch', () => {
  const html = render([
    { question: 'Which pages?', options: ['All'], ui_related: false },
    { question: 'What style?', options: [], ui_related: true },
    { question: 'Send email?', options: ['Yes', 'No'], ui_related: false },
  ]);
  assert.equal(html.match(/class="plan-question-item"/g).length, 3);
  assert.match(html, /data-count="3"/);
  assert.match(html, /class="plan-question-card batched"/);
  assert.match(html, /0 of 3 answered/);
});

test('the visual-options chip is per-question, not per-card', () => {
  const html = render([
    { question: 'Which pages?', options: ['All'], ui_related: false },
    { question: 'What style?', options: [], ui_related: true },
  ]);
  const chips = html.match(/plan-uiux-request-btn/g) || [];
  assert.equal(chips.length, 1, 'only the ui_related question gets the chip');
  // ...and it is wired to question index 1.
  assert.match(html, /data-qi="1" data-uiux-request="true"/);
});

test('a single question keeps the send arrow; a batch uses one Continue', () => {
  assert.match(render([{ question: 'Which pages?', options: [], ui_related: false }]), /plan-send-btn/);
  const batch = render([
    { question: 'A?', options: [], ui_related: false },
    { question: 'B?', options: [], ui_related: false },
  ]);
  assert.doesNotMatch(batch, /plan-send-btn/);
  assert.match(batch, /class="plan-continue-btn" disabled/);
  assert.match(batch, /plan-skip-all-btn/);
});

test('quotes in a question or option cannot break out of an attribute', () => {
  // _escapeHtml (textContent → innerHTML) does not escape quotes, so attribute
  // values have to be escaped here or a question like this ends the attribute early.
  const html = render([{ question: 'Use the "big" hero?', options: ['the "wide" one'], ui_related: false }]);
  assert.match(html, /data-question-text="Use the &quot;big&quot; hero\?"/);
    assert.match(html, /data-option="the &quot;wide&quot; one"/);
});

// ---------------------------------------------------------------- submission

test('a single question submits the original flat format', () => {
  // Unchanged from one-at-a-time, so existing agent behaviour is untouched.
  const qs = [{ question: 'Which pages?', options: ['All', 'Checkout'], ui_related: false }];
  const s = { ...blank(), options: ['Checkout'], text: 'and the cart' };
  assert.deepEqual(buildSubmission(qs, [s]), {
    answer: 'Checkout, and the cart',
    display: 'Checkout, and the cart',
  });
});

test('a batch pairs every answer with its question', () => {
  const qs = [
    { question: 'Which pages?', options: [], ui_related: false },
    { question: 'Send email?', options: [], ui_related: false },
  ];
  const { answer } = buildSubmission(qs, [
    { ...blank(), options: ['Checkout'] },
    { ...blank(), options: ['Yes'] },
  ]);
  assert.match(answer, /1\. Which pages\?\n→ Checkout/);
  assert.match(answer, /2\. Send email\?\n→ Yes/);
});

test('a skipped question says so instead of going out blank', () => {
  const qs = [
    { question: 'Which pages?', options: [], ui_related: false },
    { question: 'Send email?', options: [], ui_related: false },
  ];
  const { answer, display } = buildSubmission(qs, [
    { ...blank(), options: ['Checkout'] },
    { ...blank(), skipped: true },
  ]);
  assert.match(answer, /2\. Send email\?\n→ \(skipped/);
  assert.match(display, /Send email\? → \(skipped\)/);
});

test('visual options on one question names it and protects the other answers', () => {
  const qs = [
    { question: 'Which pages?', options: [], ui_related: false },
    { question: 'What should the button look like?', options: [], ui_related: true },
    { question: 'Send email?', options: [], ui_related: false },
  ];
  const { answer, display } = buildSubmission(qs, [
    { ...blank(), options: ['Checkout'] },
    { ...blank(), uiux: true },
    { ...blank(), options: ['Yes'] },
  ]);

  // Names the question, so Leo knows which one to draw previews for.
  assert.match(answer, /ask_user_uiux_question/);
  assert.match(answer, /"What should the button look like\?"/);
  // Tells Leo the other two answers stand, so it doesn't re-ask the whole batch.
  assert.match(answer, /do not re-ask those/);
  // The other answers are still in the payload.
  assert.match(answer, /→ Checkout/);
  assert.match(answer, /→ Yes/);
  // The user just sees the friendly label, not the directive.
  assert.doesNotMatch(display, /ask_user_uiux_question/);
  assert.match(display, /See visual options/);
});

test('text typed alongside a visual request rides along as preview context', () => {
  const qs = [
    { question: 'Which style?', options: [], ui_related: true },
    { question: 'Send email?', options: [], ui_related: false },
  ];
  const { answer } = buildSubmission(qs, [
    { ...blank(), uiux: true, text: 'something minimal' },
    { ...blank(), options: ['Yes'] },
  ]);
  assert.match(answer, /something minimal/);
});

test('the single-question directive still works and names the question', () => {
  const qs = [{ question: 'Which style?', options: [], ui_related: true }];
  const { answer, display } = buildSubmission(qs, [{ ...blank(), uiux: true }]);
  assert.match(answer, /ask_user_uiux_question/);
  assert.match(answer, /"Which style\?"/);
  assert.equal(display, 'See visual options');
  // No batch-only clause when there are no other answers to protect.
  assert.doesNotMatch(answer, /do not re-ask those/);
});

test('uiuxRequestDirective degrades safely with no question text', () => {
  assert.match(uiuxRequestDirective(''), /for this\./);
});

// ---------------------------------------------------------------- gating

test('Continue gating counts skips as answered', () => {
  assert.equal(isAnswered(blank()), false);
  assert.equal(isAnswered({ ...blank(), skipped: true }), true);
  assert.equal(isAnswered({ ...blank(), uiux: true }), true);
  assert.equal(isAnswered({ ...blank(), options: ['A'] }), true);
  assert.equal(isAnswered({ ...blank(), text: '  ' }), false, 'whitespace is not an answer');
  assert.equal(isAnswered({ ...blank(), text: 'x' }), true);
});
