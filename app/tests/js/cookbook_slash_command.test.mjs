// "/cookbook" turns the slash menu into a search over the published recipes at
// llamapress.ai/cookbook — the list opens on "/cookbook" and anything typed after
// it filters. Every other slash command closes the menu at the first space, so the
// cookbook path needs its own parsing; these tests pin that it stays open, that a
// pick fills the composer (and never sends or executes anything on the host), and
// that a failed fetch doesn't wipe a list we already had.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const {
  SlashCommandManager,
  parseCookbookInput,
  filterCookbookGuides,
  cookbookDirective,
} = await import(resolve(APP_ROOT, 'frontend', 'chat', 'ui', 'SlashCommandManager.js'));

const GUIDES = [
  {
    slug: 'pdf-download-export',
    title: 'PDF Download Export',
    category: 'Reports',
    summary: 'Render any page as a downloadable PDF.',
    tags: ['pdf', 'export'],
    url: 'https://llamapress.ai/cookbook/pdf-download-export',
  },
  {
    slug: 'rate-limiting-and-ip-controls',
    title: 'Rate Limiting & IP Allowlists',
    category: 'Auth',
    summary: 'Rack::Attack throttles, a trusted-IP safelist and an audit trail.',
    tags: ['security', 'throttle'],
    url: 'https://llamapress.ai/cookbook/rate-limiting-and-ip-controls',
  },
  {
    slug: 'inline-data-tables',
    title: 'Inline Data Tables',
    category: 'UI',
    summary: 'Sortable, filterable tables — includes a PDF export column.',
    tags: ['tables'],
    url: 'https://llamapress.ai/cookbook/inline-data-tables',
  },
];

/** Text input stand-in: only value/focus/selection/dispatch are touched. */
function fakeInput(value = '') {
  return {
    value,
    focus() {},
    setSelectionRange() {},
    dispatchEvent() {},
  };
}

/** Dropdown stand-in — the manager only sets innerHTML and toggles classes. */
function fakeDropdown() {
  const classes = new Set(['hidden']);
  return {
    innerHTML: '',
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
    },
    querySelectorAll() { return []; },
  };
}

/** A manager with no DOM and no network: guides are pre-seeded. */
function makeManager(guides = GUIDES) {
  const manager = new SlashCommandManager({});   // container passed → `document` never read
  manager.messageInput = fakeInput();
  manager.dropdown = fakeDropdown();
  manager._esc = (t) => String(t == null ? '' : t);  // no DOM for escaping in node
  if (guides) {
    manager.cookbookGuides = guides;
    manager.cookbookFetchedAt = 1;
  }
  return manager;
}

test('"/cookbook" and its search text are recognized, including with spaces', () => {
  assert.deepEqual(parseCookbookInput('/cookbook'), { query: '' });
  assert.deepEqual(parseCookbookInput('/cookbook '), { query: '' });
  assert.deepEqual(parseCookbookInput('/cookbook pdf export'), { query: 'pdf export' });
  assert.equal(parseCookbookInput('/skills'), null);
  assert.equal(parseCookbookInput('/cookbooks'), null);
  assert.equal(parseCookbookInput('tell me about /cookbook'), null);
});

test('no query lists every recipe', () => {
  assert.equal(filterCookbookGuides(GUIDES, '').length, 3);
});

test('the query filters across title, summary, category and tags', () => {
  assert.deepEqual(
    filterCookbookGuides(GUIDES, 'pdf').map(g => g.slug),
    ['pdf-download-export', 'inline-data-tables'],   // title match ranks above summary match
  );
  assert.deepEqual(filterCookbookGuides(GUIDES, 'auth').map(g => g.slug),
    ['rate-limiting-and-ip-controls']);              // category
  assert.deepEqual(filterCookbookGuides(GUIDES, 'throttle').map(g => g.slug),
    ['rate-limiting-and-ip-controls']);              // tag
});

test('every word must match — a second word narrows the list', () => {
  assert.deepEqual(filterCookbookGuides(GUIDES, 'pdf tables').map(g => g.slug),
    ['inline-data-tables']);
  assert.deepEqual(filterCookbookGuides(GUIDES, 'PDF DOWNLOAD').map(g => g.slug),
    ['pdf-download-export']);                        // case-insensitive
  assert.deepEqual(filterCookbookGuides(GUIDES, 'nonexistent'), []);
});

test('typing after /cookbook keeps the menu open and filtering', () => {
  const manager = makeManager();
  const seen = [];
  manager.showCookbookDropdown = (q) => seen.push(q);

  manager.messageInput.value = '/cookbook';
  manager.handleInput();
  manager.messageInput.value = '/cookbook pdf';
  manager.handleInput();

  assert.deepEqual(seen, ['', 'pdf']);
});

