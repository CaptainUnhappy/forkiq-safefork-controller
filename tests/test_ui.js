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
    hasAttribute(name) { return attributes.has(name); },
  };
}

// Execute the production script with isolated browser primitives and no network.
function page({ confirm = true, storageFails = false, dialogSupported = true, dialogPresent = true } = {}) {
  const form = element();
  const button = element();
  const themeButton = element();
  const region = element();
  const browser = element();
  const dialog = element();
  const errorTitle = element();
  const errorRepo = element();
  const errorMessage = element();
  const closeButton = element();
  const links = new Set();
  region.contains = candidate => links.has(candidate);
  dialog.open = false;
  dialog.querySelector = selector => ({
    '#error-dialog-title': errorTitle,
    '#error-dialog-repo': errorRepo,
    '#error-dialog-message': errorMessage,
    '[data-error-close]': closeButton,
  })[selector] || null;
  dialog.getBoundingClientRect = () => ({ left: 100, right: 740, top: 100, bottom: 500 });
  if (dialogSupported) {
    dialog.showModal = () => { dialog.open = true; document.activeElement = closeButton; };
  }
  dialog.close = () => { dialog.open = false; dialog.dispatch('close'); };
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
  // One stored timestamp, rendered by the server as a short UTC string.
  const timeStamp = element();
  timeStamp.setAttribute('datetime', '2026-10-15T03:55:19+00:00');
  timeStamp.textContent = '10-15 03:55';
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
      if (name === '#error-dialog') return dialogPresent ? [dialog] : [];
      if (name === '[data-auto-refresh]') {
        return [{ dataset: { autoRefresh: '30', refreshRegion: 'repo-table-region' } }];
      }
      if (name === '#repo-table-region') return [region];
      if (name === 'time.ts[datetime]') return timeStamp.getAttribute('datetime') ? [timeStamp] : [];
      if (name === 'form') return [form];
      if (name === 'button[aria-busy]') return button.getAttribute('aria-busy') ? [button] : [];
      const field = /^form\[data-(editing|submitting)\]$/.exec(name);
      if (field) return Object.hasOwn(form.dataset, field[1]) ? [form] : [];
      return [];
    });
  }
  // A refreshed fragment is re-scanned for timestamps in the viewer's timezone.
  region.querySelectorAll = select;
  const idleFocus = { matches() { return false; } };
  const document = {
    ...element(),
    documentElement: root,
    hidden: false,
    body: { classList: { contains() { return false; } } },
    // Focus may move away from the disabled submit button before a POST completes.
    activeElement: idleFocus,
    querySelectorAll: select,
    querySelector(selector) { return select(selector)[0] || null; },
  };
  closeButton.matches = selector => selector === 'input, select, textarea, button';
  function createErrorLink(data = {}) {
    const link = element();
    link.dataset = {
      errorRepo: 'me/demo', errorStatus: '存在分叉', errorMessage: 'GitHub merge conflict', ...data,
    };
    link.isConnected = true;
    link.matches = selector => selector === 'a[href]' || selector === 'a.error-details';
    link.closest = selector => link.matches(selector) ? link : null;
    link.focus = () => { document.activeElement = link; };
    links.add(link);
    return link;
  }
  const errorLink = createErrorLink();
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
    form, button, themeButton, browser, root, region, fetches, timeStamp,
    document, dialog, errorTitle, errorRepo, errorMessage, closeButton, errorLink, createErrorLink,
    clickError(link = errorLink, values = {}) {
      return document.dispatch('click', { button: 0, target: link, ...values });
    },
    closeError() { closeButton.dispatch('click'); },
    cancelError() {
      const event = dialog.dispatch('cancel');
      if (!event.defaultPrevented) dialog.close();
      return event;
    },
    focusOutside() { document.activeElement = idleFocus; },
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

test('delegated details show a plain-text snapshot and focus returns to its link', () => {
  const current = page();
  const message = '<img src=x onerror="alert(1)">\n' + '超长异常'.repeat(200);
  current.errorLink.dataset.errorMessage = message;
  const child = { closest: selector => selector === 'a.error-details' ? current.errorLink : null };
  const event = current.clickError(current.errorLink, { target: child });
  assert.equal(event.defaultPrevented, true);
  assert.equal(current.dialog.open, true);
  assert.equal(current.errorTitle.textContent, '状态详情 · 存在分叉');
  assert.equal(current.errorRepo.textContent, 'me/demo');
  assert.equal(current.errorMessage.textContent, message);
  assert.equal(current.errorMessage.innerHTML, undefined);
  current.errorLink.dataset.errorMessage = 'changed after opening';
  assert.equal(current.errorMessage.textContent, message);
  current.closeError();
  assert.equal(current.dialog.open, false);
  assert.equal(current.document.activeElement, current.errorLink);
});

test('delegation opens a details link added by a later fragment refresh', () => {
  const current = page();
  const added = current.createErrorLink({ errorRepo: 'me/added', errorStatus: '检查失败', errorMessage: 'new failure' });
  assert.equal(current.clickError(added).defaultPrevented, true);
  assert.equal(current.errorRepo.textContent, 'me/added');
  assert.equal(current.errorMessage.textContent, 'new failure');
  current.closeError();
  assert.equal(current.document.activeElement, added);
});

