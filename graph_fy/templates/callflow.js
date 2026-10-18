(function () {
  const mermaidConfig = {
    startOnLoad: false,
    theme: 'base',
    securityLevel: 'loose',
    flowchart: { htmlLabels: true, useMaxWidth: true },
    themeVariables: {
      primaryColor: '#e8f0fe',
      primaryTextColor: '#0b57d0',
      primaryBorderColor: '#1a73e8',
      secondaryColor: '#f8f9fa',
      tertiaryColor: '#ffffff',
      lineColor: '#5f6368',
      textColor: '#202124',
      edgeLabelBackground: '#ffffff',
      nodeBorder: '#1a73e8',
      clusterBkg: '#f8f9fa',
      clusterBorder: '#dadce0',
      fontFamily: 'Google Sans, Roboto, sans-serif'
    }
  };

  mermaid.initialize(mermaidConfig);

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function enhanceMermaidDiagrams() {
    document.querySelectorAll('.mermaid').forEach((container) => {
      if (container.dataset.zoomReady === 'true') return;
      const svg = container.querySelector('svg');
      if (!svg) return;

      container.dataset.zoomReady = 'true';
      container.classList.add('is-enhanced');

      const viewport = document.createElement('div');
      viewport.className = 'mermaid-viewport';
      svg.parentNode.insertBefore(viewport, svg);
      viewport.appendChild(svg);

      const toolbar = document.createElement('div');
      toolbar.className = 'mermaid-toolbar';
      toolbar.innerHTML = [
        '<button type="button" data-action="zoom-out" title="Zoom out">-</button>',
        '<span class="zoom-level" data-role="level">100%</span>',
        '<button type="button" data-action="zoom-in" title="Zoom in">+</button>',
        '<button type="button" data-action="fit" title="Fit width">Fit</button>',
        '<button type="button" data-action="reset" title="Reset view">Reset</button>'
      ].join('');
      container.insertBefore(toolbar, viewport);

      const state = { scale: 1, x: 0, y: 0, dragging: false, startX: 0, startY: 0, originX: 0, originY: 0 };
      const level = toolbar.querySelector('[data-role="level"]');

      function applyTransform() {
        svg.style.transform = `translate(${state.x}px, ${state.y}px) scale(${state.scale})`;
        level.textContent = `${Math.round(state.scale * 100)}%`;
      }

      function zoomBy(delta) {
        state.scale = clamp(Math.round((state.scale + delta) * 100) / 100, 0.4, 3);
        applyTransform();
      }

      function reset() {
        state.scale = 1;
        state.x = 0;
        state.y = 0;
        applyTransform();
      }

      function fitWidth() {
        reset();
      }

      toolbar.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;
        const action = button.dataset.action;
        if (action === 'zoom-in') zoomBy(0.15);
        if (action === 'zoom-out') zoomBy(-0.15);
        if (action === 'fit') fitWidth();
        if (action === 'reset') reset();
      });

      viewport.addEventListener('wheel', (event) => {
        if (!event.ctrlKey && !event.metaKey) return;
        event.preventDefault();
        zoomBy(event.deltaY < 0 ? 0.1 : -0.1);
      }, { passive: false });

      viewport.addEventListener('pointerdown', (event) => {
        if (event.button !== 0) return;
        state.dragging = true;
        state.startX = event.clientX;
        state.startY = event.clientY;
        state.originX = state.x;
        state.originY = state.y;
        viewport.classList.add('is-dragging');
        viewport.setPointerCapture(event.pointerId);
      });

      viewport.addEventListener('pointermove', (event) => {
        if (!state.dragging) return;
        state.x = state.originX + event.clientX - state.startX;
        state.y = state.originY + event.clientY - state.startY;
        applyTransform();
      });

      function endDrag(event) {
        if (!state.dragging) return;
        state.dragging = false;
        viewport.classList.remove('is-dragging');
        if (viewport.hasPointerCapture(event.pointerId)) {
          viewport.releasePointerCapture(event.pointerId);
        }
      }

      viewport.addEventListener('pointerup', endDrag);
      viewport.addEventListener('pointercancel', endDrag);
      applyTransform();
    });
  }

  function renderMermaid() {
    const result = mermaid.run
      ? mermaid.run({ querySelector: '.mermaid' })
      : Promise.resolve();
    Promise.resolve(result)
      .then(enhanceMermaidDiagrams)
      .catch((error) => {
        console.error('Mermaid render failed:', error);
        enhanceMermaidDiagrams();
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderMermaid);
  } else {
    renderMermaid();
  }
})();

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
