/**
 * Question card markup + answer formatting for `ask_user_question`.
 *
 * Leo can ask 1-4 questions in a single interrupt. The card renders one block per
 * question; the user answers them all and submits once, which is a whole round-trip
 * saved per extra question.
 *
 * The pieces that matter live here (pure, no DOM) rather than in MessageHandler so the
 * answer format — the actual contract with the agent — can be tested without a browser.
 * MessageHandler keeps the event wiring.
 */

/**
 * Submitted for a question when the user picks "See visual options" instead of
 * answering. It is a directive, not an answer: it asks Leo to re-ask THIS question
 * visually via ask_user_uiux_question. In a batch it must name the question, or Leo
 * can't tell which one to draw — and must say the other answers still stand, or Leo
 * re-asks everything.
 */
export function uiuxRequestDirective(questionText, { batched = false } = {}) {
  const target = questionText ? ` for this question ("${questionText}")` : ' for this';
  return (
    `The user would like to see visual UI/UX options${target}. Please call the ` +
    'ask_user_uiux_question tool with 2-4 concrete example designs (live HTML previews) ' +
    'for it instead of answering in text.' +
    (batched ? ' Keep the answers given to the other questions — do not re-ask those.' : '')
  );
}

/**
 * Normalize a `question_request` frame into a list of questions.
 *
 * The backend sends `questions` (the batch) and also mirrors the first one into the
 * legacy top-level `question`/`options`/`ui_related` fields, so an older cached
 * frontend still renders something. Read the batch when it's there, fall back otherwise.
 */
export function normalizeQuestions(data) {
  const batch = Array.isArray(data?.questions) ? data.questions : [];
  const source = batch.length
    ? batch
    : [{ question: data?.question, options: data?.options, ui_related: data?.ui_related }];

  return source
    .map(q => ({
      question: String(q?.question ?? '').trim(),
      options: Array.isArray(q?.options) ? q.options.map(String) : [],
      ui_related: !!q?.ui_related,
    }))
    .filter(q => q.question.length > 0);
}

