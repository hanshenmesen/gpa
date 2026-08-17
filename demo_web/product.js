(() => {
  if (globalThis.__gpaProductEnhancements) return;
  globalThis.__gpaProductEnhancements = true;

  const isTyping = () => ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName || '') || document.activeElement?.isContentEditable;
  const platformKey = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? '⌘ K' : 'Ctrl K';
  const saveKey = platformKey.startsWith('⌘') ? '⌘ S' : 'Ctrl S';
  let lastFocused = null;
  let activeIndex = 0;
  let visibleItems = [];
  let replayCommandItems = [];
  let replayCommandsPromise = null;
  let replayCommandsLoaded = false;
  let replayCommandsError = false;
  let mobileSearchTrigger = null;
  let shortcutLastFocused = null;
  let routeChordTimer = null;
  let routeChordActive = false;
  let commandCloseTimer = null;

  const overlay = document.createElement('div');
  overlay.className = 'command-overlay';
  overlay.hidden = true;
  overlay.innerHTML = `
    <section class="command-dialog" role="dialog" aria-modal="true" aria-labelledby="commandTitle">
      <div class="command-search-row">
        <span class="command-search-icon" aria-hidden="true"></span>
        <input id="commandSearch" type="search" autocomplete="off" placeholder="搜索页面、Replay 或操作" aria-label="全局搜索" role="combobox" aria-expanded="true" aria-controls="commandList" aria-autocomplete="list">
        <kbd>Esc</kbd>
      </div>
      <h2 id="commandTitle" class="sr-only">全局搜索</h2>
      <div class="command-list" id="commandList" role="listbox" aria-label="全局搜索结果"></div>
      <footer><span id="commandStatus" role="status" aria-live="polite">正在准备结果</span><span>↑↓ 选择 · Enter 打开</span><span>${platformKey}</span></footer>
    </section>`;
  document.body.appendChild(overlay);

  const shortcutDialog = document.createElement('dialog');
  shortcutDialog.className = 'product-shortcuts';
  shortcutDialog.setAttribute('aria-labelledby', 'productShortcutsTitle');
  shortcutDialog.setAttribute('aria-keyshortcuts', '?');
  shortcutDialog.innerHTML = `
    <div class="product-shortcuts-head">
      <div><small>键盘操作</small><h2 id="productShortcutsTitle">更快完成常用操作</h2></div>
      <button type="button" class="secondary" data-close-shortcuts aria-label="关闭快捷键说明">×</button>
    </div>
    <div class="product-shortcuts-list">
      <div><span><strong>全局搜索</strong><small>打开页面、Replay 与操作</small></span><kbd>${platformKey}</kbd></div>
      <div data-shortcut-scope="save"><span><strong>保存 Replay</strong><small>保存当前编辑内容</small></span><kbd>${saveKey}</kbd></div>
      <div data-shortcut-scope="search"><span><strong>搜索当前页面</strong><small>聚焦当前目录的搜索框</small></span><kbd>/</kbd></div>
      <div data-shortcut-scope="library"><span><strong>浏览 Replay</strong><small>在工作台列表中移动焦点</small></span><kbd>↑ ↓ / J K</kbd></div>
      <div><span><strong>切换页面</strong><small>商店 / 运行准备 / 运行状态 / 工作台</small></span><kbd>G S / G P / G R / G W</kbd></div>
      <div><span><strong>关闭当前面板</strong><small>退出搜索、菜单或弹窗</small></span><kbd>Esc</kbd></div>
    </div>
    <footer><span>在非输入状态按</span><kbd>?</kbd><span>随时打开此说明</span></footer>`;
  document.body.appendChild(shortcutDialog);

  const guideDialog = document.createElement('dialog');
  guideDialog.className = 'product-guide';
  guideDialog.setAttribute('aria-labelledby', 'productGuideTitle');
  guideDialog.innerHTML = `
    <div class="product-guide-head">
      <div><small>使用说明</small><h2 id="productGuideTitle">从录制到安全复现</h2></div>
      <button type="button" class="secondary" data-close-guide aria-label="关闭使用说明">×</button>
    </div>
    <div class="product-guide-body">
      <ol>
        <li><span>1</span><div><strong>完成运行准备</strong><p>配置自己的模型 API；需要网站资料库时连接在线账号，需要可信桌面任务时再主动开启本次会话授权。</p></div></li>
        <li><span>2</span><div><strong>录制任务</strong><p>在工作台描述目标并完成一次操作。Agent 会理解意图、去掉误点和回退，并合并连续输入与重复动作。</p></div></li>
        <li><span>3</span><div><strong>检查 Replay</strong><p>确认步骤、变量和成功条件。系统会自动保存录制主机、浏览器、屏幕与权限环境。</p></div></li>
        <li><span>4</span><div><strong>发布或导入</strong><p>商店会检查来源、录屏、环境证据和复现条件；导入只保存，不会直接运行。</p></div></li>
        <li><span>5</span><div><strong>安全复现</strong><p>运行前 Agent 会比较当前主机与录制环境。可适配时生成计划，不兼容时阻止复用坐标或快捷键。</p></div></li>
      </ol>
      <section><strong>安全原则</strong><p>键盘输入前确认焦点；界面变化后重新定位；成功条件不满足时立即停止。</p></section>
    </div>`;
  document.body.appendChild(guideDialog);

  const routeChordHint = document.createElement('div');
  routeChordHint.className = 'route-chord-hint';
  routeChordHint.hidden = true;
  routeChordHint.setAttribute('role', 'status');
  routeChordHint.setAttribute('aria-live', 'polite');
  routeChordHint.innerHTML = '<kbd>G</kbd><span>然后按 <b>S</b> 商店 · <b>P</b> 运行准备 · <b>R</b> 运行状态 · <b>W</b> 工作台</span>';
  document.body.appendChild(routeChordHint);

  const isDialogBackdropClick = (dialog, event) => {
    if (event.target !== dialog) return false;
    const rect = dialog.getBoundingClientRect();
    return event.clientX < rect.left
      || event.clientX > rect.right
      || event.clientY < rect.top
      || event.clientY > rect.bottom;
  };

  const hideRouteChord = () => {
    routeChordActive = false;
    if (routeChordTimer) window.clearTimeout(routeChordTimer);
    routeChordTimer = null;
    routeChordHint.classList.remove('show');
    window.setTimeout(() => { if (!routeChordActive) routeChordHint.hidden = true; }, 120);
  };
  const startRouteChord = () => {
    routeChordActive = true;
    if (routeChordTimer) window.clearTimeout(routeChordTimer);
    routeChordHint.hidden = false;
    requestAnimationFrame(() => routeChordHint.classList.add('show'));
    routeChordTimer = window.setTimeout(hideRouteChord, 1600);
  };

  const syncShortcutGuide = () => {
    const availability = {
      save: Boolean(document.querySelector('#saveWorkflow')),
      search: Boolean(document.querySelector('#storeSearch, #search')),
      library: Boolean(document.querySelector('#workflowList')),
    };
    shortcutDialog.querySelectorAll('[data-shortcut-scope]').forEach(row => {
      row.hidden = !availability[row.dataset.shortcutScope];
    });
  };

  const openShortcutGuide = () => {
    if (shortcutDialog.open) return;
    hideRouteChord();
    syncShortcutGuide();
    shortcutLastFocused = !overlay.hidden && lastFocused instanceof HTMLElement
      ? lastFocused
      : document.activeElement;
    if (typeof shortcutDialog.showModal === 'function') shortcutDialog.showModal();
    else shortcutDialog.setAttribute('open', '');
    requestAnimationFrame(() => shortcutDialog.querySelector('[data-close-shortcuts]')?.focus({ preventScroll: true }));
  };
  const restoreShortcutFocus = () => {
    if (shortcutLastFocused instanceof HTMLElement && shortcutLastFocused.isConnected) {
      shortcutLastFocused.focus({ preventScroll: true });
    }
    shortcutLastFocused = null;
  };
  const closeShortcutGuide = () => {
    if (typeof shortcutDialog.close === 'function') shortcutDialog.close();
    else {
      shortcutDialog.removeAttribute('open');
      restoreShortcutFocus();
    }
  };
  shortcutDialog.querySelector('[data-close-shortcuts]')?.addEventListener('click', closeShortcutGuide);
  shortcutDialog.addEventListener('close', restoreShortcutFocus);
  shortcutDialog.addEventListener('cancel', event => {
    event.preventDefault();
    closeShortcutGuide();
  });
  shortcutDialog.addEventListener('click', event => {
    if (isDialogBackdropClick(shortcutDialog, event)) closeShortcutGuide();
  });

  let guideReturnFocus = null;
  const restoreGuideFocus = () => {
    if (guideReturnFocus instanceof HTMLElement && guideReturnFocus.isConnected) {
      guideReturnFocus.focus({ preventScroll: true });
    }
    guideReturnFocus = null;
  };
  const openGuide = () => {
    if (guideDialog.open) return;
    hideRouteChord();
    guideReturnFocus = !overlay.hidden && lastFocused instanceof HTMLElement
      ? lastFocused
      : document.activeElement;
    if (typeof guideDialog.showModal === 'function') guideDialog.showModal();
    else guideDialog.setAttribute('open', '');
    requestAnimationFrame(() => guideDialog.querySelector('[data-close-guide]')?.focus({ preventScroll: true }));
  };
  const closeGuide = () => {
    if (typeof guideDialog.close === 'function') guideDialog.close();
    else {
      guideDialog.removeAttribute('open');
      restoreGuideFocus();
    }
  };
  guideDialog.querySelector('[data-close-guide]')?.addEventListener('click', closeGuide);
  guideDialog.addEventListener('close', restoreGuideFocus);
  guideDialog.addEventListener('cancel', event => {
    event.preventDefault();
    closeGuide();
  });
  guideDialog.addEventListener('click', event => {
    if (isDialogBackdropClick(guideDialog, event)) closeGuide();
  });

  const commandSearch = overlay.querySelector('#commandSearch');
  const commandList = overlay.querySelector('#commandList');
  const commandStatus = overlay.querySelector('#commandStatus');
  const commandApiBase = window.location.protocol === 'file:' ? 'http://127.0.0.1:8765' : '';

  const routeItems = [
    { id: 'store', group: '页面导航', kind: 'route', route: 'store', label: 'Replay 商店', description: '查找并导入可靠任务', meta: 'G S', keywords: '商店 store replay 导入', href: '/store' },
    { id: 'community', group: '页面导航', kind: 'route', route: 'community', label: 'Replay 社区', description: '发现真实任务、分享 Replay 与回传复现结果', meta: '', keywords: '社区 分享 发布 复现 规则 举报 community replay', href: '/community' },
    { id: 'setup', group: '页面导航', kind: 'route', route: 'setup', label: '运行准备', description: '配置桌面权限与模型 API', meta: 'G P', keywords: '运行 准备 setup api 密钥 桌面自动化', href: '/setup' },
    { id: 'control', group: '页面导航', kind: 'route', route: 'control', label: '运行状态', description: '查看通过率、异常和最近运行', meta: 'G R', keywords: '运行 状态 control overview', href: '/control' },
    { id: 'studio', group: '页面导航', kind: 'route', route: 'studio', label: '工作台', description: '录制、检查和运行 Replay', meta: 'G W', keywords: '工作台 studio 录制 编辑', href: '/' },
  ];

  function loadReplayCommands({ refresh = false } = {}) {
    if (refresh && replayCommandsLoaded) {
      replayCommandsPromise = null;
      replayCommandsLoaded = false;
      replayCommandsError = false;
    }
    if (replayCommandsPromise) return replayCommandsPromise;
    replayCommandsPromise = GPAClient.requestJson('/api/workflows', {
      baseUrl: commandApiBase,
      timeoutMs: 10000,
    })
      .then(data => {
        replayCommandItems = (data.workflows || []).map(item => ({
          id: `replay-${item.id}`,
          group: 'Replay',
          kind: 'replay',
          label: item.title || item.name || item.id,
          description: `${Number(item.steps || 0)} 步${Number(item.variables || 0) ? ` · ${Number(item.variables)} 个变量` : ' · 无变量'}`,
          meta: `${Number(item.steps || 0)} 步`,
          metaStyle: 'detail',
          keywords: `${item.id || ''} ${item.title || ''} ${item.name || ''} ${item.description || ''} ${item.task_description || ''}`,
          href: `/?workflow_id=${encodeURIComponent(item.id)}`,
        }));
        replayCommandsLoaded = true;
        replayCommandsError = false;
        if (!overlay.hidden) renderCommands();
        return replayCommandItems;
      })
      .catch(() => {
        replayCommandItems = [];
        replayCommandsLoaded = true;
        replayCommandsError = true;
        if (!overlay.hidden) renderCommands();
        return replayCommandItems;
      })
      .finally(() => {
        if (replayCommandsError) replayCommandsPromise = null;
      });
    return replayCommandsPromise;
  }

  function contextualItems() {
    const items = [];
    const search = document.querySelector('#storeSearch, #search');
    if (search) {
      items.push({ id: 'search', group: '当前页面', kind: 'action', mark: '/', label: '搜索当前页面', description: '直接定位到页面内搜索框', meta: '/', keywords: '搜索 查找 filter', action: () => search.focus() });
    }
    const refresh = document.querySelector('#refreshStore, #refresh');
    if (refresh && !refresh.disabled) {
      items.push({ id: 'refresh', group: '当前页面', kind: 'action', mark: '↻', label: '刷新当前数据', description: '重新同步最新状态', meta: '↻', keywords: '刷新 同步 reload', action: () => refresh.click() });
    }
    const importButton = document.querySelector('#openPublisher');
    if (importButton) {
      items.push({ id: 'import', group: '当前页面', kind: 'action', mark: '+', label: '添加 Replay', description: '发布本地流程或导入社区包', meta: '↗', keywords: '添加 导入 发布 上传 package', action: () => importButton.click() });
    }
    const recorder = document.querySelector('#recorderPanel');
    if (recorder) {
      items.push({
        id: 'record',
        group: '当前页面',
        kind: 'action',
        mark: '●',
        label: '录制新 Replay',
        description: '展开录制器并填写任务名称',
        meta: 'R',
        keywords: '录制 新建 replay record',
        action: () => {
          recorder.open = true;
          recorder.querySelector('#recordWorkflowId')?.focus();
        },
      });
    }
    const saveButton = document.querySelector('#saveWorkflow');
    const isPreviewSave = saveButton?.textContent?.includes('保存为 Replay');
    if (saveButton && !saveButton.disabled && (document.body.classList.contains('editor-has-unsaved') || isPreviewSave)) {
      items.push({
        id: 'save-workflow',
        group: '当前页面',
        kind: 'action',
        mark: '✓',
        label: isPreviewSave ? '保存录制预览' : '保存当前 Replay',
        description: isPreviewSave ? '确认并加入 Replay 资料库' : '保存工作台中的未保存修改',
        meta: saveKey,
        keywords: '保存 修改 replay save',
        action: () => saveButton.click(),
      });
    }
    items.push({
      id: 'usage-guide',
      group: '帮助',
      kind: 'action',
      mark: '?',
      label: '打开使用说明',
      description: '了解录制、检查、发布与安全复现',
      meta: 'Guide',
      keywords: '使用说明 guide 帮助 录制 发布 导入 安全复现',
      action: openGuide,
    });
    items.push({
      id: 'keyboard-shortcuts',
      group: '帮助',
      kind: 'action',
      mark: '?',
      label: '查看键盘快捷键',
      description: '查看搜索、保存和列表导航快捷键',
      meta: '?',
      keywords: '快捷键 keyboard shortcuts help 帮助',
      action: openShortcutGuide,
    });
    return items;
  }

  function allItems(query = '') {
    const replayItems = query ? replayCommandItems : replayCommandItems.slice(0, 5);
    return [...contextualItems(), ...routeItems, ...replayItems];
  }

  function renderCommands() {
    const query = commandSearch.value.trim().toLowerCase();
    visibleItems = allItems(query).filter(item => `${item.label} ${item.description} ${item.keywords}`.toLowerCase().includes(query));
    activeIndex = Math.min(activeIndex, Math.max(visibleItems.length - 1, 0));
    commandList.innerHTML = '';
    commandStatus.textContent = replayCommandsError
      ? `${visibleItems.length} 个可用结果 · Replay 暂时无法读取`
      : replayCommandsLoaded
      ? (visibleItems.length ? `${visibleItems.length} 个结果` : '没有结果')
      : `${visibleItems.length} 个结果 · 正在读取 Replay`;
    if (!visibleItems.length) {
      const empty = document.createElement('div');
      empty.className = 'command-empty';
      empty.innerHTML = replayCommandsLoaded
        ? '<strong>没有匹配结果</strong><span>尝试任务标题、页面名称或操作关键词。</span>'
        : '<strong>正在读取 Replay</strong><span>资料库结果会在这里出现。</span>';
      commandList.appendChild(empty);
      commandSearch.removeAttribute('aria-activedescendant');
      return;
    }
    let currentGroup = '';
    visibleItems.forEach((item, index) => {
      if (item.group !== currentGroup) {
        currentGroup = item.group;
        const group = document.createElement('div');
        group.className = 'command-group-label';
        group.textContent = currentGroup;
        commandList.appendChild(group);
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `command-item${index === activeIndex ? ' active' : ''}`;
      button.dataset.commandIndex = String(index);
      button.id = `command-option-${index}`;
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', index === activeIndex ? 'true' : 'false');
      const main = document.createElement('span');
      main.className = 'command-item-main';
      const mark = document.createElement('span');
      mark.className = 'command-item-mark';
      mark.dataset.kind = item.kind || 'action';
      if (item.route) mark.dataset.route = item.route;
      mark.textContent = item.kind === 'replay' ? 'R' : (item.mark || '');
      const copy = document.createElement('span');
      copy.className = 'command-item-copy';
      const label = document.createElement('strong');
      label.textContent = item.label;
      const description = document.createElement('small');
      description.textContent = item.description;
      copy.append(label, description);
      main.append(mark, copy);
      const meta = document.createElement(item.metaStyle === 'detail' ? 'span' : 'kbd');
      if (item.metaStyle === 'detail') meta.className = 'command-item-meta';
      meta.textContent = item.meta || '↵';
      button.append(main, meta);
      button.addEventListener('mouseenter', () => {
        activeIndex = index;
        updateActiveCommand();
      });
      button.addEventListener('click', () => runCommand(index));
      commandList.appendChild(button);
    });
    commandSearch.setAttribute('aria-activedescendant', `command-option-${activeIndex}`);
  }

  function updateActiveCommand() {
    commandList.querySelectorAll('[data-command-index]').forEach((button, index) => {
      const active = index === activeIndex;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    commandSearch.setAttribute('aria-activedescendant', `command-option-${activeIndex}`);
    commandList.querySelector(`[data-command-index="${activeIndex}"]`)?.scrollIntoView({ block: 'nearest' });
  }

  function runCommand(index = activeIndex) {
    const item = visibleItems[index];
    if (!item) return;
    closeCommands({ restoreFocus: false });
    if (item.href) {
      window.location.assign(item.href);
      return;
    }
    item.action?.();
  }

  function openCommands() {
    if (commandCloseTimer) {
      window.clearTimeout(commandCloseTimer);
      commandCloseTimer = null;
    }
    if (!overlay.hidden && overlay.classList.contains('show')) return;
    hideRouteChord();
    if (overlay.hidden) lastFocused = document.activeElement;
    overlay.hidden = false;
    document.body.classList.add('command-open');
    mobileSearchTrigger?.classList.add('active');
    mobileSearchTrigger?.setAttribute('aria-pressed', 'true');
    commandSearch.value = '';
    activeIndex = 0;
    renderCommands();
    loadReplayCommands({ refresh: true });
    requestAnimationFrame(() => {
      overlay.classList.add('show');
      commandSearch.focus();
    });
  }

  function closeCommands({ restoreFocus = true } = {}) {
    if (overlay.hidden) return;
    if (commandCloseTimer) window.clearTimeout(commandCloseTimer);
    overlay.classList.remove('show');
    document.body.classList.remove('command-open');
    mobileSearchTrigger?.classList.remove('active');
    mobileSearchTrigger?.setAttribute('aria-pressed', 'false');
    commandCloseTimer = window.setTimeout(() => {
      commandCloseTimer = null;
      overlay.hidden = true;
      if (restoreFocus && lastFocused instanceof HTMLElement) lastFocused.focus({ preventScroll: true });
    }, 150);
  }

  commandSearch.addEventListener('input', () => {
    activeIndex = 0;
    renderCommands();
  });
  commandSearch.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      activeIndex = (activeIndex + 1) % Math.max(visibleItems.length, 1);
      updateActiveCommand();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      activeIndex = (activeIndex - 1 + Math.max(visibleItems.length, 1)) % Math.max(visibleItems.length, 1);
      updateActiveCommand();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      runCommand();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeCommands();
    }
  });
  overlay.addEventListener('mousedown', event => {
    if (event.target === overlay) closeCommands();
  });
  overlay.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const focusable = [...overlay.querySelectorAll('input, button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])')]
      .filter(item => !item.hidden && item.getClientRects().length);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      const saveButton = document.querySelector('#saveWorkflow');
      if (saveButton && !saveButton.disabled) {
        event.preventDefault();
        if (!overlay.hidden) closeCommands({ restoreFocus: false });
        const isPreviewSave = saveButton.textContent?.includes('保存为 Replay');
        if (document.body.classList.contains('editor-has-unsaved') || isPreviewSave) {
          saveButton.click();
        } else if (typeof globalThis.showToast === 'function') {
          globalThis.showToast('当前 Replay 已保存');
        }
      }
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      overlay.hidden ? openCommands() : closeCommands();
      return;
    }
    const routeKey = event.key.toLowerCase();
    if (routeChordActive) {
      const route = { s: '/store', p: '/setup', r: '/control', w: '/' }[routeKey];
      hideRouteChord();
      if (route) {
        event.preventDefault();
        window.location.assign(route);
        return;
      }
    } else if (
      routeKey === 'g'
      && !event.metaKey && !event.ctrlKey && !event.altKey
      && !isTyping() && overlay.hidden
      && !document.querySelector('dialog[open]')
    ) {
      event.preventDefault();
      startRouteChord();
      return;
    }
    if (event.key === '?' && !isTyping() && overlay.hidden && !shortcutDialog.open) {
      event.preventDefault();
      openShortcutGuide();
      return;
    }
    if (event.key === 'Escape' && overlay.hidden) {
      const openActionMenu = document.querySelector('.editor-action-menu[open]');
      if (openActionMenu) {
        openActionMenu.open = false;
        openActionMenu.querySelector('summary')?.focus();
        return;
      }
    }
    if (event.key === '/' && !isTyping() && overlay.hidden) {
      const search = document.querySelector('#storeSearch, #search');
      if (search) {
        event.preventDefault();
        search.focus();
      }
    }
  });
  document.addEventListener('pointerdown', event => {
    document.querySelectorAll('.editor-action-menu[open]').forEach(menu => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener('click', event => {
    const item = event.target.closest('.editor-action-menu .action-menu-item');
    if (!item) return;
    const menu = item.closest('.editor-action-menu');
    requestAnimationFrame(() => { menu.open = false; });
  });

  const header = document.querySelector('header');
  if (header) {
    const sidebarPreferenceKey = 'gpa.sidebar.collapsed';
    const desktopSidebar = window.matchMedia('(min-width: 961px)');
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'command-trigger';
    trigger.setAttribute('aria-label', `打开全局搜索，快捷键 ${platformKey}`);
    trigger.innerHTML = `<kbd>${platformKey}</kbd><span>全局搜索</span>`;
    trigger.addEventListener('click', openCommands);
    header.insertBefore(trigger, header.querySelector('.topbar'));

    const guideTrigger = document.createElement('button');
    guideTrigger.type = 'button';
    guideTrigger.className = 'guide-trigger';
    guideTrigger.innerHTML = '<span aria-hidden="true">?</span><span>使用说明</span>';
    guideTrigger.setAttribute('aria-haspopup', 'dialog');
    guideTrigger.addEventListener('click', openGuide);
    header.insertBefore(guideTrigger, header.querySelector('.topbar'));

    mobileSearchTrigger = document.createElement('button');
    mobileSearchTrigger.type = 'button';
    mobileSearchTrigger.className = 'mobile-command-trigger ghost';
    mobileSearchTrigger.setAttribute('aria-label', '打开全局搜索');
    mobileSearchTrigger.setAttribute('aria-pressed', 'false');
    mobileSearchTrigger.innerHTML = '<span class="mobile-command-icon" aria-hidden="true"></span><span>搜索</span>';
    mobileSearchTrigger.addEventListener('click', openCommands);
    header.querySelector('.product-nav')?.appendChild(mobileSearchTrigger);

    const sidebarToggle = document.createElement('button');
    const sidebarArrow = document.createElement('span');
    const sidebarLabel = document.createElement('span');
    sidebarToggle.type = 'button';
    sidebarToggle.className = 'sidebar-toggle';
    sidebarArrow.setAttribute('aria-hidden', 'true');
    sidebarToggle.append(sidebarArrow, sidebarLabel);

    const readSidebarPreference = () => {
      try {
        const stored = localStorage.getItem(sidebarPreferenceKey);
        return stored === null ? true : stored === 'true';
      } catch {
        return false;
      }
    };
    const writeSidebarPreference = collapsed => {
      try {
        localStorage.setItem(sidebarPreferenceKey, String(collapsed));
      } catch {
        // Storage can be unavailable in privacy-restricted browser contexts.
      }
    };
    const syncSidebar = () => {
      const collapsed = readSidebarPreference();
      document.body.classList.toggle('sidebar-collapsed', desktopSidebar.matches && collapsed);
      sidebarToggle.setAttribute('aria-pressed', collapsed ? 'true' : 'false');
      sidebarToggle.setAttribute('aria-label', collapsed ? '展开导航' : '收起导航');
      sidebarToggle.title = collapsed ? '展开导航' : '收起导航';
      sidebarArrow.textContent = '';
      sidebarLabel.textContent = collapsed ? '展开导航' : '收起导航';
    };

    sidebarToggle.addEventListener('click', () => {
      writeSidebarPreference(!readSidebarPreference());
      syncSidebar();
    });
    desktopSidebar.addEventListener?.('change', syncSidebar);
    header.insertBefore(sidebarToggle, header.querySelector('.topbar'));
    header.querySelectorAll('.product-nav a').forEach(link => {
      if (!link.title) link.title = link.textContent.trim();
    });
    header.querySelectorAll('.topbar .chip, .topbar .workspace-status').forEach(chip => {
      const syncChipTitle = () => { chip.title = chip.textContent.trim(); };
      new MutationObserver(syncChipTitle).observe(chip, { childList: true, subtree: true, characterData: true });
      syncChipTitle();
    });
    syncSidebar();
  }

  const confirmDialog = document.createElement('dialog');
  confirmDialog.className = 'product-confirm';
  confirmDialog.setAttribute('aria-labelledby', 'productConfirmTitle');
  confirmDialog.setAttribute('aria-describedby', 'productConfirmMessage');
  confirmDialog.innerHTML = `
    <form method="dialog">
      <div class="confirm-mark" aria-hidden="true">!</div>
      <h2 id="productConfirmTitle">确认操作</h2>
      <p id="productConfirmMessage"></p>
      <div class="confirm-actions">
        <button type="button" class="secondary" data-confirm-cancel>取消</button>
        <button type="button" class="danger" data-confirm-accept>确认</button>
      </div>
    </form>`;
  document.body.appendChild(confirmDialog);
  let productDialogQueue = Promise.resolve();

  const enqueueProductDialog = task => {
    const result = productDialogQueue.then(task, task);
    productDialogQueue = result.catch(() => {});
    return result;
  };

  const restoreDialogFocus = target => requestAnimationFrame(() => {
    if (!document.querySelector('.product-confirm[open]') && target instanceof HTMLElement && target.isConnected) {
      target.focus({ preventScroll: true });
    }
  });

  globalThis.productConfirm = (message, options = {}) => enqueueProductDialog(() => {
    hideRouteChord();
    if (typeof confirmDialog.showModal !== 'function') return window.confirm(message);
    const returnFocus = document.activeElement;
    return new Promise(resolve => {
      const messageNode = confirmDialog.querySelector('#productConfirmMessage');
      const accept = confirmDialog.querySelector('[data-confirm-accept]');
      const cancel = confirmDialog.querySelector('[data-confirm-cancel]');
      messageNode.textContent = String(message || '确定继续吗？');
      accept.textContent = options.confirmLabel || '确认';
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        if (confirmDialog.open) confirmDialog.close();
        resolve(value);
        restoreDialogFocus(returnFocus);
      };
      accept.onclick = () => finish(true);
      cancel.onclick = () => finish(false);
      confirmDialog.oncancel = event => {
        event.preventDefault();
        finish(false);
      };
      confirmDialog.showModal();
      cancel.focus();
    });
  });
  confirmDialog.addEventListener('click', event => {
    if (isDialogBackdropClick(confirmDialog, event)) confirmDialog.querySelector('[data-confirm-cancel]')?.click();
  });

  const choiceDialog = document.createElement('dialog');
  choiceDialog.className = 'product-confirm product-choice';
  choiceDialog.setAttribute('aria-labelledby', 'productChoiceTitle');
  choiceDialog.setAttribute('aria-describedby', 'productChoiceMessage');
  choiceDialog.innerHTML = `
    <form method="dialog">
      <div class="confirm-mark" aria-hidden="true">●</div>
      <h2 id="productChoiceTitle">未保存的修改</h2>
      <p id="productChoiceMessage"></p>
      <div class="confirm-actions choice-actions">
        <button type="button" class="secondary" data-choice-cancel>继续编辑</button>
        <button type="button" class="danger" data-choice-discard>放弃修改</button>
        <button type="button" data-choice-save>保存并继续</button>
      </div>
    </form>`;
  document.body.appendChild(choiceDialog);

  globalThis.productChoice = (message, options = {}) => enqueueProductDialog(() => {
    hideRouteChord();
    if (typeof choiceDialog.showModal !== 'function') {
      return window.confirm(message) ? 'discard' : 'cancel';
    }
    const returnFocus = document.activeElement;
    return new Promise(resolve => {
      const title = choiceDialog.querySelector('#productChoiceTitle');
      const messageNode = choiceDialog.querySelector('#productChoiceMessage');
      const save = choiceDialog.querySelector('[data-choice-save]');
      const discard = choiceDialog.querySelector('[data-choice-discard]');
      const cancel = choiceDialog.querySelector('[data-choice-cancel]');
      title.textContent = options.title || '未保存的修改';
      messageNode.textContent = String(message || '请选择如何处理当前修改。');
      save.textContent = options.saveLabel || '保存并继续';
      discard.textContent = options.discardLabel || '放弃修改';
      cancel.textContent = options.cancelLabel || '继续编辑';
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        if (choiceDialog.open) choiceDialog.close();
        resolve(value);
        restoreDialogFocus(returnFocus);
      };
      save.onclick = () => finish('save');
      discard.onclick = () => finish('discard');
      cancel.onclick = () => finish('cancel');
      choiceDialog.oncancel = event => {
        event.preventDefault();
        finish('cancel');
      };
      choiceDialog.showModal();
      cancel.focus();
    });
  });
  choiceDialog.addEventListener('click', event => {
    if (isDialogBackdropClick(choiceDialog, event)) choiceDialog.querySelector('[data-choice-cancel]')?.click();
  });
})();
