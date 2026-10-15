(() => {
  const root = document.documentElement;
  root.classList.add('js');
  const themeButtons = document.querySelectorAll('.theme-toggle');
  function setTheme(theme) {
    root.dataset.theme = theme;
    themeButtons.forEach(button => {
      const label = theme === 'dark' ? '切换为浅色主题' : '切换为深色主题';
      button.setAttribute('aria-label', label);
      button.title = label;
    });
  }
  try {
    setTheme(localStorage.getItem('forkiq-theme') === 'dark' ? 'dark' : 'light');
  } catch {
    setTheme('light');
  }
  themeButtons.forEach(button => button.addEventListener('click', () => {
    const theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    setTheme(theme);
    try { localStorage.setItem('forkiq-theme', theme); } catch { /* Storage is optional. */ }
  }));

  const menu = document.querySelector('.menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.querySelector('.sidebar-overlay');
  const mobile = window.matchMedia('(max-width: 760px)');
  function closeMenu() {
    document.body.classList.remove('nav-open');
    if (overlay) overlay.hidden = true;
    if (menu) {
      menu.setAttribute('aria-expanded', 'false');
      menu.setAttribute('aria-label', '打开导航');
    }
    if (sidebar) sidebar.inert = mobile.matches;
  }
  function openMenu() {
    document.body.classList.add('nav-open');
    overlay.hidden = false;
    sidebar.inert = false;
    menu.setAttribute('aria-expanded', 'true');
    menu.setAttribute('aria-label', '关闭导航');
    sidebar.querySelector('a').focus();
  }
  if (menu && sidebar && overlay) {
    closeMenu();
    menu.addEventListener('click', () => {
      if (document.body.classList.contains('nav-open')) closeMenu(); else openMenu();
    });
    overlay.addEventListener('click', () => { closeMenu(); menu.focus(); });
    mobile.addEventListener('change', closeMenu);
    document.addEventListener('keydown', event => {
      if (!document.body.classList.contains('nav-open')) return;
      if (event.key === 'Escape') { closeMenu(); menu.focus(); }
      if (event.key === 'Tab') {
        const elements = [...sidebar.querySelectorAll('a, button')];
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
  }

  // Timestamps are stored in UTC. The server renders a short UTC form so the
  // page works without scripting; the browser re-renders every stamp in the
  // viewer's own timezone, including in fragments swapped in later.
  const pad = value => String(value).padStart(2, '0');
  function localizeTimes(root) {
    (root || document).querySelectorAll('time.ts[datetime]').forEach(element => {
      const when = new Date(element.getAttribute('datetime'));
      if (Number.isNaN(when.getTime())) return;
      element.textContent = pad(when.getMonth() + 1) + '-' + pad(when.getDate()) + ' ' +
        pad(when.getHours()) + ':' + pad(when.getMinutes());
      element.title = when.toLocaleString();
    });
  }
  localizeTimes();

  const errorDialog = document.querySelector('#error-dialog');
  if (errorDialog && typeof errorDialog.showModal === 'function') {
    const title = errorDialog.querySelector('#error-dialog-title');
    const repo = errorDialog.querySelector('#error-dialog-repo');
    const message = errorDialog.querySelector('#error-dialog-message');
    const close = errorDialog.querySelector('[data-error-close]');
    let trigger = null;
    document.addEventListener('click', event => {
      const link = event.target.closest?.('a.error-details');
      if (!link || event.defaultPrevented || event.button > 0 ||
          event.ctrlKey || event.metaKey || event.shiftKey || event.altKey ||
          (link.getAttribute('target') && link.getAttribute('target') !== '_self') ||
          link.hasAttribute('download')) return;
      event.preventDefault();
      if (errorDialog.open) return;
      trigger = link;
      title.textContent = link.dataset.errorStatus ? '状态详情 · ' + link.dataset.errorStatus : '状态详情';
      repo.textContent = link.dataset.errorRepo || '';
      message.textContent = link.dataset.errorMessage || '';
      errorDialog.showModal();
    });
    close.addEventListener('click', () => errorDialog.close());
    errorDialog.addEventListener('click', event => {
      if (event.target !== errorDialog) return;
      const bounds = errorDialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right ||
          event.clientY < bounds.top || event.clientY > bounds.bottom) errorDialog.close();
    });
    errorDialog.addEventListener('close', () => {
      if (trigger?.isConnected) trigger.focus();
      trigger = null;
    });
  }

  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('input', () => { form.dataset.editing = 'true'; });
    form.addEventListener('submit', event => {
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
      }
      const button = event.submitter;
      if (button) {
        // Preserve submitter values; disable only after native form data is captured.
        setTimeout(() => { button.disabled = true; button.setAttribute('aria-busy', 'true'); }, 0);
      }
      form.dataset.submitting = 'true';
      delete form.dataset.editing;
    });
    form.addEventListener('reset', () => { delete form.dataset.editing; });
  });
  // Background refresh replaces only the list region. A full reload re-fetched
  // the shell, CSS and JS every 30 seconds and made the panel feel sluggish, so
  // the server renders the same rows into a fragment that is swapped in place.
  const refresh = document.querySelector('[data-auto-refresh]');
  if (refresh) {
    const region = document.querySelector('#' + refresh.dataset.refreshRegion);
    // Keep reading and keyboard focus intact, including while a fetch is in flight.
    const busy = () => document.hidden || document.body.classList.contains('nav-open') ||
      errorDialog?.open || document.querySelector('form[data-editing], form[data-submitting]') ||
      document.activeElement?.matches('input, select, textarea, button') ||
      (region?.contains(document.activeElement) && document.activeElement?.matches('a[href]'));
    let inFlight = false;
    // Cloudflare rewrites the HTML it proxies and drops the ETag header, so the
    // revision the server stamps into the markup is sent back as a query
    // parameter instead of an If-None-Match header.
    const revision = () => region?.querySelector('[data-revision]')?.dataset.revision || '';
    const update = async () => {
      if (busy() || inFlight) return;
      const url = region && region.dataset.refreshUrl;
      if (!url || typeof window.fetch !== 'function') { location.reload(); return; }
      inFlight = true;
      try {
        const known = revision();
        const target = known ? url + (url.includes('?') ? '&' : '?') + 'v=' + known : url;
        const response = await window.fetch(target, { cache: 'no-store' });
        // An expired session must fall back to a navigation so /login is shown.
        if (response.status === 401) { location.reload(); return; }
        // 204: nothing changed since the last tick, so the DOM stays untouched.
        if (response.status === 204 || !response.ok) return;
        const html = await response.text();
        if (!busy() && region.innerHTML !== html) { region.innerHTML = html; localizeTimes(region); }
      } catch { /* Keep the current view; the next tick retries. */ }
      finally { inFlight = false; }
    };
    setInterval(update, Number(refresh.dataset.autoRefresh) * 1000);
    if (document.addEventListener) {
      document.addEventListener('visibilitychange', () => { if (!document.hidden) update(); });
    }
  }
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('form').forEach(form => { delete form.dataset.submitting; });
    document.querySelectorAll('button[aria-busy]').forEach(button => {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    });
  });
})();
