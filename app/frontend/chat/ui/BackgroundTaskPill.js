/**
 * "Research running · 1:40" / "2 coding tasks running · 1:15" — the status pill
 * for work Leo runs in the background.
 *
 * delegate_research and delegate_task no longer hold the turn open: the turn ends, the user can
 * keep chatting, and the results arrive later as a new turn. The pill is what
 * tells the user something is still going. The backend sends
 * `background_task` frames (status `running` / `finished`) for every thread, so
 * tasks are tracked per thread and the pill shows the ones for the chat on
 * screen. It stays up across turns and clears when the results land.
 */

export function formatElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// Plural for several tasks of one kind: "2 coding tasks running".
const PLURALS = { coding: 'coding tasks', research: 'research tasks' };

/**
 * Pill text for a thread's running tasks (startedAt is epoch seconds). Parallel
 * tasks share one pill with a count; the clock is the oldest task's.
 */
export function pillText(tasks, nowMs) {
  if (!tasks.length) return '';
  const oldest = Math.min(...tasks.map((t) => t.startedAt * 1000));
  let label = tasks[0].label;
  if (tasks.length > 1) {
    const kinds = new Set(tasks.map((t) => t.kind));
    const noun = kinds.size === 1 ? PLURALS[tasks[0].kind] : null;
    label = `${tasks.length} ${noun || 'background tasks'}`;
  }
  return `${label} running · ${formatElapsed(nowMs - oldest)}`;
}

export class BackgroundTaskPill {
  constructor({
    element,
    getThreadId,
    now = () => Date.now(),
    setInterval: startTimer = (fn, ms) => setInterval(fn, ms),
    clearInterval: stopTimer = (id) => clearInterval(id),
    onChange = () => {},
  }) {
    this.element = element;
    this.getThreadId = getThreadId;
    this.now = now;
    this.startTimer = startTimer;
    this.stopTimer = stopTimer;
    this.tasksByThread = new Map(); // threadId -> Map(taskId -> {label, startedAt})
    this.timer = null;
    this.onChange = onChange;
    this.lastHasTasks = false;
  }

  /** Whether the chat on screen has background work running (the Stop button needs this). */
  hasTasks() {
    return this.currentTasks().length > 0;
  }

  currentTasks() {
    return [...(this.tasksByThread.get(String(this.getThreadId?.() ?? '_'))?.values() || [])];
  }

  handleFrame(data) {
    const threadId = String(data.thread_id ?? this.getThreadId?.() ?? '_');
    if (!this.tasksByThread.has(threadId)) this.tasksByThread.set(threadId, new Map());
    const tasks = this.tasksByThread.get(threadId);
    if (data.status === 'running') {
      tasks.set(data.task_id, {
        label: data.label || 'Research',
        kind: data.kind || 'research',
        startedAt: Number(data.started_at) || this.now() / 1000,
      });
    } else {
      tasks.delete(data.task_id);
    }
    this.render();
  }

  render() {
    const tasks = this.currentTasks();
    if (tasks.length > 0 !== this.lastHasTasks) {
      this.lastHasTasks = tasks.length > 0;
      this.onChange();
    }
    if (!this.element) return;
    if (!tasks.length) {
      this.element.classList.add('hidden');
      this.element.textContent = '';
      if (this.timer !== null) {
        this.stopTimer(this.timer);
        this.timer = null;
      }
      return;
    }
    this.element.textContent = pillText(tasks, this.now());
    this.element.classList.remove('hidden');
    if (this.timer === null) {
      this.timer = this.startTimer(() => this.render(), 1000);
    }
  }
}
