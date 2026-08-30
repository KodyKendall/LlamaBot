// "/cookbook" turns the slash menu into a search over the published recipes at
// llamapress.ai/cookbook — the list opens on "/cookbook" and anything typed after
// it filters. Every other slash command closes the menu at the first space and owns
// the whole composer, so the cookbook path needs its own parsing; these tests pin
// that it triggers mid-sentence, that it stays open while you type the search, that
// a pick swaps ONLY the "/cookbook …" text for a short @cookbook: reference (never
// sending or executing anything on the host), and that a failed fetch doesn't wipe
// a list we already had.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const {
  SlashCommandManager,
  findCookbookTrigger,
  filterCookbookGuides,
  cookbookMention,
  cookbookJsonUrl,
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
    selectionStart: value.length,
    focus() {},
    setSelectionRange(start) { this.selectionStart = start; },
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

/** Type `value` into the composer with the caret at `caret` (default: the end). */
function setInput(manager, value, caret = null) {
  manager.messageInput.value = value;
  manager.messageInput.selectionStart = caret == null ? value.length : caret;
}

test('"/cookbook" and its search text are recognized, including with spaces', () => {
  assert.deepEqual(findCookbookTrigger('/cookbook'), { query: '', start: 0, end: 9 });
  assert.deepEqual(findCookbookTrigger('/cookbook '), { query: '', start: 0, end: 10 });
  assert.deepEqual(findCookbookTrigger('/cookbook pdf export'),
    { query: 'pdf export', start: 0, end: 20 });
  assert.equal(findCookbookTrigger('/skills'), null);
  assert.equal(findCookbookTrigger('/cookbooks'), null);
  assert.equal(findCookbookTrigger(''), null);
});

test('the trigger fires mid-sentence and reports what a pick replaces', () => {
  // The whole point: you are half way through a thought when you reach for a
  // recipe, and only the "/cookbook …" fragment may be touched.
  const value = 'add a pdf export /cookbook pdf and make it purple';
  const caret = value.indexOf(' and make');
  const trigger = findCookbookTrigger(value, caret);

  assert.deepEqual(trigger, { query: 'pdf', start: 17, end: caret });
  assert.equal(value.slice(trigger.start, trigger.end), '/cookbook pdf');
});

test('the trigger only counts at a word boundary, at or before the caret', () => {
  assert.equal(findCookbookTrigger('see docs/cookbook'), null);   // mid-word slash
  assert.equal(findCookbookTrigger('/cookbook pdf', 3), null);    // caret inside the word
  // Text typed AFTER the caret is not part of the query.
  assert.deepEqual(findCookbookTrigger('/cookbook pdf later', 13),
    { query: 'pdf', start: 0, end: 13 });
  // A newline ends the search — the recipe list shouldn't span paragraphs.
  assert.equal(findCookbookTrigger('/cookbook pdf\nsecond line'), null);
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

  setInput(manager, '/cookbook');
  manager.handleInput();
  setInput(manager, '/cookbook pdf');
  manager.handleInput();
  setInput(manager, 'halfway through a thought /cookbook pdf');
  manager.handleInput();

  assert.deepEqual(seen, ['', 'pdf', 'pdf']);
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

test('picking a recipe drops a short reference in and executes nothing', () => {
  const manager = makeManager();
  let executed = 0;
  manager.executeCommand = () => { executed += 1; };
  manager.showConfirmModal = () => { executed += 1; };

  setInput(manager, '/cookbook pdf');
  manager.handleInput();
  manager.executeSelected();

  assert.equal(executed, 0, 'a recipe must never run a host command');
  assert.equal(manager.isOpen, false);
  assert.equal(
    manager.messageInput.value,
    '@cookbook:pdf-download-export '
      + '(https://llamapress.ai/cookbook/pdf-download-export.json) ',
  );
  assert.ok(!manager.messageInput.value.startsWith('/'),
    'the slash token must be replaced, not sent to the agent');
});

test('a mid-sentence pick keeps everything already typed', () => {
  const manager = makeManager();
  const value = 'add a pdf export /cookbook pdf and make it purple';
  setInput(manager, value, value.indexOf(' and make'));

  manager.handleInput();
  manager.executeSelected();

  const mention = '@cookbook:pdf-download-export '
    + '(https://llamapress.ai/cookbook/pdf-download-export.json)';
  assert.equal(manager.messageInput.value,
    `add a pdf export ${mention} and make it purple`);
  // Caret sits right after the reference so the user can keep typing.
  assert.equal(manager.messageInput.selectionStart,
    `add a pdf export ${mention}`.length);
});

test('the reference points at the recipe JSON the agent can curl', () => {
  assert.equal(cookbookMention(GUIDES[0]),
    '@cookbook:pdf-download-export '
      + '(https://llamapress.ai/cookbook/pdf-download-export.json)');
  // Missing url (older payload) still yields a usable link from the slug.
  assert.equal(cookbookJsonUrl({ slug: 'x', title: 'X' }),
    'https://llamapress.ai/cookbook/x.json');
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

// ── Personal cookbook (0.7.5) ────────────────────────────────────────────────
// Every LlamaPress user now has a personal cookbook of recipes published from their own
// boxes. A recipe published on one Leo must be findable on their others, and be
// distinguishable from a fleet guide at a glance.

test('the owner\'s own recipes rank above fleet guides', () => {
  const guides = [
    { slug: 'fleet-toggle', title: 'Toggle', category: 'ui' },
    { slug: 'my-toggle', title: 'Toggle', category: 'ui', personal: true },
  ];

  const filtered = filterCookbookGuides(guides, 'toggle');

  assert.equal(filtered[0].slug, 'my-toggle', 'the user published this one for exactly this');
});

test('personal recipes lead the list even with no query typed', () => {
  const guides = [
    { slug: 'fleet-a', title: 'A' },
    { slug: 'mine', title: 'B', personal: true },
  ];

  assert.equal(filterCookbookGuides(guides, '')[0].slug, 'mine');
});

test('a personal recipe still loses to a better fleet match', () => {
  // Ownership breaks ties; it does not override relevance being wrong.
  const guides = [
    { slug: 'mine', title: 'Something else', category: 'pdf', personal: true },
    { slug: 'fleet-pdf', title: 'PDF export', category: 'docs' },
  ];

  assert.equal(filterCookbookGuides(guides, 'pdf')[0].slug, 'fleet-pdf');
});

test('the mention URL mechanics are unchanged for a personal recipe', () => {
  // .json and .md both exist under /cookbook/u/<handle>/<slug>, so nothing special needed.
  const guide = { slug: 'glow-toggle', title: 'Glow', personal: true,
                  url: 'https://llamapress.ai/cookbook/u/kody/glow-toggle' };

  assert.equal(cookbookJsonUrl(guide), 'https://llamapress.ai/cookbook/u/kody/glow-toggle.json');
});