test('modified clicks and explicit new-tab or download links keep native navigation', () => {
  const current = page();
  for (const values of [{ button: 1 }, { button: 2 }, { ctrlKey: true }, { metaKey: true },
                        { shiftKey: true }, { altKey: true }]) {
    assert.equal(current.clickError(current.errorLink, values).defaultPrevented, false);
    assert.equal(current.dialog.open, false);
  }
  current.errorLink.setAttribute('target', '_blank');
  assert.equal(current.clickError().defaultPrevented, false);
  current.errorLink.removeAttribute('target');
  current.errorLink.setAttribute('download', '');
  assert.equal(current.clickError().defaultPrevented, false);
  assert.equal(current.dialog.open, false);
});

test('missing native dialog support leaves the repository detail link usable', () => {
  for (const options of [{ dialogSupported: false }, { dialogPresent: false }]) {
    const current = page(options);
    assert.equal(current.clickError().defaultPrevented, false);
    assert.equal(current.dialog.open, false);
    assert.equal(current.errorMessage.textContent, undefined);
  }
});

test('keyboard activation opens details and native dialog cancellation restores focus', () => {
  const current = page();
  current.errorLink.focus();
  assert.equal(current.clickError(current.errorLink, { button: undefined }).defaultPrevented, true);
  assert.equal(current.dialog.open, true);
  // Browsers close native modal dialogs on an uncancelled Escape/cancel event.
  assert.equal(current.cancelError().defaultPrevented, false);
  assert.equal(current.dialog.open, false);
  assert.equal(current.document.activeElement, current.errorLink);
});

test('only a click outside the dialog bounds dismisses its backdrop', () => {
  const current = page();
  current.clickError();
  current.dialog.dispatch('click', { target: current.dialog, clientX: 140, clientY: 150 });
  assert.equal(current.dialog.open, true);
  current.dialog.dispatch('click', { target: current.dialog, clientX: 20, clientY: 20 });
  assert.equal(current.dialog.open, false);
  assert.equal(current.document.activeElement, current.errorLink);
});

test('closing details does not focus a link removed from the document', () => {
  const current = page();
  current.clickError();
  current.errorLink.isConnected = false;
  current.closeError();
  assert.notEqual(current.document.activeElement, current.errorLink);
});

test('refresh pauses while details are open or the returned link retains focus', async () => {
  const current = page();
  current.clickError();
  current.focusOutside();
  await current.tickAsync();
  assert.equal(current.fetches.length, 0);
  current.closeError();
  await current.tickAsync();
  assert.equal(current.fetches.length, 0);
  current.focusOutside();
  await current.tickAsync();
  assert.equal(current.fetches.length, 1);
  assert.equal(current.region.innerHTML, '<p>rows</p>');
});

test('an in-flight refresh cannot replace the list after details open', async () => {
  const current = page();
  let resolveFetch;
  current.setFetchResponse(new Promise(resolve => { resolveFetch = resolve; }));
  const refresh = current.tickAsync();
  assert.equal(current.fetches.length, 1);
  current.clickError();
  current.focusOutside();
  resolveFetch({ status: 200, ok: true, text: async () => '<p>changed rows</p>' });
  await refresh;
  assert.equal(current.dialog.open, true);
  assert.equal(current.region.innerHTML, undefined);
  assert.equal(current.errorMessage.textContent, 'GitHub merge conflict');
  current.closeError();
  current.focusOutside();
  current.setFetchResponse({ status: 200, ok: true, text: async () => '<p>latest rows</p>' });
  await current.tickAsync();
  assert.equal(current.region.innerHTML, '<p>latest rows</p>');
});

test('links focused inside the list protect focus before and after a fetch', async () => {
  const current = page();
  current.errorLink.focus();
  await current.tickAsync();
  assert.equal(current.fetches.length, 0);
  current.focusOutside();
  let resolveFetch;
  current.setFetchResponse(new Promise(resolve => { resolveFetch = resolve; }));
  const refresh = current.tickAsync();
  current.errorLink.focus();
  resolveFetch({ status: 200, ok: true, text: async () => '<p>new rows</p>' });
  await refresh;
  assert.equal(current.region.innerHTML, undefined);
  assert.equal(current.document.activeElement, current.errorLink);
});

test('only one background fetch runs while its response is pending', async () => {
  const current = page();
  let resolveFetch;
  current.setFetchResponse(new Promise(resolve => { resolveFetch = resolve; }));
  const refresh = current.tickAsync();
  await current.tickAsync();
  assert.equal(current.fetches.length, 1);
  resolveFetch({ status: 204, ok: true, text: async () => '' });
  await refresh;
  assert.equal(current.region.innerHTML, undefined);
});

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

test('stored timestamps are rewritten in the viewer timezone', async () => {
  const current = page();
  const expected = (when => String(when.getMonth() + 1).padStart(2, '0') + '-' +
    String(when.getDate()).padStart(2, '0') + ' ' +
    String(when.getHours()).padStart(2, '0') + ':' +
    String(when.getMinutes()).padStart(2, '0'))(new Date('2026-10-15T03:55:19+00:00'));
  assert.equal(current.timeStamp.textContent, expected);
  assert.match(current.timeStamp.textContent, /^\d\d-\d\d \d\d:\d\d$/);
  assert.equal(current.timeStamp.title, new Date('2026-10-15T03:55:19+00:00').toLocaleString());
});

test('a refreshed fragment has its timestamps rewritten too', async () => {
  const current = page();
  current.timeStamp.textContent = '10-15 03:55';
  current.setFetchResponse({ status: 200, ok: true, text: async () => '<p>rows</p>' });
  await current.tickAsync();
  assert.match(current.timeStamp.textContent, /^\d\d-\d\d \d\d:\d\d$/);
  assert.notEqual(current.timeStamp.textContent, '10-15 03:55');
});
