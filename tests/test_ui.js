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
  const region = element();
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
  const fetches = [];
  let reloads = 0;
  let submissions = 0;
  let confirmations = 0;
  let fetchResponse = { status: 200, ok: true, text: async () => '<p>rows</p>' };
  region.dataset.refreshUrl = '/partials/repos?filter=';
  const revisionNode = element();
  revisionNode.dataset.revision = 'rev-1';
  region.querySelector = () => revisionNode;
  browser.confirm = () => { confirmations++; return confirm; };
  browser.fetch = (url, options) => {
    fetches.push({ url, options });
    return Promise.resolve(fetchResponse);
  };
  const storage = {
    getItem() { if (storageFails) throw new Error('Storage denied'); return null; },
    setItem() { if (storageFails) throw new Error('Storage denied'); },
  };
  function select(selector) {
    return selector.split(',').flatMap(part => {
      const name = part.trim();
      if (name === '.theme-toggle') return [themeButton];
      if (name === '[data-auto-refresh]') {
        return [{ dataset: { autoRefresh: '30', refreshRegion: 'repo-table-region' } }];
      }
      if (name === '#repo-table-region') return [region];
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
    addEventListener() {},
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
    form, button, themeButton, browser, root, region, fetches,
    setFetchResponse(response) { fetchResponse = response; },
    tick() { for (const callback of intervals) callback(); },
    // Interval callbacks await the fragment fetch; let that microtask settle.
    async tickAsync() {
      for (const callback of intervals) await callback();
      await Promise.resolve();
    },
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

test('background refresh swaps the list region instead of reloading the page', async () => {
  const current = page();
  await current.tickAsync();
  assert.equal(current.reloads, 0);
  assert.equal(current.fetches.length, 1);
  // The revision rendered into the page is sent on the very first tick.
  assert.equal(current.fetches[0].url, '/partials/repos?filter=&v=rev-1');
  assert.equal(current.region.innerHTML, '<p>rows</p>');
});

test('the stored revision is sent back so an unchanged tick can be skipped', async () => {
  const current = page();
  await current.tickAsync();
  assert.equal(current.region.innerHTML, '<p>rows</p>');
  current.setFetchResponse({ status: 204, ok: false, text: async () => '' });
  await current.tickAsync();
  assert.equal(current.reloads, 0);
  // The previous rows stay visible and the request carried the stored revision.
  assert.equal(current.region.innerHTML, '<p>rows</p>');
  assert.equal(current.fetches[1].url, '/partials/repos?filter=&v=rev-1');
});

test('identical fragment bytes do not rewrite the region', async () => {
  const current = page();
  await current.tickAsync();
  current.region.innerHTML = '<p>rows</p>';
  let writes = 0;
  Object.defineProperty(current.region, 'innerHTML', {
    get() { return '<p>rows</p>'; },
    set() { writes++; },
    configurable: true,
  });
  await current.tickAsync();
  assert.equal(writes, 0);
});

test('expired session falls back to a navigation so the login page is shown', async () => {
  const current = page();
  current.setFetchResponse({ status: 401, ok: false, headers: { get: () => null },
                             text: async () => '{"error":"未登录"}' });
  await current.tickAsync();
  assert.equal(current.reloads, 1);
  assert.equal(current.region.innerHTML, undefined);
});

test('failed refresh keeps the current rows visible', async () => {
  const current = page();
  current.setFetchResponse({ status: 500, ok: false, headers: { get: () => null }, text: async () => 'boom' });
  await current.tickAsync();
  assert.equal(current.reloads, 0);
  assert.equal(current.region.innerHTML, undefined);
});

test('automatic refresh pauses for unsaved input and resumes after reset', async () => {
  const current = page();
  await current.tickAsync();
  assert.equal(current.fetches.length, 1);
  current.form.dispatch('input');
  await current.tickAsync();
  assert.equal(current.fetches.length, 1);
  current.form.dispatch('reset');
  await current.tickAsync();
  assert.equal(current.fetches.length, 2);
});

test('accepted POST is not interrupted by refresh after focus leaves the submitter', async () => {
  const current = page();
  current.form.dispatch('input');
  assert.equal(current.submit().defaultPrevented, false);
  assert.equal(current.submissions, 1);
  assert.equal(current.button.disabled, false);
  current.flushTimers();
  assert.equal(current.button.disabled, true);
  await current.tickAsync();
  await current.tickAsync();
  assert.equal(current.fetches.length, 0);
  assert.equal(current.reloads, 0);
});

test('cancelled confirmation prevents submission and retains unsaved input', async () => {
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
  await current.tickAsync();
  assert.equal(current.fetches.length, 0);
});

test('accepted confirmation submits once and remains protected from refresh', async () => {
  const current = page();
  current.form.dataset.confirm = 'Disconnect GitHub?';
  current.submit();
  current.flushTimers();
  await current.tickAsync();
  assert.equal(current.confirmations, 1);
  assert.equal(current.submissions, 1);
  assert.equal(current.fetches.length, 0);
});

test('pageshow restores buttons and submission state after browser history navigation', async () => {
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
  await current.tickAsync();
  assert.equal(current.fetches.length, 1);
});

test('unavailable theme storage does not break theme controls or refresh guards', async () => {
  const current = page({ storageFails: true });
  assert.equal(current.root.dataset.theme, 'light');
  current.themeButton.dispatch('click');
  assert.equal(current.root.dataset.theme, 'dark');
  current.form.dispatch('input');
  await current.tickAsync();
  assert.equal(current.fetches.length, 0);
  current.form.dispatch('reset');
  await current.tickAsync();
  assert.equal(current.fetches.length, 1);
});
