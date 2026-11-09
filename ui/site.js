// The Rust tab on a site: the dev server's last build and an on-demand
// cargo check. Every value from the API is set as text, never as HTML.
(() => {
  const ctx = lerd.context;
  const query = 'site=' + encodeURIComponent(ctx.site) +
    (ctx.branch ? '&branch=' + encodeURIComponent(ctx.branch) : '');
  const $ = (id) => document.getElementById(id);
  let timer = null;
  let lastStatus = null;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'text') node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children) if (child) node.append(child);
    return node;
  }

  function pill(text, tone) {
    return el('span', { class: 'lerd-pill' + (tone ? ' ' + tone : ''), text });
  }

  function ago(seconds) {
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 60) return seconds + 's ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    return Math.floor(seconds / 3600) + 'h ago';
  }

  function showEmpty(title, detail) {
    $('main').hidden = true;
    const box = $('empty');
    box.hidden = false;
    box.querySelector('strong').textContent = title;
    box.querySelector('span').textContent = detail || '';
  }

  function diagnosticsList(container, result, emptyText) {
    container.replaceChildren();
    if (!result) return;
    const items = result.diagnostics || [];
    if (result.cargo_error && !items.length) {
      container.append(el('div', { class: 'lerd-card' },
        el('pre', { class: 'lerd-mono', style: 'margin:0;padding:12px;overflow-x:auto', text: result.cargo_error })));
      return;
    }
    if (!items.length) {
      container.append(el('div', { class: 'lerd-empty' }, el('strong', { text: emptyText })));
      return;
    }
    const card = el('div', { class: 'lerd-card flush' });
    for (const item of items) {
      const where = [
        item.file ? `${item.file}:${item.line}:${item.column}` : null,
        item.code, item.target,
      ].filter(Boolean).join(' · ');
      const body = el('div', { class: 'body' },
        el('div', { text: item.message }),
        where ? el('div', { class: 'where lerd-mono lerd-muted', text: where }) : null);
      if (item.rendered) {
        body.append(el('details', {},
          el('summary', { class: 'lerd-muted', text: 'Compiler output' }),
          el('pre', { class: 'lerd-mono', text: item.rendered })));
      }
      card.append(el('div', { class: 'lerd-row diag' },
        pill(item.level, item.level === 'error' ? 'bad' : 'warn'), body));
    }
    if (result.truncated) {
      card.append(el('div', { class: 'lerd-row lerd-muted', text: 'Only the first diagnostics are shown.' }));
    }
    container.append(card);
  }

  function counts(result) {
    const parts = [];
    parts.push(result.errors + (result.errors === 1 ? ' error' : ' errors'));
    parts.push(result.warnings + (result.warnings === 1 ? ' warning' : ' warnings'));
    return parts.join(', ');
  }

  function render(status) {
    lastStatus = status;
    if (!status.rust) {
      showEmpty('Not a Rust project', 'There is no Cargo.toml in ' + status.path + '.');
      return;
    }
    $('empty').hidden = true;
    $('main').hidden = false;

    const dev = status.dev || {};
    const server = status.dev_server;
    const build = server && server.build;
    const pills = $('pills');
    pills.replaceChildren();
    pills.append(dev.watcher ? pill('rebuild on save', 'ok') : pill('rebuild on save off'));

    if (server && server.phase === 'stopped') pills.append(pill('dev server stopped'));
    if (server && server.building_since) pills.append(pill('building', 'accent'));
    if (build) pills.append(build.ok ? pill('build ok', 'ok') : pill('build failed', 'bad'));
    const app = server && server.app;
    if (app && app.listening === true) pills.append(pill('listening on ' + app.address, 'ok'));
    else if (app && app.listening === false) pills.append(pill('not answering on ' + app.address, 'warn'));
    else if (server && server.phase !== 'stopped' && server.app_exit !== null && server.app_exit !== undefined) {
      pills.append(pill('app exited (' + server.app_exit + ')', 'bad'));
      if (server.restart_at) pills.append(pill('starting again', 'accent'));
    }

    const note = $('note');
    note.hidden = true;
    if (server && server.serving_previous) {
      note.hidden = false;
      note.textContent = 'The last build failed, so the site still serves the previous build. Fix the errors below and save.';
    } else if (app && app.listening === false) {
      note.hidden = false;
      note.textContent = 'The app is running, but nothing answers on ' + app.address +
        '. Under Lerd it must listen on the HOST and PORT environment variables.';
    }

    const setup = $('setup');
    setup.hidden = !!dev.watcher;
    if (!dev.watcher) {
      let text = 'Lerd restarts this site only when you ask it to.';
      if (dev.rustyard) text = 'This site runs through Rustyard, which rebuilds on save itself.';
      else if (dev.error) text = 'Its .lerd.yaml could not be read: ' + dev.error;
      else if (dev.command) text = 'Its dev command is ' + dev.command + ', which does not rebuild on save.';
      $('setup-text').textContent = text;
    }

    const title = $('build-title');
    if (server && server.building_since) {
      title.textContent = 'Building for ' + Math.round(status.now - server.building_since) + 's' +
        (build ? '; below is the previous build' : '');
    } else if (build) {
      title.textContent = (build.ok ? 'Built' : 'Failed') + ' ' + ago(status.now - build.finished_at) +
        ' in ' + build.duration + 's: ' + counts(build);
    } else {
      title.textContent = dev.watcher ? 'No build recorded yet; the dev server records one when it starts.' : '';
    }
    diagnosticsList($('build-list'), build, 'No errors or warnings');
    $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
  }

  async function refresh() {
    clearTimeout(timer);
    try {
      render(JSON.parse(await lerd.api('status?' + query)));
    } catch (err) {
      showEmpty('Could not read the dev server status', err.message);
    }
    const busy = lastStatus && lastStatus.dev_server && lastStatus.dev_server.building_since;
    timer = setTimeout(tick, busy ? 1500 : 5000);
  }

  function tick() {
    if (document.hidden) timer = setTimeout(tick, 5000);
    else refresh();
  }

  async function loadProject() {
    try {
      const project = JSON.parse(await lerd.api('project?' + query));
      const root = (project.packages || []).find((p) => p.root) || (project.packages || [])[0];
      if (root) $('package').textContent = root.name + ' ' + root.version;
      const meta = [];
      if (root && root.edition) meta.push('edition ' + root.edition);
      if (project.workspace) meta.push(project.packages.length + ' packages');
      if (project.rustc) meta.push(project.rustc.split(' (')[0]);
      if (project.error) meta.push(project.error.split('\n')[0]);
      $('meta').textContent = meta.join(' · ');
    } catch (err) {
      // Remote dashboards cannot run cargo here; the rest still works.
    }
  }

  function select(tab) {
    for (const name of ['build', 'check']) {
      $('tab-' + name).setAttribute('aria-selected', String(name === tab));
      $('panel-' + name).hidden = name !== tab;
    }
  }

  $('tab-build').addEventListener('click', () => select('build'));
  $('tab-check').addEventListener('click', () => select('check'));
  $('run-check').addEventListener('click', async () => {
    const button = $('run-check');
    button.disabled = true;
    button.textContent = 'Checking...';
    $('check-title').textContent = 'cargo check --all-targets is running; the first run can take a while.';
    $('check-list').replaceChildren();
    try {
      const result = JSON.parse(await lerd.api('check?' + query, { method: 'POST' }));
      $('check-title').textContent = (result.ok ? 'Passed' : 'Failed') + ' in ' + result.duration + 's: ' + counts(result);
      diagnosticsList($('check-list'), result, 'No errors or warnings');
    } catch (err) {
      $('check-title').textContent = 'cargo check could not run: ' + err.message;
    } finally {
      button.disabled = false;
      button.textContent = 'Run cargo check';
    }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

  loadProject();
  refresh();
})();
