const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '../app/static/app.js'), 'utf8');

function element() {
  const listeners = new Map();
  const attributes = new Map();
  return {
    dataset: {},
    disabled: false,
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(callback);
    },
    dispatch(name, values = {}) {
      const event = {
        ...values,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
      };
      for (const callback of listeners.get(name) || []) callback(event);
      return event;
    },
    setAttribute(name, value) { attributes.set(name, value); },
    getAttribute(name) { return attributes.get(name) ?? null; },
    removeAttribute(name) { attributes.delete(name); },
  };
}

// Execute the production script with isolated browser primitives and no network.
function page({ confirm = true, storageFails = false } = {}) {
  const form = element();
  const button = element();
  const themeButton = element();
  const browser = element();
  browser.matchMedia = () => ({ matches: false });
  browser.scrollX = 0;
  browser.scrollY = 0;
  const classes = new Set();
  const root = {
    dataset: {},
    classList: { add(name) { classes.add(name); } },
  };
  const timers = [];
  const intervals = [];
  let reloads = 0;
  let submissions = 0;
  let confirmations = 0;
  browser.confirm = () => { confirmations++; return confirm; };
  const storage = {
    getItem() { if (storageFails) throw new Error('Storage denied'); return null; },
    setItem() { if (storageFails) throw new Error('Storage denied'); },
  };
  function select(selector) {
    return selector.split(',').flatMap(part => {
      const name = part.trim();
      if (name === '.theme-toggle') return [themeButton];
      if (name === '[data-auto-refresh]') return [{ dataset: { autoRefresh: '30' } }];
      if (name === 'form') return [form];
      if (name === 'button[aria-busy]') return button.getAttribute('aria-busy') ? [button] : [];
      const field = /^form\[data-(editing|submitting)\]$/.exec(name);
      if (field) return Object.hasOwn(form.dataset, field[1]) ? [form] : [];
      return [];
    });
  }
  const document = {
    documentElement: root,
    hidden: false,
    body: { classList: { contains() { return false; } } },
    // Focus may move away from the disabled submit button before a POST completes.
    activeElement: { matches() { return false; } },
    querySelectorAll: select,
    querySelector(selector) { return select(selector)[0] || null; },
  };
  vm.runInNewContext(source, {
    window: browser,
    document,
    localStorage: storage,
    sessionStorage: storage,
    location: { pathname: '/repos', search: '', reload() { reloads++; } },
    setTimeout(callback) { timers.push(callback); },
    setInterval(callback) { intervals.push(callback); },
  }, { filename: 'app/static/app.js' });
  return {
    form, button, themeButton, browser, root,
    tick() { for (const callback of intervals) callback(); },
    flushTimers() { while (timers.length) timers.shift()(); },
    submit() {
      const event = form.dispatch('submit', { submitter: button });
      if (!event.defaultPrevented) submissions++;
      return event;
    },
    get reloads() { return reloads; },
    get submissions() { return submissions; },
    get confirmations() { return confirmations; },
  };
}

test('automatic refresh pauses for unsaved input and resumes after reset', () => {
  const current = page();
  current.tick();
  assert.equal(current.reloads, 1);
  current.form.dispatch('input');
  current.tick();
  assert.equal(current.reloads, 1);
  current.form.dispatch('reset');
  current.tick();
  assert.equal(current.reloads, 2);
});

test('accepted POST is not interrupted by refresh after focus leaves the submitter', () => {
  const current = page();
  current.form.dispatch('input');
  assert.equal(current.submit().defaultPrevented, false);
  assert.equal(current.submissions, 1);
  assert.equal(current.button.disabled, false);
  current.flushTimers();
  assert.equal(current.button.disabled, true);
  current.tick();
  current.tick();
  assert.equal(current.reloads, 0);
});

test('cancelled confirmation prevents submission and retains unsaved input', () => {
  const current = page({ confirm: false });
  current.form.dataset.confirm = 'Remove this repository?';
  current.form.dispatch('input');
  assert.equal(current.submit().defaultPrevented, true);
  current.flushTimers();
  assert.equal(current.confirmations, 1);
  assert.equal(current.submissions, 0);
  assert.equal(current.form.dataset.editing, 'true');
  assert.equal(current.button.disabled, false);
  assert.equal(current.button.getAttribute('aria-busy'), null);
  assert.equal(Object.hasOwn(current.form.dataset, 'submitting'), false);
  current.tick();
  assert.equal(current.reloads, 0);
});

test('accepted confirmation submits once and remains protected from refresh', () => {
  const current = page();
  current.form.dataset.confirm = 'Disconnect GitHub?';
  current.submit();
  current.flushTimers();
  current.tick();
  assert.equal(current.confirmations, 1);
  assert.equal(current.submissions, 1);
  assert.equal(current.reloads, 0);
});

test('pageshow restores buttons and submission state after browser history navigation', () => {
  const current = page();
  current.form.dispatch('input');
  current.submit();
  current.flushTimers();
  assert.equal(current.button.getAttribute('aria-busy'), 'true');
  assert.equal(current.form.dataset.submitting, 'true');
  current.browser.dispatch('pageshow', { persisted: true });
  assert.equal(current.button.disabled, false);
  assert.equal(current.button.getAttribute('aria-busy'), null);
  assert.equal(Object.hasOwn(current.form.dataset, 'submitting'), false);
  current.tick();
  assert.equal(current.reloads, 1);
});

test('unavailable theme storage does not break theme controls or refresh guards', () => {
  const current = page({ storageFails: true });
  assert.equal(current.root.dataset.theme, 'light');
  current.themeButton.dispatch('click');
  assert.equal(current.root.dataset.theme, 'dark');
  current.form.dispatch('input');
  current.tick();
  assert.equal(current.reloads, 0);
  current.form.dispatch('reset');
  current.tick();
  assert.equal(current.reloads, 1);
});
