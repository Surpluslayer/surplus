import React, { useRef, useEffect, useState, useCallback, useMemo } from "react";

const SIDE_COLORS = {
  Builds: { fill: "rgba(47,109,246,0.18)", stroke: "#2f6df6" },
  Hires: { fill: "rgba(63,127,214,0.18)", stroke: "#3f7fd6" },
  Operates: { fill: "rgba(207,95,166,0.18)", stroke: "#cf5fa6" },
};

const NODE_R = 22;

function initials(name) {
  return (name || "?")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

/** One cluster per table/team : members on a ring, groups spread horizontally. */
function buildGroupClusterLayout(nodes, groups, w, h) {
  const padX = 56;
  const padY = 52;
  const nG = Math.max(groups.length, 1);
  const colW = (w - padX * 2) / nG;

  const byGroup = {};
  groups.forEach((g) => { byGroup[g] = []; });
  nodes.forEach((n) => {
    const g = n.grp ?? n.group_id ?? groups[0];
    if (!byGroup[g]) byGroup[g] = [];
    byGroup[g].push(n);
  });

  const anchors = {};
  const groupCenters = {};

  groups.forEach((g, gi) => {
    const members = byGroup[g] || [];
    const cx = padX + colW * (gi + 0.5);
    const cy = h / 2;
    const ringR = Math.max(64, 36 + members.length * 16);

    groupCenters[g] = { x: cx, y: cy, radius: ringR };

    if (members.length === 0) return;

    members.forEach((m, mi) => {
      const ang = -Math.PI / 2 + (mi / members.length) * Math.PI * 2;
      anchors[m.id] = {
        x: cx + Math.cos(ang) * ringR,
        y: cy + Math.sin(ang) * ringR,
        groupId: g,
      };
    });
  });

  const xs = Object.values(anchors).map((a) => a.x);
  const ys = Object.values(anchors).map((a) => a.y);
  const bounds = xs.length
    ? {
        minX: Math.min(...xs) - 80,
        maxX: Math.max(...xs) + 80,
        minY: Math.min(...ys) - 80,
        maxY: Math.max(...ys) + 80,
      }
    : { minX: padX, maxX: w - padX, minY: padY, maxY: h - padY };

  return {
    anchors,
    groupCenters,
    bounds,
    cx: w / 2,
    cy: h / 2,
    R: Math.min(w, h) * 0.4,
  };
}

/** Cut edge clutter: in-group symbiotic links + top cross-group pairs; more on hover/focus. */
function selectVisibleEdges(edges, nodes, hoverId, picked) {
  const focus = new Set(picked);
  if (hoverId != null) focus.add(hoverId);

  if (focus.size > 0) {
    return edges.filter((e) => focus.has(e.a) || focus.has(e.b));
  }

  const withinSym = edges.filter((e) => e.type === "sym" && !e.cross);
  const crossSym = edges
    .filter((e) => e.type === "sym" && e.cross)
    .sort((a, b) => (b.w || 0) - (a.w || 0))
    .slice(0, Math.max(4, Math.min(8, Math.ceil(nodes.length / 2))));

  return [...withinSym, ...crossSym];
}

function fitViewToBounds(bounds, w, h, padding = 40) {
  const bw = bounds.maxX - bounds.minX;
  const bh = bounds.maxY - bounds.minY;
  if (bw <= 0 || bh <= 0) return { pan: { x: 0, y: 0 }, zoom: 1 };

  const zx = (w - padding * 2) / bw;
  const zy = (h - padding * 2) / bh;
  const zoom = Math.min(1.35, Math.max(0.55, Math.min(zx, zy)));
  const pan = {
    x: (w - (bounds.minX + bounds.maxX) * zoom) / 2,
    y: (h - (bounds.minY + bounds.maxY) * zoom) / 2,
  };
  return { pan, zoom };
}

export default function MatchingRadarGraph({
  nodes,
  edges,
  groups,
  groupWord = "Table",
  picked = [],
  onNodeClick,
  loading = false,
  height = 420,
}) {
  const wrapRef = useRef(null);
  const canvasRef = useRef(null);
  const [size, setSize] = useState({ w: 600, h: height });
  const [positions, setPositions] = useState({});
  const [drag, setDrag] = useState(null);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const [hoverId, setHoverId] = useState(null);
  const panDragRef = useRef(null);
  const dragMovedRef = useRef(false);

  const layout = useMemo(
    () => buildGroupClusterLayout(nodes, groups, size.w, size.h),
    [nodes, groups, size.w, size.h]
  );

  const visibleEdges = useMemo(
    () => selectVisibleEdges(edges, nodes, hoverId, picked),
    [edges, nodes, hoverId, picked]
  );

  const nodeKey = useMemo(
    () => nodes.map((n) => n.id).sort().join(","),
    [nodes]
  );

  useEffect(() => {
    const next = {};
    nodes.forEach((n) => {
      const a = layout.anchors[n.id];
      if (a) next[n.id] = { x: a.x, y: a.y };
    });
    setPositions(next);
    const fit = fitViewToBounds(layout.bounds, size.w, size.h);
    setPan(fit.pan);
    setZoom(fit.zoom);
  }, [nodeKey, layout.anchors, layout.bounds, nodes, size.w, size.h]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      const w = Math.max(320, el.clientWidth || 600);
      setSize({ w, h: height });
    };
    measure();
    const ro = new ResizeObserver(() => measure());
    ro.observe(el);
    return () => ro.disconnect();
  }, [height]);

  const getPos = useCallback(
    (id) => {
      const p = positions[id];
      const a = layout.anchors[id];
      if (p) return p;
      if (a) return { x: a.x, y: a.y };
      return { x: layout.cx, y: layout.cy };
    },
    [positions, layout]
  );

  useEffect(() => {
    if (drag || loading || nodes.length === 0) return;
    let raf;
    const tick = () => {
      setPositions((prev) => {
        let moved = false;
        const next = { ...prev };
        nodes.forEach((n) => {
          const a = layout.anchors[n.id];
          const p = next[n.id];
          if (!a || !p) return;
          const dx = a.x - p.x;
          const dy = a.y - p.y;
          if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
            next[n.id] = { x: p.x + dx * 0.1, y: p.y + dy * 0.1 };
            moved = true;
          }
        });
        return moved ? next : prev;
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [drag, loading, nodes, layout.anchors]);

  const screenToWorld = useCallback(
    (clientX, clientY) => {
      const canvas = canvasRef.current;
      if (!canvas) return { x: 0, y: 0 };
      const rect = canvas.getBoundingClientRect();
      const wx = (clientX - rect.left - pan.x) / zoom;
      const wy = (clientY - rect.top - pan.y) / zoom;
      return { x: wx, y: wy };
    },
    [pan, zoom]
  );

  const hitNode = useCallback(
    (wx, wy) => {
      for (let i = nodes.length - 1; i >= 0; i--) {
        const n = nodes[i];
        const p = getPos(n.id);
        const dx = wx - p.x;
        const dy = wy - p.y;
        if (dx * dx + dy * dy <= NODE_R * NODE_R) return n;
      }
      return null;
    },
    [nodes, getPos]
  );

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const { w, h } = size;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    ctx.save();
    ctx.translate(pan.x, pan.y);
    ctx.scale(zoom, zoom);

    const { groupCenters } = layout;

    groups.forEach((g) => {
      const gc = groupCenters[g];
      if (!gc) return;
      ctx.beginPath();
      ctx.arc(gc.x, gc.y, gc.radius + 18, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(108, 67, 217, 0.05)";
      ctx.fill();
      ctx.strokeStyle = "rgba(107, 70, 224, 0.22)";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 5]);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = "#5b616a";
      ctx.font = "700 11px 'Inter', system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillText(`${groupWord} ${g}`, gc.x, gc.y - gc.radius - 24);
    });

    visibleEdges.forEach((e) => {
      const a = getPos(e.a);
      const b = getPos(e.b);
      if (!a || !b) return;
      const isSym = e.type === "sym";
      const hi =
        hoverId === e.a || hoverId === e.b ||
        picked.some((id) => id === e.a || id === e.b);

      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      if (isSym) {
        ctx.strokeStyle = hi ? "rgba(107, 70, 224, 0.85)" : "rgba(107, 70, 224, 0.5)";
        ctx.lineWidth = hi ? 2.5 : 1.75;
      } else {
        ctx.setLineDash([4, 5]);
        ctx.strokeStyle = hi ? "rgba(95, 91, 115, 0.6)" : "rgba(95, 91, 115, 0.25)";
        ctx.lineWidth = 1;
      }
      ctx.stroke();
      ctx.setLineDash([]);
    });

    nodes.forEach((n) => {
      const p = getPos(n.id);
      const colors = SIDE_COLORS[n.side] || SIDE_COLORS.Builds;
      const isPicked = picked.some((id) => id === n.id);
      const isHover = hoverId === n.id;
      const r = isPicked || isHover ? NODE_R + 3 : NODE_R;

      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fillStyle = colors.fill;
      ctx.fill();
      ctx.strokeStyle = isPicked ? "#2f6df6" : colors.stroke;
      ctx.lineWidth = isPicked ? 2.5 : 1.5;
      ctx.stroke();

      ctx.fillStyle = "#1b1e22";
      ctx.font = "700 10px 'Inter', system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(initials(n.name), p.x, p.y);

      ctx.fillStyle = "#5b616a";
      ctx.font = "500 9px 'Inter', system-ui, sans-serif";
      ctx.textBaseline = "top";
      ctx.fillText((n.name || "").split(" ")[0], p.x, p.y + r + 4);
    });

    ctx.restore();

    ctx.fillStyle = "#99a0a8";
    ctx.font = "500 10px 'Inter', system-ui, sans-serif";
    ctx.textAlign = "left";
    const hint = hoverId || picked.length
      ? "Showing links for selected guest(s) · drag to rearrange · scroll to zoom"
      : "Each ring is a group · solid lines = top complementary pairs · hover a guest for their links";
    ctx.fillText(hint, 12, h - 10);
  }, [
    size, pan, zoom, layout, groups, groupWord, visibleEdges, nodes,
    getPos, hoverId, picked,
  ]);

  useEffect(() => {
    draw();
  }, [draw, positions, drag]);

  const onPointerDown = (e) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    canvas.setPointerCapture(e.pointerId);
    const { x, y } = screenToWorld(e.clientX, e.clientY);
    const node = hitNode(x, y);
    dragMovedRef.current = false;
    if (node) {
      setDrag({ id: node.id });
      return;
    }
    panDragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      panX: pan.x,
      panY: pan.y,
    };
  };

  const onPointerMove = (e) => {
    if (panDragRef.current) {
      const d = panDragRef.current;
      const dx = e.clientX - d.startX;
      const dy = e.clientY - d.startY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragMovedRef.current = true;
      setPan({ x: d.panX + dx, y: d.panY + dy });
      return;
    }
    const { x, y } = screenToWorld(e.clientX, e.clientY);
    if (drag) {
      const p = getPos(drag.id);
      if (Math.abs(x - p.x) > 3 || Math.abs(y - p.y) > 3) dragMovedRef.current = true;
      setPositions((prev) => ({
        ...prev,
        [drag.id]: { ...prev[drag.id], x, y },
      }));
      return;
    }
    setHoverId(hitNode(x, y)?.id ?? null);
  };

  const onPointerUp = (e) => {
    const canvas = canvasRef.current;
    if (canvas?.hasPointerCapture(e.pointerId)) {
      canvas.releasePointerCapture(e.pointerId);
    }
    if (drag && !dragMovedRef.current && onNodeClick) {
      onNodeClick(drag.id);
    }
    panDragRef.current = null;
    setDrag(null);
  };

  const onWheel = (e) => {
    e.preventDefault();
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY > 0 ? 0.92 : 1.08;
    const nz = Math.min(2.2, Math.max(0.45, zoom * factor));
    const wx = (mx - pan.x) / zoom;
    const wy = (my - pan.y) / zoom;
    setZoom(nz);
    setPan({ x: mx - wx * nz, y: my - wy * nz });
  };

  const resetView = () => {
    const fit = fitViewToBounds(layout.bounds, size.w, size.h);
    setPan(fit.pan);
    setZoom(fit.zoom);
    const next = {};
    nodes.forEach((n) => {
      const a = layout.anchors[n.id];
      if (a) next[n.id] = { x: a.x, y: a.y };
    });
    setPositions(next);
  };

  return (
    <div className="radar-graph-wrap" ref={wrapRef} style={{ minHeight: height }}>
      <canvas
        ref={canvasRef}
        className="radar-graph-canvas"
        style={{
          touchAction: "none",
          minHeight: height,
          cursor: loading ? "wait" : drag ? "grabbing" : hoverId ? "pointer" : "default",
        }}
        onPointerDown={loading ? undefined : onPointerDown}
        onPointerMove={loading ? undefined : onPointerMove}
        onPointerUp={loading ? undefined : onPointerUp}
        onPointerLeave={loading ? undefined : onPointerUp}
        onWheel={loading ? undefined : onWheel}
      />
      {loading && <div className="radar-graph-loading">Building graph…</div>}
      {!loading && nodes.length === 0 && (
        <div className="radar-graph-empty">No confirmed guests to chart yet.</div>
      )}
      {!loading && (
        <button type="button" className="radar-graph-reset" onClick={resetView}>
          Reset view
        </button>
      )}
    </div>
  );
}