const initialJsonData = __DATA_JSON__;

    function transformData(jsonData) {
        // Helper function to recursively build the children structure
        function processNode(node, parentL1StageName) {
            let displayName = node.name;
            // Append total_count if it exists and is not already in the name
            if (node.total_count !== undefined) {
                if (!/\(Total Count: \d+\)$/.test(displayName)) {
                    displayName += ` (Total Count: ${node.total_count})`;
                }
            }

            const newNode = { name: displayName };

            if (parentL1StageName === "Root") {
                 newNode.originalStageName = node.name;
            } else {
                newNode.originalStageName = parentL1StageName;
            }

            if (node.children && node.children.length > 0) {
                const stageNameToPass = (parentL1StageName === "Root") ? node.name : parentL1StageName;
                newNode.children = node.children.map(child => processNode(child, stageNameToPass));
            }

            return newNode;
        }

        let rootDisplayName = jsonData.name;
        if (jsonData.total_count !== undefined && !/\(Total Count: \d+\)$/.test(rootDisplayName)) {
            rootDisplayName += ` (Total Count: ${jsonData.total_count})`;
        }

        return {
            name: rootDisplayName,
            originalStageName: "Root",
            children: (jsonData.children || []).map(child => processNode(child, "Root"))
        };
    }

    const treeData = transformData(initialJsonData);

    // Auto-populated phaseColors: every depth-1 child of the root gets
    // a stable colour from a bigger palette so all top-level dirs are
    // distinguishable.
    const PALETTE = [
      ["#1a73e8","#174ea6","#d2e3fc"],
      ["#1e8e3e","#137333","#ceead6"],
      ["#d93025","#a50e0e","#fad2cf"],
      ["#f9ab00","#b06000","#feefc3"],
      ["#9334e6","#681da8","#f3e8fd"],
      ["#0097a7","#006064","#b2ebf2"],
      ["#e8710a","#b34700","#fedfc8"],
      ["#e52592","#9c166f","#fce4ec"],
      ["#3f51b5","#283593","#c5cae9"],
      ["#00acc1","#00838f","#e0f7fa"],
      ["#5f6368","#3c4043","#dadce0"],
      ["#c2185b","#880e4f","#f8bbd0"],
      ["#1565c0","#0d47a1","#bbdefb"],
      ["#2e7d32","#1b5e20","#c8e6c9"],
      ["#7b1fa2","#4a148c","#e1bee7"],
    ];
    const phaseColors = { "Root": { fill: "#1a73e8", stroke: "#174ea6", collapsedFill: "#d2e3fc" },
                          "Default": { fill: "#bdc1c6", stroke: "#9aa0a6", collapsedFill: "#e8eaed" } };
    (initialJsonData.children || []).forEach((c, i) => {
      const pal = PALETTE[i % PALETTE.length];
      phaseColors[c.name] = { fill: pal[0], stroke: pal[1], collapsedFill: pal[2] };
    });

    const getPalette = depth => {
      const pal = PALETTE[depth % PALETTE.length];
      return { fill: pal[0], stroke: pal[1], collapsedFill: pal[2] };
    };

    const svgElement = d3.select("#tree-svg");
    const initialSvgWidth = +svgElement.attr("width");
    const initialSvgHeight = +svgElement.attr("height");
    const margin = { top: 40, right: 120, bottom: 80, left: 450 };
    let width = initialSvgWidth - margin.left - margin.right;
    let height = initialSvgHeight - margin.top - margin.bottom;
    const duration = 500;
    let nodeCounter = 0;
    const g = svgElement.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
    const treemap = d3.tree().nodeSize([40, 0]);
    let rootNode = d3.hierarchy(treeData, d => d.children);
    rootNode.x0 = 0;
    rootNode.y0 = 0;

    if (rootNode.children) {
      rootNode.children.forEach(d_child => {
        if (d_child.children) { collapseBranch(d_child); }
      });
    }
    updateTree(rootNode);

    function collapseBranch(d) { if (d.children) { d._children = d.children; d._children.forEach(collapseBranch); d.children = null; } }
    function expandBranch(d) { if (d._children) { d.children = d._children; d._children = null; } if (d.children) { d.children.forEach(expandBranch); } }
    window.expandAll = () => { expandBranch(rootNode); updateTree(rootNode); };
    window.collapseAll = () => { if (rootNode.children) { rootNode.children.forEach(collapseBranch); } updateTree(rootNode); };
    window.resetView = () => { if (rootNode.children) { rootNode.children.forEach(d_child => { if (d_child.children || d_child._children) { collapseBranch(d_child); } }); } if (rootNode._children && !rootNode.children) { rootNode.children = rootNode._children; rootNode._children = null; } updateTree(rootNode); };

    window.filterTree = (query) => {
      const q = (query || '').toLowerCase().trim();
      try {
        if (history.replaceState) {
          const url = new URL(window.location);
          if (q) url.searchParams.set('search', q);
          else url.searchParams.delete('search');
          history.replaceState(null, '', url);
        }
      } catch(e) {}
      if (!q) {
        window.resetView();
        return;
      }
      function searchAndExpand(d) {
        let hasMatch = (d.data.name || '').toLowerCase().includes(q);
        const children = d.children || d._children || [];
        for (const child of children) {
          if (searchAndExpand(child)) hasMatch = true;
        }
        if (hasMatch && d._children) {
          d.children = d._children;
          d._children = null;
        }
        return hasMatch;
      }
      searchAndExpand(rootNode);
      updateTree(rootNode);
    };

    // Initialize from URL deep link
    (function() {
      try {
        const params = new URLSearchParams(window.location.search);
        const q = params.get('search') || params.get('q') || (window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '');
        const searchInput = document.getElementById('tree-search');
        if (q && searchInput) {
          searchInput.value = q;
          window.filterTree(q);
        }
      } catch(e) {}
    })();

    function updateTree(source) {
      const treeLayoutData = treemap(rootNode);
      let nodes = treeLayoutData.descendants();
      let links = treeLayoutData.descendants().slice(1);

      let minX = 0;
      let maxX = 0;
      if (nodes.length > 0) {
        minX = d3.min(nodes, d => d.x);
        maxX = d3.max(nodes, d => d.x);
      }

      let neededHeight = Math.max(initialSvgHeight, maxX - minX + margin.top + margin.bottom + 100);
      svgElement.transition().duration(duration / 2).attr("height", neededHeight);
      g.transition().duration(duration / 2).attr("transform", `translate(${margin.left},${margin.top - minX + 40})`);

      nodes.forEach(d => { d.y = d.depth * 400; }); // Adjust horizontal separation if needed

      const node = g.selectAll('g.node').data(nodes, d => d.id || (d.id = ++nodeCounter));
      const nodeEnter = node.enter().append('g')
        .attr('class', d => "node" + (d.children || d._children ? " node--internal" : " node--leaf") + (d._children ? " _children" : ""))
        .attr('transform', d => `translate(${source.y0},${source.x0})`)
        .on('click', (event, d) => { if (d.children) { d._children = d.children; d.children = null; } else if (d._children) { d.children = d._children; d._children = null; } updateTree(d); })
        .style('cursor', d => (d.children || d._children) ? 'pointer' : 'default');

      nodeEnter.append('circle').attr('r', 1e-6);

      nodeEnter.append('text')
        .attr('dy', '.35em')
        .attr('x', d => d.children || d._children ? -14 : 14)
        .attr('text-anchor', d => d.children || d._children ? 'end' : 'start')
        .style("fill-opacity", 1e-6)
        .call(wrapText, 380);

      const nodeUpdate = nodeEnter.merge(node);
      nodeUpdate.transition().duration(duration)
        .attr('transform', d => `translate(${d.y},${d.x})`)
        .attr('class', d => "node" + (d.children ? " node--internal" : " node--leaf") + (d._children ? " node--internal _children" : ""));

      nodeUpdate.select('circle').attr('r', 8.5)
        .style('fill', d => {
            const palette = (d.depth === 1) ? (phaseColors[d.data.originalStageName] || phaseColors.Default) : getPalette(d.depth);
            if (d._children) return palette.collapsedFill;
            if (d.children) return palette.fill;
            return "#ffffff";
        })
        .style('stroke', d => {
            const palette = (d.depth === 1) ? (phaseColors[d.data.originalStageName] || phaseColors.Default) : getPalette(d.depth);
            return palette.stroke;
        });
      nodeUpdate.select('text').style("fill-opacity", 1).call(wrapText, 380);

      const nodeExit = node.exit().transition().duration(duration).attr('transform', d => `translate(${source.y},${source.x})`).remove();
      nodeExit.select('circle').attr('r', 1e-6);
      nodeExit.select('text').style('fill-opacity', 1e-6);

      const link = g.selectAll('path.link').data(links, d => d.id);
      const linkEnter = link.enter().insert('path', "g").attr('class', 'link').attr('d', d => { const o = { x: source.x0, y: source.y0 }; return diagonal(o, o); });

      linkEnter.merge(link).transition().duration(duration).attr('d', d => diagonal(d, d.parent))
        .style('stroke', d => {
            const sourceNode = d.parent;
            if (!sourceNode) return phaseColors.Default.stroke;
            const l1AncestorName = sourceNode.data.originalStageName;
            const colorPalette = phaseColors[l1AncestorName] || phaseColors.Default;
            return colorPalette.stroke;
        });
      link.exit().transition().duration(duration).attr('d', d => { const o = { x: source.x, y: source.y }; return diagonal(o, o); }).remove();
      nodes.forEach(d => { d.x0 = d.x; d.y0 = d.y; });
    }

    function diagonal(s, d) { return `M ${s.y} ${s.x} C ${(s.y + d.y) / 2} ${s.x}, ${(s.y + d.y) / 2} ${d.x}, ${d.y} ${d.x}`; }

    function wrapText(textElements, maxWidth) {
        const textPartColors = {
            name: 'currentColor',
            count: 'var(--accent)'
        };
        const countRegex = /(\s\(Total Count: \d+\))$/;

        textElements.each(function () {
            const textD3 = d3.select(this);
            const originalNodeText = textD3.datum().data.name;
            const x = parseFloat(textD3.attr("x") || 0);
            const initialDy = textD3.attr("dy");
            const textAnchor = textD3.attr("text-anchor");
            const lineHeight = 1.1;

            textD3.text(null);

            let namePart = originalNodeText;
            let countPartText = "";

            const countMatch = originalNodeText.match(countRegex);
            if (countMatch && originalNodeText.endsWith(countMatch[0])) {
                namePart = originalNodeText.substring(0, originalNodeText.length - countMatch[0].length).trim();
                countPartText = countMatch[0].trim();
            }

            const tokens = [];
            namePart.split(/\s+/).filter(Boolean).forEach(word => {
                tokens.push({ text: word, type: 'name' });
            });
            if (countPartText) {
                tokens.push({ text: countPartText, type: 'count' });
            }

            if (tokens.length === 0 && originalNodeText) {
                tokens.push({ text: originalNodeText, type: 'name' });
            }

            let currentTspan = textD3.append("tspan").attr("x", x).attr("dy", initialDy);
            if (textAnchor === "end") currentTspan.attr("text-anchor", "end");

            let lineTokens = [];

            for (let i = 0; i < tokens.length; i++) {
                const tokenObj = tokens[i];

                lineTokens.push(tokenObj);
                currentTspan.text(lineTokens.map(t => t.text).join(" "));

                if (currentTspan.node().getComputedTextLength() > maxWidth && lineTokens.length > 1) {
                    lineTokens.pop();

                    currentTspan.text(null);
                    lineTokens.forEach((prevToken, idx) => {
                        currentTspan.append("tspan")
                            .text((idx > 0 ? " " : "") + prevToken.text)
                            .style("fill", textPartColors[prevToken.type] || textPartColors.name)
                            .style("font-weight", prevToken.type === 'count' ? "bold" : "normal");
                    });

                    lineTokens = [tokenObj];
                    currentTspan = textD3.append("tspan").attr("x", x).attr("dy", lineHeight + "em");
                    if (textAnchor === "end") currentTspan.attr("text-anchor", "end");
                }
            }

            currentTspan.text(null);
            lineTokens.forEach((token, idx) => {
                currentTspan.append("tspan")
                    .text((idx > 0 ? " " : "") + token.text)
                    .style("fill", textPartColors[token.type] || textPartColors.name)
                    .style("font-weight", token.type === 'count' ? "bold" : "normal");
            });

            if (textD3.selectAll("tspan > tspan").empty() && textD3.select("tspan").text().length === 0 && originalNodeText) {
                let t = textD3.select("tspan");
                let displayText = originalNodeText;
                t.text(displayText).style("fill", textPartColors.name);
                if (t.node() && t.node().getComputedTextLength() > maxWidth && displayText.length > 20) {
                    let estimatedChars = Math.floor(maxWidth / (t.node().getComputedTextLength()/displayText.length) );
                    displayText = displayText.substring(0, Math.max(0, estimatedChars - 3)) + "...";
                    t.text(displayText);
                }
            }
        });
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
