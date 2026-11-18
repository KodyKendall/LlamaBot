/**
 * Saved whole-app versions (LP-47) — the words, the shapes and the requests.
 *
 * No DOM here, so it can be tested in Node. VersionsSection.js draws it.
 * The versions live on LlamaPress; the box proxies /api/versions to it and
 * passes the mothership's own messages through verbatim.
 */

export const SAVING_LABEL = 'Saving';
export const SAVED_LABEL = 'Saved';
export const FAILED_LABEL = "Couldn't save";
export const RESTORE_MINUTES = 2;
// The box restarts during a restore; give it this long before saying it's slow.
export const RESTORE_TIMEOUT_MS = 10 * 60 * 1000;

const STATUS_LABELS = { saving: SAVING_LABEL, saved: SAVED_LABEL, failed: FAILED_LABEL };

export function statusLabel(status) {
  return STATUS_LABELS[status] || SAVING_LABEL;
}

export function formatSize(bytes) {
  if (typeof bytes !== 'number' || !isFinite(bytes) || bytes <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const shown = value >= 10 || unit === 0 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${shown} ${units[unit]}`;
}

export function formatDate(iso, locale = undefined) {
  if (!iso) return '';
  const date = new Date(iso);
  if (isNaN(date.getTime())) return '';
  return date.toLocaleString(locale, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  });
}

/**
 * What the Versions section shows for a GET /api/versions body.
 * hidden: the mothership turned the feature off and had nothing to say.
 */
export function describeVersions(body, locale = undefined) {
  const data = body && typeof body === 'object' ? body : {};
  const enabled = data.enabled !== false;
  const items = (Array.isArray(data.versions) ? data.versions : []).map((v) => ({
    id: v.id,
    name: v.name || 'Untitled version',
    date: formatDate(v.created_at, locale),
    size: formatSize(v.size_bytes),
    status: v.status || 'saving',
    statusLabel: statusLabel(v.status),
    safety: v.kind === 'safety',
    ready: v.status === 'saved',
  }));
  return {
    hidden: !enabled && !data.message,
    enabled,
    message: data.message || '',
    upgradeUrl: data.upgrade_url || '',
    canSave: enabled,
    canRestore: enabled && data.can_restore !== false,
    canLaunch: enabled && data.can_launch !== false,
    items,
    empty: enabled && items.length === 0,
    restoring: data.restore?.status === 'restoring',
    restoringVersionId: data.restore?.version_id || null,
    anySaving: items.some((item) => item.status === 'saving'),
  };
}

/** A failed request: the server's own words, and its upgrade link if it sent one. */
export function errorView(body) {
  const data = body && typeof body === 'object' ? body : {};
  return {
    message: data.error || data.message || "That didn't work. Please try again.",
    upgradeUrl: data.upgrade_url || '',
  };
}

export function restoreConfirmText(version) {
  const when = version.date || 'when you saved it';
  return `Your app will go back to ${when} ("${version.name}") — code, database and uploaded files. `
    + `Your current app is saved first. Takes about ${RESTORE_MINUTES} minutes.`;
}

export function launchConfirmText(version) {
  return `A second Leo will start with a copy of "${version.name}" — code, database and uploaded files. `
    + 'Your current app stays exactly as it is. The new one counts toward your plan.';
}

export function restoringText(name) {
  return `Restoring your app to "${name}"… This takes about ${RESTORE_MINUTES} minutes. `
    + 'This page reconnects by itself when your app is back.';
}

export const RESTORE_SLOW_TEXT = 'This is taking longer than usual. Refresh the page in a minute.';

export function launchedText(instance) {
  const name = instance?.name ? ` (${instance.name})` : '';
  return `Your new Leo${name} is starting. It will appear on your dashboard in a few minutes.`;
}

/** Typed confirmation for a restore: the version's name, ignoring case and spaces at the ends. */
export function confirmMatches(typed, name) {
  return String(typed || '').trim().toLowerCase() === String(name || '').trim().toLowerCase()
    && String(name || '').trim() !== '';
}

/**
 * Has the box come back from a restore? The mothership's word wins; if it
 * doesn't say, wait until we've seen the box go down and come back.
 */
export function restoreFinished(poll) {
  if (!poll.ok) return false;
  const status = poll.body?.restore?.status;
  if (status) return status !== 'restoring';
  return poll.sawOutage === true;
}

/** The requests, over any fetch. Every one goes to the box's own /api/versions proxy. */
export function makeVersionsApi(fetchFn = (...args) => fetch(...args)) {
  const call = async (method, path = '', body = undefined) => {
    const init = { method, credentials: 'same-origin', cache: 'no-store', headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await fetchFn(`/api/versions${path}`, init);
    let data = {};
    try {
      data = await response.json();
    } catch (e) {
      data = {};
    }
    return { ok: response.ok, status: response.status, body: data };
  };
  const id = (versionId) => `/${encodeURIComponent(versionId)}`;
  return {
    list: () => call('GET'),
    save: (name) => call('POST', '', { name }),
    remove: (versionId) => call('DELETE', id(versionId)),
    restore: (versionId, confirm) => call('POST', `${id(versionId)}/restore`, { confirm }),
    launch: (versionId) => call('POST', `${id(versionId)}/launch`, {}),
  };
}
