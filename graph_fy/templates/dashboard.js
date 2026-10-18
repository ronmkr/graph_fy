let isSplit = false;
    const viewSrcs = {
      arch: 'architecture.html',
      graph: 'graph.html',
      callflow: '__CALLFLOW_FILE__',
      tree: 'GRAPH_TREE.html'
    };

    function loadIframe(viewId) {
      const container = document.getElementById('view-' + viewId);
      if (!container) return;
      const iframe = container.querySelector('iframe');
      if (iframe && !iframe.getAttribute('src') && iframe.dataset.src) {
        iframe.setAttribute('src', iframe.dataset.src);
      }
    }

    function switchTab(viewId) {
      if (isSplit) toggleSplit();
      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.view-container').forEach(c => c.classList.remove('active'));
      const activeBtn = document.getElementById('tab-' + viewId);
      if (activeBtn) activeBtn.classList.add('active');
      const target = document.getElementById('view-' + viewId);
      if (target) {
        target.classList.add('active');
        loadIframe(viewId);
        const iframe = target.querySelector('iframe');
        if (iframe && iframe.contentWindow) {
          try { iframe.contentWindow.dispatchEvent(new Event('resize')); } catch(e){}
        }
      }
      const extLink = document.getElementById('extLink');
      if (extLink && viewSrcs[viewId]) {
        extLink.setAttribute('href', viewSrcs[viewId]);
        extLink.setAttribute('title', 'Open ' + viewSrcs[viewId] + ' directly');
      }
    }

    function toggleSplit() {
      isSplit = !isSplit;
      const vp = document.getElementById('mainViewport');
      const splitBtn = document.getElementById('splitBtn');
      if (isSplit) {
        vp.classList.add('grid-layout');
        splitBtn.classList.add('active');
        splitBtn.innerText = '⊟ Single Tab View';
        Object.keys(viewSrcs).forEach(loadIframe);
      } else {
        vp.classList.remove('grid-layout');
        splitBtn.classList.remove('active');
        splitBtn.innerText = '⊞ Split 2×2 View';
        switchTab('arch');
      }
    }
