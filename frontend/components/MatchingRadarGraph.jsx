import React, { useRef, useEffect, useState, useCallback, useMemo } from "react";

const SIDE_COLORS = {
  Builds: { fill: "rgba(107,70,224,0.18)", stroke: "#6b46e0" },
  Hires: { fill: "rgba(63,127,214,0.18)", stroke: "#3f7fd6" },
  Operates: { fill: "rgba(207,95,166,0.18)", stroke: "#cf5fa6" },
};

function initials(name) {
  return (name || "?")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

/** Radar anchors: groups on spokes, members on rings within each sector. */
function buildRadarAnchors(nodes, groups, w, h) {
  const cx = w / 2;
  const cy = h / 2;
  const R = Math.min(w, h) * 0.38;
  const groupRing = R * 0.55;
  const memberRing = R * 0.28;
  const nG = Math.max(groups.length, 1);

  const byGroup = {};
  groups.forEach((g) => { byGroup[g] = []; });
  nodes.forEach((n) => {
    const g = n.grp ?? n.group_id ?? 1;
    if (!byGroup[g]) byGroup[g] = [];
    byGroup[g].push(n);
  });

  const anchors = {};
  const groupCenters = {};

  groups.forEach((g, gi) => {
    const mid = -Math.PI / 2 + ((gi + 0.5) / nG) * Math.PI * 2;
    const gcx = cx + Math.cos(mid) * groupRing;
    const gcy = cy + Math.sin(mid) * groupRing;
    groupCenters[g] = { x: gcx, y: gcy, angle: mid };

    const members = byGroup[g] || [];
    members.forEach((m, mi) => {
      const spread = members.length === 1 ? 0 : 0.35;
      const off = members.length === 1
        ? 0
        : (mi / (members.length - 1) - 0.5) * spread;
      const ang = mid + off;
      const r = memberRing * (0.85 + (mi % 3) * 0.08);
      anchors[m.id] = {
        x: gcx + Math.cos(ang) * r,
        y: gcy + Math.sin(ang) * r,
        groupId: g,
      };
    });
  });

  return { anchors, groupCenters, cx, cy, R };
}

export default function MatchingRadarGraph({
  nodes,
  edges,
  groups,
  groupWord = "Table",
  picked = [],
  onNodeClick,
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
    () => buildRadarAnchors(nodes, groups, size.w, size.h),
    [nodes, groups, size.w, size.h]
  );

  // Sync positions when nodes/layout change
  useEffect(() => {
    setPositions((prev) => {
      const next = { ...prev };
      nodes.forEach((n) => {
        const a = layout.anchors[n.id];
        if (!a) return;
        if (next[n.id] == null) {
          next[n.id] = { x: a.x, y: a.y, vx: 0, vy: 0 };
        }
      });
      Object.keys(next).forEach((id) => {
        if (!nodes.find((n) => n.id === Number(id) || String(n.id) === id)) {
          delete next[id];
        }
      });
      return next;
    });
  }, [nodes, layout.anchors]);

  // Resize observer
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect;
      setSize({ w: Math.max(320, width), h: height });
    });
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

  // Gentle spring toward radar anchors when not dragging
  useEffect(() => {
    if (drag) return;
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
          if (Math.abs(dx) > 0.4 || Math.abs(dy) > 0.4) {
            next[n.id] = {
              ...p,
              x: p.x + dx * 0.08,
              y: p.y + dy * 0.08,
            };
            moved = true;
          }
        });
        return moved ? next : prev;
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [drag, nodes, layout.anchors]);

  const screenToWorld = useCallback(
    (clientX, clientY) => {
      const canvas = canvasRef.current;
      if (!canvas) return { x: 0, y: 0 };
      const rect = canvas.getBoundingClientRect();
      const sx = clientX - rect.left;
      const sy = clientY - rect.top;
      const wx = (sx - pan.x) / zoom;
      const wy = (sy - pan.y) / zoom;
      return { x: wx, y: wy };
    },
    [pan, zoom]
  );

  const hitNode = useCallback(
    (wx, wy) => {
      const NODE_R = 22;
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

    const { cx, cy, R, groupCenters } = layout;

    // Radar grid — concentric rings
    ctx.strokeStyle = "rgba(107, 70, 224, 0.12)";
    ctx.lineWidth = 1;
    for (let i = 1; i <= 4; i++) {
      ctx.beginPath();
      ctx.arc(cx, cy, (R * i) / 4, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Spokes per group
    const nG = Math.max(groups.length, 1);
    groups.forEach((g, gi) => {
      const ang = -Math.PI / 2 + ((gi + 0.5) / nG) * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(ang) * R, cy + Math.sin(ang) * R);
      ctx.strokeStyle = "rgba(107, 70, 224, 0.2)";
      ctx.stroke();

      const gc = groupCenters[g];
      if (gc) {
        ctx.beginPath();
        ctx.arc(gc.x, gc.y, 52, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(108, 67, 217, 0.04)";
        ctx.strokeStyle = "rgba(107, 70, 224, 0.25)";
        ctx.setLineDash([4, 4]);
        ctx.fill();
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = "#9b96ac";
        ctx.font = "600 10px 'Plus Jakarta Sans', system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        const lx = gc.x + Math.cos(gc.angle) * 62;
        const ly = gc.y + Math.sin(gc.angle) * 62;
        ctx.fillText(`${groupWord} ${g}`.toUpperCase(), lx, ly);
      }
    });

    // Edges (draw under nodes)
    edges.forEach((e) => {
      const a = getPos(e.a);
      const b = getPos(e.b);
      if (!a || !b) return;
      const isSym = e.type === "sym";
      const dim = e.cross;
      const hi =
        hoverId === e.a || hoverId === e.b ||
        picked.includes(e.a) || picked.includes(e.b);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      if (isSym) {
        ctx.strokeStyle = hi
          ? "rgba(107, 70, 224, 0.75)"
          : dim
            ? "rgba(107, 70, 224, 0.12)"
            : "rgba(107, 70, 224, 0.45)";
        ctx.lineWidth = hi ? 2.5 : 2;
      } else {
        ctx.setLineDash([3, 4]);
        ctx.strokeStyle = hi
          ? "rgba(95, 91, 115, 0.55)"
          : dim
            ? "rgba(95, 91, 115, 0.1)"
            : "rgba(95, 91, 115, 0.3)";
        ctx.lineWidth = 1;
      }
      ctx.stroke();
      ctx.setLineDash([]);
    });

    // Nodes
    nodes.forEach((n) => {
      const p = getPos(n.id);
      const colors = SIDE_COLORS[n.side] || SIDE_COLORS.Builds;
      const isPicked = picked.includes(n.id);
      const isHover = hoverId === n.id;

      ctx.beginPath();
      ctx.arc(p.x, p.y, isPicked || isHover ? 24 : 21, 0, Math.PI * 2);
      ctx.fillStyle = colors.fill;
      ctx.fill();
      ctx.strokeStyle = isPicked ? "#6b46e0" : colors.stroke;
      ctx.lineWidth = isPicked ? 2.5 : 1.5;
      ctx.stroke();

      ctx.fillStyle = "#1f1c2e";
      ctx.font = "700 10px 'Plus Jakarta Sans', system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(initials(n.name), p.x, p.y);

      ctx.fillStyle = "#5f5b73";
      ctx.font = "500 9px 'Plus Jakarta Sans', system-ui, sans-serif";
      ctx.textBaseline = "top";
      const first = (n.name || "").split(" ")[0];
      ctx.fillText(first, p.x, p.y + 26);
    });

    ctx.restore();

    // Hint overlay (screen space)
    ctx.fillStyle = "#9b96ac";
    ctx.font = "500 10px 'Plus Jakarta Sans', system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("Drag guests · drag background to pan · scroll to zoom", 12, h - 10);
  }, [size, pan, zoom, layout, groups, groupWord, edges, nodes, getPos, hoverId, picked]);

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
      setDrag({ id: node.id, ox: x, oy: y });
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
    const nz = Math.min(2.5, Math.max(0.45, zoom * factor));
    const wx = (mx - pan.x) / zoom;
    const wy = (my - pan.y) / zoom;
    setZoom(nz);
    setPan({ x: mx - wx * nz, y: my - wy * nz });
  };

  const resetView = () => {
    setPan({ x: 0, y: 0 });
    setZoom(1);
    setPositions({});
  };

  return (
    <div className="radar-graph-wrap" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        className="radar-graph-canvas"
        style={{ touchAction: "none", cursor: drag ? "grabbing" : hoverId ? "pointer" : "default" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
        onWheel={onWheel}
      />
      <button type="button" className="radar-graph-reset" onClick={resetView}>
        Reset view
      </button>
    </div>
  );
}
