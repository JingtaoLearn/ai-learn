(() => {
  'use strict';
  const snapshot = JSON.parse(document.getElementById('snapshot-data').textContent);
  const liveStatuses = new Set(['running', 'review', 'ready', 'todo']);
  const statusNames = {
    running: '运行中', review: '待复审', ready: '待执行', todo: '等待依赖',
    blocked: '已阻塞', done: '已完成', archived: '已归档', scheduled: '已排期', triage: '待梳理'
  };
  const boardTabs = document.getElementById('board-tabs');
  const taskList = document.getElementById('task-list');
  const emptyState = document.getElementById('empty-state');
  const searchInput = document.getElementById('search-input');
  const statusSelect = document.getElementById('status-select');
  const dialog = document.getElementById('task-dialog');
  let board = 'all';
  let status = 'all';

  const safeDate = value => value ? new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: snapshot.timezone
  }).format(new Date(value)) : '—';

  function relativeDate(value) {
    if (!value) return '—';
    const seconds = Math.max(0, (new Date(snapshot.generated_at) - new Date(value)) / 1000);
    if (seconds < 60) return '刚刚';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  }

  function groupFor(task) {
    if (liveStatuses.has(task.status)) return 'live';
    if (task.status === 'blocked') return 'blocked';
    return 'done';
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderTabs() {
    const options = [{ slug: 'all', name: '全部' }, ...snapshot.boards];
    options.forEach(item => {
      const button = el('button', 'board-tab', item.name);
      button.type = 'button';
      button.role = 'tab';
      button.dataset.board = item.slug;
      button.setAttribute('aria-selected', String(item.slug === board));
      button.addEventListener('click', () => {
        board = item.slug;
        [...boardTabs.children].forEach(tab => tab.setAttribute('aria-selected', String(tab.dataset.board === board)));
        render();
      });
      boardTabs.append(button);
    });
  }

  function updateMetrics(scoped) {
    const counts = { live: 0, blocked: 0, done: 0 };
    scoped.forEach(task => { counts[groupFor(task)] += 1; });
    document.getElementById('metric-live').textContent = counts.live;
    document.getElementById('metric-blocked').textContent = counts.blocked;
    document.getElementById('metric-done').textContent = counts.done;
  }

  function openTask(task) {
    document.getElementById('dialog-board').textContent = `${task.board_name} · ${task.id}`;
    document.getElementById('dialog-title').textContent = task.title;
    const body = document.getElementById('dialog-body');
    body.replaceChildren();
    const dl = el('dl', 'detail-grid');
    const fields = [
      ['状态', statusNames[task.status] || task.status], ['Agent', task.assignee],
      ['优先级', String(task.priority)], ['创建', safeDate(task.created_at)],
      ['开始', safeDate(task.started_at)], ['完成', safeDate(task.completed_at)],
      ['心跳', safeDate(task.heartbeat_at)]
    ];
    fields.forEach(([label, value]) => {
      const wrap = el('div', 'detail');
      wrap.append(el('dt', '', label), el('dd', '', value));
      dl.append(wrap);
    });
    body.append(dl);
    if (task.summary) {
      const summary = el('section', 'detail-summary');
      summary.append(el('h3', '', '最新结果摘要'), el('p', '', task.summary));
      body.append(summary);
    }
    dialog.showModal();
  }

  function makeCard(task) {
    const button = el('button', 'task-card');
    button.type = 'button';
    button.setAttribute('aria-label', `查看任务：${task.title}`);
    const mark = el('span', `status-mark status-${task.status}`);
    mark.setAttribute('aria-hidden', 'true');
    const main = el('span', 'task-main');
    main.append(el('span', 'task-title', task.title));
    const meta = el('span', 'task-meta');
    meta.append(el('span', '', task.board_name), el('span', '', task.assignee), el('span', '', task.id));
    main.append(meta);
    const group = groupFor(task);
    main.append(el('span', `status-label ${group}`, statusNames[task.status] || task.status));
    const date = task.heartbeat_at || task.completed_at || task.started_at || task.created_at;
    button.append(mark, main, el('span', 'task-age', relativeDate(date)));
    button.addEventListener('click', () => openTask(task));
    return button;
  }

  function render() {
    const query = searchInput.value.trim().toLocaleLowerCase('zh-CN');
    const scoped = snapshot.tasks.filter(task => board === 'all' || task.board === board);
    updateMetrics(scoped);
    const filtered = scoped.filter(task => {
      const group = groupFor(task);
      const statusMatch = status === 'all' || group === status;
      const text = `${task.title} ${task.id} ${task.assignee} ${task.board_name}`.toLocaleLowerCase('zh-CN');
      return statusMatch && (!query || text.includes(query));
    });
    const rank = { running: 0, review: 1, ready: 2, todo: 3, blocked: 4, done: 5, archived: 6 };
    filtered.sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9) || new Date(b.completed_at || b.started_at || b.created_at) - new Date(a.completed_at || a.started_at || a.created_at));
    taskList.replaceChildren(...filtered.map(makeCard));
    emptyState.hidden = filtered.length !== 0;
    document.getElementById('result-count').textContent = `${filtered.length} 项`;
    const boardLabel = board === 'all' ? '全部看板' : snapshot.boards.find(item => item.slug === board)?.name;
    const statusLabel = status === 'all' ? '全部任务' : ({ live: '进行中', blocked: '需关注', done: '最近完成' })[status];
    document.getElementById('list-title').textContent = `${boardLabel} · ${statusLabel}`;
  }

  document.querySelectorAll('[data-quick-status]').forEach(button => button.addEventListener('click', () => {
    status = button.dataset.quickStatus;
    statusSelect.value = status;
    render();
    document.getElementById('task-list').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
  searchInput.addEventListener('input', render);
  statusSelect.addEventListener('change', () => { status = statusSelect.value; render(); });
  document.getElementById('reset-button').addEventListener('click', () => {
    board = 'all'; status = 'all'; searchInput.value = ''; statusSelect.value = 'all';
    [...boardTabs.children].forEach(tab => tab.setAttribute('aria-selected', String(tab.dataset.board === 'all')));
    render();
  });
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  document.getElementById('snapshot-note').textContent = `快照 ${safeDate(snapshot.generated_at)} · Asia/Shanghai · 不会修改原看板`;
  renderTabs();
  render();
})();
