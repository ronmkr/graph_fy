  (function() {
    const ARCH_DATA = __DATA_JSON__;

    // Flatten symbols index
    const symbolMap = new Map();
    ARCH_DATA.stages.forEach(st => {
      st.steps.forEach(step => {
        step.symbols.forEach(sym => {
          symbolMap.set(sym.id, sym);
        });
      });
    });

    // Tab Switching
    const tabBtns = document.querySelectorAll('.view-tab-btn');
    const viewPanes = document.querySelectorAll('.view-pane');
    let mermaidRendered = false;

    function renderMermaidIfNeeded() {
      if (mermaidRendered) return;
      const target = document.getElementById('mermaid-target');
      if (!target) return;
      if (typeof mermaid === 'undefined') {
        setTimeout(renderMermaidIfNeeded, 100);
        return;
      }
      try {
        mermaid.run({ nodes: [target] });
        mermaidRendered = true;
      } catch (e) {
        console.warn('Mermaid render warning:', e);
      }
    }

    tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        if (!btn.dataset.view) return;
        tabBtns.forEach(b => b.classList.remove('active'));
        viewPanes.forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const viewId = 'view-' + btn.dataset.view;
        const target = document.getElementById(viewId);
        if (target) target.classList.add('active');
        if (btn.dataset.view === 'mermaid') {
          renderMermaidIfNeeded();
        }
        try {
          if (history.replaceState) {
            const url = new URL(window.location);
            url.searchParams.set('view', btn.dataset.view);
            history.replaceState(null, '', url);
          }
        } catch(e) {}
      });
    });

    // Stage Pill Filtering
    const stagePills = document.querySelectorAll('.stage-pill');
    stagePills.forEach(pill => {
      pill.addEventListener('click', () => {
        stagePills.forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        const filter = pill.dataset.stageFilter;

        const stageSections = document.querySelectorAll('.stage-section');
        stageSections.forEach(sec => {
          if (filter === 'all' || sec.dataset.stage === filter) {
            sec.style.display = 'block';
          } else {
            sec.style.display = 'none';
          }
        });
      });
    });

    // Search Filtering & URL Sync
    const searchInput = document.getElementById('arch-search');
    searchInput.addEventListener('input', (e) => {
      const query = e.target.value.toLowerCase().trim();
      const stepCards = document.querySelectorAll('.step-card');
      const stageSections = document.querySelectorAll('.stage-section');

      try {
        if (history.replaceState) {
          const url = new URL(window.location);
          if (query) url.searchParams.set('search', query);
          else url.searchParams.delete('search');
          history.replaceState(null, '', url);
        }
      } catch(e) {}

      if (!query) {
        stepCards.forEach(c => c.style.display = 'block');
        stageSections.forEach(s => s.style.display = 'block');
        return;
      }

      stageSections.forEach(sec => {
        let hasMatch = false;
        const cards = sec.querySelectorAll('.step-card');
        cards.forEach(card => {
          const text = card.textContent.toLowerCase();
          if (text.includes(query)) {
            card.style.display = 'block';
            hasMatch = true;
          } else {
            card.style.display = 'none';
          }
        });
        sec.style.display = hasMatch ? 'block' : 'none';
      });
    });

    // Initialize from URL deep link
    (function() {
      try {
        const params = new URLSearchParams(window.location.search);
        const q = params.get('search') || params.get('q') || (window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '');
        const view = params.get('view');
        if (view) {
          const btn = document.querySelector(`.view-tab-btn[data-view="${view}"]`);
          if (btn) btn.click();
        }
        if (q && searchInput) {
          searchInput.value = q;
          searchInput.dispatchEvent(new Event('input'));
        }
      } catch(e) {}
    })();

    // Keyboard shortcut to search
    window.addEventListener('keydown', (e) => {
      if (e.key === '/' && document.activeElement !== searchInput) {
        e.preventDefault();
        searchInput.focus();
      }
      if (e.key === 'Escape') {
        closeDrawer();
      }
    });

    // AST Inspector Drawer
    const drawer = document.getElementById('ast-drawer');
    const overlay = document.getElementById('drawer-overlay');
    const closeBtn = document.getElementById('drawer-close-btn');

    function openDrawer(symId) {
      const sym = symbolMap.get(symId);
      if (!sym) return;

      document.getElementById('drawer-title').textContent = sym.label;
      document.getElementById('drawer-kind').textContent = sym.kind;
      document.getElementById('drawer-file').textContent = sym.source_file + (sym.line_number ? ':' + sym.line_number : '');
      document.getElementById('drawer-skeleton').textContent = sym.skeleton || sym.signature || sym.label;

      // Render Callers
      const callersList = document.getElementById('drawer-callers');
      callersList.innerHTML = '';
      if (sym.callers && sym.callers.length > 0) {
        sym.callers.forEach(c => {
          const li = document.createElement('li');
          li.innerHTML = '<span>λ ' + escapeHtml(c.label) + '</span><span style="font-size:11px;color:var(--muted);">' + escapeHtml(c.file) + '</span>';
          callersList.appendChild(li);
        });
      } else {
        callersList.innerHTML = '<li style="color:var(--muted);">No inbound callers recorded</li>';
      }

      // Render Callees
      const calleesList = document.getElementById('drawer-callees');
      calleesList.innerHTML = '';
      if (sym.callees && sym.callees.length > 0) {
        sym.callees.forEach(c => {
          const li = document.createElement('li');
          li.innerHTML = '<span>λ ' + escapeHtml(c.label) + '</span><span style="font-size:11px;color:var(--muted);">' + escapeHtml(c.file) + '</span>';
          calleesList.appendChild(li);
        });
      } else {
        calleesList.innerHTML = '<li style="color:var(--muted);">No outbound calls recorded</li>';
      }

      // Render Sinks
      const sinksDiv = document.getElementById('drawer-sinks');
      if (sym.sinks && sym.sinks.length > 0) {
        sinksDiv.innerHTML = sym.sinks.map(s => '<span class="contract-pill" style="margin-right:6px;color:#f472b6;">' + escapeHtml(s) + '</span>').join('');
      } else {
        sinksDiv.textContent = 'None';
      }

      drawer.classList.add('open');
      overlay.classList.add('open');
    }

    function closeDrawer() {
      drawer.classList.remove('open');
      overlay.classList.remove('open');
    }

    closeBtn.addEventListener('click', closeDrawer);
    overlay.addEventListener('click', closeDrawer);

    // Click on symbol chips
    document.querySelectorAll('.symbol-chip, .matrix-sym-link').forEach(el => {
      el.addEventListener('click', (e) => {
        e.preventDefault();
        const symId = el.dataset.symId;
        if (symId) openDrawer(symId);
      });
    });

    // SVG Node Click Handlers
    document.querySelectorAll('.svg-step-card').forEach(card => {
      card.addEventListener('click', () => {
        const symId = card.dataset.symId;
        if (symId) openDrawer(symId);
      });
    });

    // SVG Pan & Zoom
    const svgCanvas = document.getElementById('arch-svg-canvas');
    let scale = 1.0;
    const zoomInBtn = document.getElementById('zoom-in');
    const zoomOutBtn = document.getElementById('zoom-out');
    const zoomResetBtn = document.getElementById('zoom-reset');

    if (svgCanvas && zoomInBtn) {
      zoomInBtn.addEventListener('click', () => {
        scale = Math.min(2.0, scale + 0.15);
        applySvgScale();
      });
      zoomOutBtn.addEventListener('click', () => {
        scale = Math.max(0.4, scale - 0.15);
        applySvgScale();
      });
      zoomResetBtn.addEventListener('click', () => {
        scale = 1.0;
        applySvgScale();
      });
      function applySvgScale() {
        svgCanvas.style.transform = 'scale(' + scale + ')';
        svgCanvas.style.transformOrigin = '0 0';
      }
    }

    // Flow Animation Controls (Pause / Resume & 1x / 2x Speed)
    const flowToggleBtn = document.getElementById('flow-toggle-btn');
    const flowSpeedBtn = document.getElementById('flow-speed-btn');
    let isFlowPaused = false;
    let isDoubleSpeed = false;

    if (svgCanvas && flowToggleBtn) {
      flowToggleBtn.addEventListener('click', () => {
        isFlowPaused = !isFlowPaused;
        if (isFlowPaused) {
          try { svgCanvas.pauseAnimations(); } catch (e) {}
          svgCanvas.classList.add('flow-paused');
          flowToggleBtn.textContent = '▶ Flow';
          flowToggleBtn.style.color = 'var(--accent)';
        } else {
          try { svgCanvas.unpauseAnimations(); } catch (e) {}
          svgCanvas.classList.remove('flow-paused');
          flowToggleBtn.textContent = '⏸ Flow';
          flowToggleBtn.style.color = '';
        }
      });
    }

    if (svgCanvas && flowSpeedBtn) {
      flowSpeedBtn.addEventListener('click', () => {
        isDoubleSpeed = !isDoubleSpeed;
        const wires = svgCanvas.querySelectorAll('.flow-wire');
        const anims = svgCanvas.querySelectorAll('animateMotion');
        if (isDoubleSpeed) {
          wires.forEach(w => { w.style.animationDuration = '0.7s'; });
          anims.forEach(a => {
            const orig = parseFloat(a.dataset.origDur || a.getAttribute('dur') || '2.4');
            a.dataset.origDur = orig;
            a.setAttribute('dur', (orig / 2) + 's');
          });
          flowSpeedBtn.textContent = '⚡ 2x';
          flowSpeedBtn.style.color = 'var(--accent)';
        } else {
          wires.forEach(w => { w.style.animationDuration = ''; });
          anims.forEach(a => {
            const orig = a.dataset.origDur || '2.4';
            a.setAttribute('dur', orig + 's');
          });
          flowSpeedBtn.textContent = '⚡ 1x';
          flowSpeedBtn.style.color = '';
        }
      });
    }

    function escapeHtml(str) {
      return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // Mermaid Controls: Orientation Toggle, Zoom, Pan & Copy
    const copyMermaidBtn = document.getElementById('copy-mermaid-btn');
    const toggleMermaidRaw = document.getElementById('toggle-mermaid-raw');
    const mermaidRawWrap = document.getElementById('mermaid-raw-wrap');
    const rawMermaidPre = document.getElementById('raw-mermaid-pre');
    const mermaidDirTdBtn = document.getElementById('mermaid-dir-td');
    const mermaidDirLrBtn = document.getElementById('mermaid-dir-lr');
    const mermaidTarget = document.getElementById('mermaid-target');
    const mermaidPzContainer = document.getElementById('mermaid-pan-zoom-container');
    const mermaidRenderedWrap = document.getElementById('mermaid-rendered-wrap');

    const mermaidCodeTD = __MERMAID_CODE_TD__;
    const mermaidCodeLR = __MERMAID_CODE_LR__;
    let currentMermaidDir = 'TD';
    let mermaidScale = 1.0;

    function applyMermaidScale() {
      if (mermaidPzContainer) {
        mermaidPzContainer.style.transform = 'scale(' + mermaidScale + ')';
      }
    }

    const mZoomIn = document.getElementById('mermaid-zoom-in');
    const mZoomOut = document.getElementById('mermaid-zoom-out');
    const mZoomReset = document.getElementById('mermaid-zoom-reset');

    if (mZoomIn) {
      mZoomIn.addEventListener('click', () => {
        mermaidScale = Math.min(2.5, mermaidScale + 0.15);
        applyMermaidScale();
      });
    }
    if (mZoomOut) {
      mZoomOut.addEventListener('click', () => {
        mermaidScale = Math.max(0.3, mermaidScale - 0.15);
        applyMermaidScale();
      });
    }
    if (mZoomReset) {
      mZoomReset.addEventListener('click', () => {
        mermaidScale = 1.0;
        applyMermaidScale();
      });
    }

    // Direction Toggle
    function setMermaidDirection(dir) {
      if (currentMermaidDir === dir) return;
      currentMermaidDir = dir;
      if (dir === 'TD') {
        if (mermaidDirTdBtn) mermaidDirTdBtn.classList.add('active');
        if (mermaidDirLrBtn) mermaidDirLrBtn.classList.remove('active');
        if (mermaidTarget) {
          mermaidTarget.removeAttribute('data-processed');
          mermaidTarget.innerHTML = escapeHtml(mermaidCodeTD);
        }
        if (rawMermaidPre) rawMermaidPre.textContent = mermaidCodeTD;
      } else {
        if (mermaidDirLrBtn) mermaidDirLrBtn.classList.add('active');
        if (mermaidDirTdBtn) mermaidDirTdBtn.classList.remove('active');
        if (mermaidTarget) {
          mermaidTarget.removeAttribute('data-processed');
          mermaidTarget.innerHTML = escapeHtml(mermaidCodeLR);
        }
        if (rawMermaidPre) rawMermaidPre.textContent = mermaidCodeLR;
      }
      mermaidScale = 1.0;
      applyMermaidScale();
      if (typeof mermaid !== 'undefined' && mermaidTarget) {
        try {
          mermaid.run({ nodes: [mermaidTarget] });
        } catch(e) {
          console.warn('Mermaid re-render error:', e);
        }
      }
    }

    if (mermaidDirTdBtn) {
      mermaidDirTdBtn.addEventListener('click', () => setMermaidDirection('TD'));
    }
    if (mermaidDirLrBtn) {
      mermaidDirLrBtn.addEventListener('click', () => setMermaidDirection('LR'));
    }

    if (copyMermaidBtn) {
      copyMermaidBtn.addEventListener('click', () => {
        const textToCopy = currentMermaidDir === 'TD' ? mermaidCodeTD : mermaidCodeLR;
        navigator.clipboard.writeText(textToCopy).then(() => {
          copyMermaidBtn.textContent = 'Copied! ✓';
          setTimeout(() => { copyMermaidBtn.textContent = 'Copy Mermaid 📋'; }, 2000);
        });
      });
    }

    if (toggleMermaidRaw && mermaidRawWrap) {
      toggleMermaidRaw.addEventListener('click', () => {
        const isHidden = mermaidRawWrap.style.display === 'none';
        mermaidRawWrap.style.display = isHidden ? 'block' : 'none';
        toggleMermaidRaw.textContent = isHidden ? 'Hide Raw Syntax' : 'Toggle Raw Syntax';
      });
    }
  })();

if (typeof mermaid !== 'undefined') {
  try {
    mermaid.initialize({
      startOnLoad: false,
      theme: 'dark',
      securityLevel: 'loose',
      flowchart: { htmlLabels: true, useMaxWidth: false }
    });
  } catch (e) {
    console.warn('Mermaid initialization warning:', e);
  }
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('graph_fy_theme', next); } catch(e){}
}
(function() {
  try {
    const saved = localStorage.getItem('graph_fy_theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch(e){}
})();