/** Escape for use inside a double-quoted HTML attribute. */
function escapeAttr(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const EYE_ICON =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" aria-hidden="true">' +
  '<path d="M12 2C6.49 2 2 6.49 2 12s4.49 10 10 10c1.38 0 2.5-1.12 2.5-2.5 0-.61-.23-1.2-.64-1.67-.08-.1-.13-.21-.13-.33 0-.28.22-.5.5-.5H16c3.31 0 6-2.69 6-6 0-4.96-4.49-9-10-9zm-5.5 9c-.83 0-1.5-.67-1.5-1.5S5.67 8 6.5 8 8 8.67 8 9.5 7.33 11 6.5 11zm3-4C8.67 7 8 6.33 8 5.5S8.67 4 9.5 4s1.5.67 1.5 1.5S10.33 7 9.5 7zm5 0c-.83 0-1.5-.67-1.5-1.5S13.67 4 14.5 4s1.5.67 1.5 1.5S15.33 7 14.5 7zm3 4c-.83 0-1.5-.67-1.5-1.5S16.67 8 17.5 8s1.5.67 1.5 1.5-.67 1.5-1.5 1.5z"/></svg>';

/**
 * Build the card markup.
 *
 * A single question renders exactly as it always has (options row with Skip, hidden
 * Continue, free-text row with a send arrow). Two or more switch to the batched
 * layout: one block per question, a per-question Skip, and one footer Continue that
 * stays disabled until every question is answered or skipped.
 *
 * `escapeHtml` and `parseMarkdown` are injected so this module stays DOM-free.
 */
export function buildQuestionCardHtml({
  questionId,
  questions,
  context = '',
  threadId,
  agentName,
  escapeHtml,
  parseMarkdown,
}) {
  const batched = questions.length > 1;

  const blocks = questions.map((q, i) => {
    const optionButtons = q.options.map(opt =>
      `<button class="plan-option-btn" data-qi="${i}" data-option="${escapeAttr(opt)}">${escapeHtml(opt)}</button>`
    ).join('');

    // Only on questions the agent flagged as visual. Kept per-question in a batch:
    // the user can ask for previews on question 2 while answering 1 and 3 in text.
    const uiuxBtn = q.ui_related ? `
        <button class="plan-option-btn plan-uiux-request-btn" data-qi="${i}" data-uiux-request="true"
                title="Have Leo show you visual UI/UX options to pick from">
          ${EYE_ICON}See visual options
        </button>` : '';

    // Batched: Skip marks just this question and waits for Continue.
    // Single: Skip submits the whole card immediately (unchanged behaviour).
    const skipBtn = `<button class="plan-skip-btn" data-qi="${i}">Skip</button>`;
    const hasOptionRow = q.options.length > 0 || q.ui_related || batched;

    const sendBtn = batched ? '' : `<button class="plan-send-btn"><i class="fa-solid fa-arrow-up"></i></button>`;

    return `
      <div class="plan-question-item" data-qi="${i}" data-question-text="${escapeAttr(q.question)}">
        ${batched ? `<span class="plan-question-num">${i + 1}</span>` : ''}
        <div class="plan-question-text">${parseMarkdown(q.question)}</div>
        ${hasOptionRow ? `<div class="plan-question-options">${optionButtons}${uiuxBtn}${skipBtn}</div>` : ''}
        <div class="plan-question-input-row">
          <textarea class="plan-question-input" data-qi="${i}" rows="${batched ? 1 : 2}" placeholder="${batched ? 'Or type your own…' : 'Add to your answer...'}"></textarea>
          ${sendBtn}
        </div>
      </div>`;
  }).join('');

  const foot = batched ? `
      <div class="plan-question-foot">
        <span class="plan-question-progress">0 of ${questions.length} answered</span>
        <div class="plan-question-foot-actions">
          <button class="plan-skip-all-btn">Skip all</button>
          <button class="plan-continue-btn" disabled>Continue</button>
        </div>
      </div>`
    : `<button class="plan-continue-btn" style="display: none;">Continue</button>`;

  return `
      <div class="plan-question-card${batched ? ' batched' : ''}" data-question-id="${questionId}"
           data-thread-id="${escapeAttr(threadId)}" data-agent-name="${escapeAttr(agentName)}"
           data-count="${questions.length}">
        ${context ? `<div class="plan-question-context">${escapeHtml(context)}</div>` : ''}
        ${blocks}
        ${foot}
      </div>
    `;
}

/**
 * True once this question has something to send.
 * A blank question is what keeps Continue disabled in a batch.
 */
export function isAnswered(state) {
  return !!(state && (state.skipped || state.uiux || state.options.length > 0 || (state.text || '').trim()));
}

/**
 * Turn the per-question UI state into what resumes the agent (`answer`) and what the
 * user sees in their own chat bubble (`display`).
 *
 * A single question keeps the original flat format so nothing about existing
 * one-at-a-time behaviour changes. A batch is numbered and pairs each question with
 * its answer, because the agent has no other way to map answers back to questions.
 *
 * `states[i]` is `{ options: string[], text: string, uiux: bool, skipped: bool }`.
 */
export function buildSubmission(questions, states) {
  const batched = questions.length > 1;

  if (!batched) {
    const s = states[0] || { options: [], text: '', uiux: false, skipped: false };
    const freeText = (s.text || '').trim();
    const parts = [...s.options];
    const displayParts = [...s.options];
    if (freeText) { parts.push(freeText); displayParts.push(freeText); }
    if (s.uiux) {
      parts.push(uiuxRequestDirective(questions[0]?.question));
      displayParts.push('See visual options');
    }
    return { answer: parts.join(', '), display: displayParts.join(', ') };
  }

  const answerBlocks = [];
  const displayLines = [];

  questions.forEach((q, i) => {
    const s = states[i] || { options: [], text: '', uiux: false, skipped: false };
    const freeText = (s.text || '').trim();

    let answer;
    let shown;
    if (s.uiux) {
      answer = uiuxRequestDirective(q.question, { batched: true });
      shown = 'See visual options';
      // Anything typed alongside the request is still useful context for the previews.
      if (freeText) { answer += ` The user added: "${freeText}"`; shown += `, ${freeText}`; }
    } else if (s.skipped || (!s.options.length && !freeText)) {
      answer = '(skipped — no preference)';
      shown = '(skipped)';
    } else {
      const parts = [...s.options];
      if (freeText) parts.push(freeText);
      answer = parts.join(', ');
      shown = answer;
    }

    answerBlocks.push(`${i + 1}. ${q.question}\n→ ${answer}`);
    displayLines.push(`${q.question} → ${shown}`);
  });

  return {
    answer: `The user answered all ${questions.length} questions:\n\n${answerBlocks.join('\n\n')}`,
    display: displayLines.join('\n'),
  };
}
