// The slash menu used to be an all-or-nothing thing: it only opened when "/" was
// the first character of an empty composer, and every pick owned the whole box.
// You reach for a command or a skill mid-thought, so the menu now opens on a "/"
// at any word boundary and every pick swaps ONLY the "/…" you typed, leaving the
// sentence around it intact. These tests pin both halves — and the guard that
// keeps a stray slash ("5 / 2") from arming a menu that the next Enter would fire.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { SlashCommandManager, findSlashTrigger } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'ui', 'SlashCommandManager.js'));

const HOST_COMMANDS = [
  { name: 'restart', description: 'Restart the app' },
  { name: 'history', description: 'Command history' },
];
const SKILLS = [
  { name: 'rails-migration', description: 'Add a column', is_skill: true, skill_slug: 'rails-migration' },
];
const GUIDES = [{
  slug: 'pdf-download-export',
  title: 'PDF Download Export',
  category: 'Reports',
  summary: 'Render any page as a downloadable PDF.',
  tags: ['pdf'],
  url: 'https://llamapress.ai/cookbook/pdf-download-export',
}];

function fakeInput(value = '') {
  return {
    value,
    selectionStart: value.length,
    focus() {},
    setSelectionRange(start) { this.selectionStart = start; },
    dispatchEvent() {},
  };
}

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

/** A manager with no DOM and no network; host commands, skills and recipes seeded. */
function makeManager() {
  const manager = new SlashCommandManager({});
  manager.messageInput = fakeInput();
  manager.dropdown = fakeDropdown();
  manager._esc = (t) => String(t == null ? '' : t);
  manager.hostCommands = HOST_COMMANDS;
  manager.skillCommands = SKILLS;
  manager.cookbookGuides = GUIDES;
  manager.cookbookFetchedAt = 1;
  manager.refreshSkills = async () => {};
  manager.rebuildCommandList();
  return manager;
}

/** Type into the composer (caret at the end unless given) and let the menu react. */
function type(manager, value, caret = null) {
  manager.messageInput.value = value;
  manager.messageInput.selectionStart = caret == null ? value.length : caret;
  manager.handleInput();
}

/** Highlight the row whose command name matches, then take it. */
function pick(manager, name) {
  const index = manager.filteredCommands.findIndex(
    c => c.name === name || c.slug === name);
  assert.ok(index >= 0, `${name} is not in the open menu`);
  manager.selectedIndex = index;
  manager.executeSelected();
}

test('a "/" at a word boundary opens the menu wherever it sits', () => {
  assert.deepEqual(findSlashTrigger('/re'), { kind: 'command', query: 're', start: 0, end: 3 });
  const typed = 'ship this then /re';
  assert.deepEqual(findSlashTrigger(typed),
    { kind: 'command', query: 're', start: 15, end: typed.length });
  // The cookbook search is its own kind — it keeps filtering past a space.
  assert.equal(findSlashTrigger('ship this /cookbook pdf').kind, 'cookbook');
});

test('a slash inside a word is a path, not a menu', () => {
  assert.equal(findSlashTrigger('look in app/frontend'), null);
  assert.equal(findSlashTrigger('read docs/cookbook'), null);
  assert.equal(findSlashTrigger('/re', 0), null, 'caret before the token');
});

test('a bare "/" mid-sentence stays quiet, but opens the full menu at the start', () => {
  // "what is 5 / 2" must not leave a menu open that the next Enter would fire.
  assert.equal(findSlashTrigger('what is 5 /'), null);
  assert.deepEqual(findSlashTrigger('/'), { kind: 'command', query: '', start: 0, end: 1 });
});

test('the menu filters on the token typed mid-sentence', () => {
  const manager = makeManager();
  type(manager, 'ok now do this /rest');

  assert.equal(manager.isOpen, true);
  assert.deepEqual(manager.filteredCommands.map(c => c.name), ['restart']);
});

test('a picked skill lands in the sentence instead of replacing it', () => {
  const manager = makeManager();
  type(manager, 'before the deploy /rails-mig');
  pick(manager, 'rails-migration');

  assert.equal(manager.messageInput.value, 'before the deploy /rails-migration ');
  assert.equal(manager.isOpen, false);

  // …and with words on the far side of the token, both sides survive.
  const other = makeManager();
  const typed = 'before the deploy /rails-mig add a column';
  type(other, typed, typed.indexOf(' add a column'));
  pick(other, 'rails-migration');
  assert.equal(other.messageInput.value, 'before the deploy /rails-migration add a column');
});

test('a picked host command runs and takes only its own token with it', () => {
  const manager = makeManager();
  const confirmed = [];
  manager.showConfirmModal = (cmd) => confirmed.push(cmd.name);

  type(manager, 'hang on, let me /restart');
  pick(manager, 'restart');

  assert.deepEqual(confirmed, ['restart'], 'the command still runs');
  assert.equal(manager.messageInput.value, 'hang on, let me ',
    'the message survives — only the /token goes');
});

test('a host command picked from an empty composer still clears it', () => {
  const manager = makeManager();
  manager.showConfirmModal = () => {};

  type(manager, '/restart');
  pick(manager, 'restart');

  assert.equal(manager.messageInput.value, '');
});

test('the /cookbook entry starts its search without eating the sentence', () => {
  const manager = makeManager();
  type(manager, 'add a pdf export /cook');
  pick(manager, 'cookbook');

  assert.equal(manager.messageInput.value, 'add a pdf export /cookbook ');
  assert.equal(manager.isOpen, true, 'the recipe list is now open');
  assert.equal(manager.filteredCommands.length, GUIDES.length);

  // And picking a recipe from there leaves the sentence in place.
  manager.selectedIndex = 0;
  manager.executeSelected();
  assert.equal(
    manager.messageInput.value,
    'add a pdf export @cookbook:pdf-download-export '
      + '(https://llamapress.ai/cookbook/pdf-download-export.json) ',
  );
});

test('the /skills entry lists skills without eating the sentence', () => {
  const manager = makeManager();
  type(manager, 'remind me how /skil');
  pick(manager, 'skills');

  assert.equal(manager.messageInput.value, 'remind me how /skills');
  assert.equal(manager.isOpen, true);
  assert.deepEqual(manager.filteredCommands.map(c => c.name), ['rails-migration']);
});

test('typing past the token closes the menu again', () => {
  const manager = makeManager();
  type(manager, 'ok now do this /rest');
  assert.equal(manager.isOpen, true);
  type(manager, 'ok now do this /rest ');
  assert.equal(manager.isOpen, false, 'a space ends a command token');
});