test('the recipe list renders and is selectable', () => {
  const manager = makeManager();
  manager.showCookbookDropdown('pdf');

  assert.equal(manager.isOpen, true);
  assert.equal(manager.dropdown.classList.contains('hidden'), false);
  assert.equal(manager.filteredCommands.length, 2);
  assert.equal(manager.selectedIndex, 0);
  assert.ok(manager.dropdown.innerHTML.includes('PDF Download Export'));
  assert.ok(manager.dropdown.innerHTML.includes('2 matches'));
});

test('the cookbook list gets its own taller panel, and gives it back', () => {
  // ~30 two-line recipes don't fit the command-sized panel; the taller height is
  // keyed off this class, so it must go on for the cookbook and off for everything
  // else — a stuck class leaves a half-empty giant menu behind for "/restart".
  const manager = makeManager();
  manager.showCookbookDropdown('');
  assert.equal(manager.dropdown.classList.contains('cookbook-mode'), true);

  manager.hideDropdown();
  assert.equal(manager.dropdown.classList.contains('cookbook-mode'), false);

  manager.showCookbookDropdown('');
  manager.rebuildCommandList();
  manager.showDropdown('');           // back to the normal command menu
  assert.equal(manager.dropdown.classList.contains('cookbook-mode'), false);
});

test('a recipe row stacks its title over a summary that can be clamped', () => {
  const manager = makeManager();
  manager.showCookbookDropdown('rate');

  const html = manager.dropdown.innerHTML;
  assert.ok(html.includes('cookbook-body'), 'title + summary share one column');
  assert.ok(html.includes('cookbook-summary'), 'the summary is clampable on its own');
  // The full summary stays reachable on hover even when clamped to two lines.
  assert.ok(html.includes('title="Rack::Attack throttles, a trusted-IP safelist and an audit trail."'));
});

test('a query matching nothing says so instead of closing the menu', () => {
  const manager = makeManager();
  manager.showCookbookDropdown('zzzz');

  assert.equal(manager.isOpen, true);
  assert.deepEqual(manager.filteredCommands, []);
  assert.ok(manager.dropdown.innerHTML.includes('No recipes match'));
});

test('picking a recipe fills the composer and executes nothing', () => {
  const manager = makeManager();
  let executed = 0;
  manager.executeCommand = () => { executed += 1; };
  manager.showConfirmModal = () => { executed += 1; };

  manager.showCookbookDropdown('pdf');
  manager.executeSelected();

  assert.equal(executed, 0, 'a recipe must never run a host command');
  assert.equal(manager.isOpen, false);
  assert.ok(manager.messageInput.value.includes('PDF Download Export'));
  assert.ok(manager.messageInput.value.includes(
    'https://llamapress.ai/cookbook/pdf-download-export.json'));
  assert.ok(!manager.messageInput.value.startsWith('/'),
    'the slash token must be replaced, not sent to the agent');
});

test('the directive points at the recipe JSON the agent can curl', () => {
  assert.ok(cookbookDirective(GUIDES[0]).includes(
    'curl https://llamapress.ai/cookbook/pdf-download-export.json'));
  // Missing url (older payload) still yields a usable link from the slug.
  assert.ok(cookbookDirective({ slug: 'x', title: 'X' }).includes(
    'https://llamapress.ai/cookbook/x.json'));
});

test('/cookbook appears in the slash menu as its own entry', () => {
  const manager = makeManager();
  manager.hostCommands = [{ name: 'restart', description: 'Restart' }];
  manager.skillCommands = [];
  manager.rebuildCommandList();

  const entry = manager.commands.find(c => c.name === 'cookbook');
  assert.ok(entry, '/cookbook must be listed');
  assert.equal(entry.is_cookbook_meta, true);
});

test('a failed refresh keeps the recipes we already have', async () => {
  const manager = makeManager();
  globalThis.fetch = async () => { throw new Error('offline'); };
  try {
    await manager.fetchCookbook(true);
  } finally {
    delete globalThis.fetch;
  }
  assert.equal(manager.cookbookGuides.length, 3);
});

test('recipes are fetched from the backend proxy, not llamapress.ai directly', async () => {
  const manager = makeManager(null);
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(url);
    return { ok: true, json: async () => ({ guides: GUIDES }) };
  };
  try {
    await manager.fetchCookbook();
  } finally {
    delete globalThis.fetch;
  }
  assert.deepEqual(urls, ['/api/cookbook']);
  assert.equal(manager.cookbookGuides.length, 3);
});
