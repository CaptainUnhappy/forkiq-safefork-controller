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
  // Do not discard typed form values, background tabs or an open mobile menu.
  const refresh = document.querySelector('[data-auto-refresh]');
  if (refresh) {
    setInterval(() => {
      if (document.hidden || document.body.classList.contains('nav-open') ||
          document.querySelector('form[data-editing], form[data-submitting]') ||
          document.activeElement?.matches('input, select, textarea, button')) return;
      const scroll = { x: window.scrollX, y: window.scrollY };
      try { sessionStorage.setItem('forkiq-scroll:' + location.pathname + location.search, JSON.stringify(scroll)); } catch { /* Optional. */ }
      location.reload();
    }, Number(refresh.dataset.autoRefresh) * 1000);
    try {
      const key = 'forkiq-scroll:' + location.pathname + location.search;
      const saved = sessionStorage.getItem(key);
      if (saved) { const position = JSON.parse(saved); window.scrollTo(position.x, position.y); sessionStorage.removeItem(key); }
    } catch { /* Ignore unavailable or outdated scroll storage. */ }
  }
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('form').forEach(form => { delete form.dataset.submitting; });
    document.querySelectorAll('button[aria-busy]').forEach(button => {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    });
  });
})();
