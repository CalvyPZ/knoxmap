// Polygon clipping for the map.
//
// `difference` cuts one shape out of another, for the eraser.
// `within` reports whether two shapes overlap or come within a distance.
// `unionWithin` joins shapes into one outline, filling a gap no wider than
// that distance so borders that nearly meet become a single selection.
//
// Coordinates are a flat metre grid: [ [ outer, hole, ... ], ... ], each ring
// [ [x, y], ... ]. Rings may be open or closed, and either winding is fine.
// `difference` reports `changed: false` when the clip misses the subject, so
// the eraser can keep a rectangle or a circle.

const knoxClip = (() => {
  const SNAP = 0.01;       // 1 cm; merges vertices the intersection math splits
  const TOL = 0.005;       // 5 mm; "this point is on this edge"
  const PROBE = 0.002;     // 2 mm; a point just inside a traced face
  const MIN_AREA = 1;      // m²; thinner than this is a seam, not a shape

  function snap(p) {
    return [Math.round(p[0] / SNAP) * SNAP, Math.round(p[1] / SNAP) * SNAP];
  }

  function keyOf(p) {
    return Math.round(p[0] / SNAP) + ',' + Math.round(p[1] / SNAP);
  }

  function openRing(ring) {
    const out = [];
    for (const p of ring) {
      const s = snap(p);
      const prev = out[out.length - 1];
      if (prev && prev[0] === s[0] && prev[1] === s[1]) continue;
      out.push(s);
    }
    if (out.length > 1) {
      const a = out[0], b = out[out.length - 1];
      if (a[0] === b[0] && a[1] === b[1]) out.pop();
    }
    return out.length >= 3 ? out : null;
  }

  function cleanPolys(polys) {
    const out = [];
    for (const rings of polys || []) {
      const cleaned = [];
      for (const ring of rings) {
        const open = openRing(ring);
        if (open) cleaned.push(open);
      }
      if (cleaned.length) out.push(cleaned);
    }
    return out;
  }

  function signedArea(ring) {
    let sum = 0;
    for (let i = 0, n = ring.length; i < n; i++) {
      const a = ring[i], b = ring[(i + 1) % n];
      sum += a[0] * b[1] - b[0] * a[1];
    }
    return sum / 2;
  }

  function areaOf(polys) {
    let total = 0;
    for (const rings of polys) {
      total += Math.abs(signedArea(rings[0]));
      for (let i = 1; i < rings.length; i++) total -= Math.abs(signedArea(rings[i]));
    }
    return total;
  }

  function bbox(polys) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const rings of polys) {
      for (const p of rings[0]) {
        if (p[0] < minX) minX = p[0];
        if (p[1] < minY) minY = p[1];
        if (p[0] > maxX) maxX = p[0];
        if (p[1] > maxY) maxY = p[1];
      }
    }
    return { minX, minY, maxX, maxY };
  }

  function bboxHit(a, b) {
    return a.maxX >= b.minX && b.maxX >= a.minX && a.maxY >= b.minY && b.maxY >= a.minY;
  }

  function orient(a, b, c) {
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  }

  function pointOnSeg(a, b, p) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const len = Math.hypot(dx, dy);
    if (len < TOL) return Math.hypot(p[0] - a[0], p[1] - a[1]) <= TOL;
    const cross = Math.abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0]));
    if (cross / len > TOL) return false;
    const dot = (p[0] - a[0]) * dx + (p[1] - a[1]) * dy;
    return dot >= -TOL * len && dot <= len * len + TOL * len;
  }

  // Points that lie on both segments. Endpoints count; the caller drops
  // anything that is already a corner of the segment it would split.
  function hits(a, b, c, d) {
    const lenAB = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const o1 = orient(a, b, c);
    const o2 = orient(a, b, d);
    if (Math.abs(o1) <= TOL * lenAB && Math.abs(o2) <= TOL * lenAB) {
      const pts = [];
      if (pointOnSeg(a, b, c)) pts.push(c);
      if (pointOnSeg(a, b, d)) pts.push(d);
      if (pointOnSeg(c, d, a)) pts.push(a);
      if (pointOnSeg(c, d, b)) pts.push(b);
      return pts;
    }
    const rx = b[0] - a[0], ry = b[1] - a[1];
    const sx = d[0] - c[0], sy = d[1] - c[1];
    const den = rx * sy - ry * sx;
    if (Math.abs(den) < 1e-12) return [];
    const qpx = c[0] - a[0], qpy = c[1] - a[1];
    const t = (qpx * sy - qpy * sx) / den;
    const u = (qpx * ry - qpy * rx) / den;
    if (t < -1e-8 || t > 1 + 1e-8 || u < -1e-8 || u > 1 + 1e-8) return [];
    return [[a[0] + t * rx, a[1] + t * ry]];
  }

  function pointInRing(p, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const a = ring[j], b = ring[i];
      if ((a[1] > p[1]) !== (b[1] > p[1])) {
        const x = (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0];
        if (x > p[0]) inside = !inside;
      }
    }
    return inside;
  }

  function insidePoly(p, rings) {
    if (!pointInRing(p, rings[0])) return false;
    for (let i = 1; i < rings.length; i++) {
      if (pointInRing(p, rings[i])) return false;
    }
    return true;
  }

  function insideMulti(p, polys) {
    for (const rings of polys) {
      if (insidePoly(p, rings)) return true;
    }
    return false;
  }

  function offsetPoint(ring, sign) {
    let best = -1, ax = 0, ay = 0, bx = 0, by = 0;
    for (let i = 0, n = ring.length; i < n; i++) {
      const a = ring[i], b = ring[(i + 1) % n];
      const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (len > best) { best = len; ax = a[0]; ay = a[1]; bx = b[0]; by = b[1]; }
    }
    if (best < 1e-9) return null;
    const nx = -(by - ay) / best, ny = (bx - ax) / best;
    return [(ax + bx) / 2 + sign * nx * PROBE, (ay + by) / 2 + sign * ny * PROBE];
  }

  function closeRing(ring, ccw) {
    const out = ring.map(p => [p[0], p[1]]);
    const positive = signedArea(out) > 0;
    if (positive !== ccw) out.reverse();
    out.push([out[0][0], out[0][1]]);
    return out;
  }

  function sameTopology(subject, result) {
    if (subject.length !== result.length) return false;
    const holes = rings => rings.reduce((n, poly) => n + poly.length - 1, 0);
    return holes(subject) === holes(result);
  }

  function difference(subjectIn, clipIn) {
    const subject = cleanPolys(subjectIn);
    const clip = cleanPolys(clipIn);
    if (!subject.length || !clip.length) return { polygons: subject, changed: false };
    if (!bboxHit(bbox(subject), bbox(clip))) return { polygons: subject, changed: false };

    const verts = [];
    const index = new Map();
    function vid(p) {
      const s = snap(p);
      const k = keyOf(s);
      let id = index.get(k);
      if (id === undefined) {
        id = verts.length;
        verts.push(s);
        index.set(k, id);
      }
      return id;
    }

    const segs = [];
    function addPolyEdges(polys) {
      for (const rings of polys) {
        for (const ring of rings) {
          const ids = [];
          for (const p of ring) {
            const id = vid(p);
            if (!ids.length || ids[ids.length - 1] !== id) ids.push(id);
          }
          if (ids.length > 1 && ids[0] === ids[ids.length - 1]) ids.pop();
          if (ids.length < 3) continue;
          for (let i = 0; i < ids.length; i++) {
            segs.push({ a: ids[i], b: ids[(i + 1) % ids.length], splits: [] });
          }
        }
      }
    }
    addPolyEdges(subject);
    addPolyEdges(clip);
    if (!segs.length) return { polygons: subject, changed: false };

    // Only compare edges that share a grid cell. A town outline is a loop,
    // so each cell holds a handful of edges rather than all of them.
    const box = bbox(subject.concat(clip));
    const span = Math.max(box.maxX - box.minX, box.maxY - box.minY, 1);
    const nCell = Math.min(128, Math.max(8, Math.ceil(Math.sqrt(segs.length))));
    const cell = span / nCell;
    const buckets = new Map();
    segs.forEach((s, i) => {
      const pa = verts[s.a], pb = verts[s.b];
      const x0 = Math.floor((Math.min(pa[0], pb[0]) - box.minX) / cell);
      const x1 = Math.floor((Math.max(pa[0], pb[0]) - box.minX) / cell);
      const y0 = Math.floor((Math.min(pa[1], pb[1]) - box.minY) / cell);
      const y1 = Math.floor((Math.max(pa[1], pb[1]) - box.minY) / cell);
      for (let x = x0; x <= x1; x++) {
        for (let y = y0; y <= y1; y++) {
          const k = x + ',' + y;
          let bucket = buckets.get(k);
          if (!bucket) buckets.set(k, bucket = []);
          bucket.push(i);
        }
      }
    });
    const seen = new Set();
    for (const bucket of buckets.values()) {
      for (let i = 0; i < bucket.length; i++) {
        for (let j = i + 1; j < bucket.length; j++) {
          const a = bucket[i] < bucket[j] ? bucket[i] : bucket[j];
          const b = bucket[i] < bucket[j] ? bucket[j] : bucket[i];
          const k = a + ',' + b;
          if (seen.has(k)) continue;
          seen.add(k);
          const sa = segs[a], sb = segs[b];
          const pts = hits(verts[sa.a], verts[sa.b], verts[sb.a], verts[sb.b]);
          for (const p of pts) {
            const id = vid(p);
            if (id !== sa.a && id !== sa.b) sa.splits.push(id);
            if (id !== sb.a && id !== sb.b) sb.splits.push(id);
          }
        }
      }
    }

    const edgeKey = new Set();
    const half = [];
    function addHalf(from, to) {
      if (from === to) return;
      const lo = from < to ? from : to, hi = from < to ? to : from;
      const undirected = lo + ',' + hi;
      if (!edgeKey.has(undirected)) {
        edgeKey.add(undirected);
        const a = verts[from], b = verts[to];
        half.push({ from, to, angle: Math.atan2(b[1] - a[1], b[0] - a[0]), next: -1, used: false });
        half.push({ from: to, to: from, angle: Math.atan2(a[1] - b[1], a[0] - b[0]), next: -1, used: false });
      }
    }
    for (const s of segs) {
      const pts = s.splits.map(id => {
        const p = verts[id], a = verts[s.a], b = verts[s.b];
        const dx = b[0] - a[0], dy = b[1] - a[1];
        const len2 = dx * dx + dy * dy;
        const t = len2 > 0 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2 : 0;
        return { id, t };
      });
      pts.push({ id: s.a, t: 0 }, { id: s.b, t: 1 });
      pts.sort((p, q) => p.t - q.t || p.id - q.id);
      for (let i = 0; i < pts.length - 1; i++) addHalf(pts[i].id, pts[i + 1].id);
    }
    if (!half.length) return { polygons: subject, changed: false };

    const out = Array.from({ length: verts.length }, () => []);
    half.forEach((h, i) => out[h.from].push(i));
    for (const list of out) list.sort((i, j) => half[i].angle - half[j].angle);

    for (let i = 0; i < half.length; i++) {
      const h = half[i];
      const list = out[h.to];
      let twin = -1;
      for (let k = 0; k < list.length; k++) {
        if (half[list[k]].to === h.from) { twin = k; break; }
      }
      if (twin < 0 || list.length < 2) continue;
      // Clockwise neighbour of the edge we arrived on: the face stays on the left.
      h.next = list[(twin - 1 + list.length) % list.length];
    }

    const faces = [];
    for (let i = 0; i < half.length; i++) {
      if (half[i].used || half[i].next < 0) continue;
      const ring = [];
      let k = i, guard = 0, closed = false;
      do {
        half[k].used = true;
        ring.push(verts[half[k].from]);
        k = half[k].next;
        if (k === i) { closed = true; break; }
      } while (k >= 0 && ++guard <= half.length);
      if (closed && ring.length >= 3) faces.push(ring);
    }
    if (!faces.length) return { polygons: subject, changed: false };

    const shells = [];
    const holes = [];
    for (const ring of faces) {
      const area = signedArea(ring);
      if (Math.abs(area) < MIN_AREA) continue;
      const probe = offsetPoint(ring, 1);
      if (!probe || !insideMulti(probe, subject) || insideMulti(probe, clip)) continue;
      if (area > 0) shells.push({ ring, area });
      else holes.push(ring);
    }

    const polys = shells.map(s => [closeRing(s.ring, true)]);
    for (const ring of holes) {
      if (Math.abs(signedArea(ring)) < MIN_AREA) continue;
      const probe = offsetPoint(ring, -1);
      if (!probe) continue;
      let best = -1, bestArea = Infinity;
      for (let i = 0; i < shells.length; i++) {
        if (shells[i].area < bestArea && pointInRing(probe, shells[i].ring)) {
          best = i;
          bestArea = shells[i].area;
        }
      }
      if (best >= 0) polys[best].push(closeRing(ring, false));
    }

    const before = areaOf(subject);
    const after = areaOf(polys);
    const changed = !polys.length
      ? before >= MIN_AREA
      : Math.abs(before - after) >= MIN_AREA || !sameTopology(subject, polys);
    if (!changed) return { polygons: subject, changed: false };
    return { polygons: polys, changed: true };
  }

  // Every face of the arrangement, as an open ring. Interior stays on the left
  // of the walk, the same rule the eraser uses.
  function traceFaces(allPolys) {
    if (!allPolys.length) return [];

    const verts = [];
    const index = new Map();
    function vid(p) {
      const s = snap(p);
      const k = keyOf(s);
      let id = index.get(k);
      if (id === undefined) {
        id = verts.length;
        verts.push(s);
        index.set(k, id);
      }
      return id;
    }

    const segs = [];
    for (const rings of allPolys) {
      for (const ring of rings) {
        const ids = [];
        for (const p of ring) {
          const id = vid(p);
          if (!ids.length || ids[ids.length - 1] !== id) ids.push(id);
        }
        if (ids.length > 1 && ids[0] === ids[ids.length - 1]) ids.pop();
        if (ids.length < 3) continue;
        for (let i = 0; i < ids.length; i++) {
          segs.push({ a: ids[i], b: ids[(i + 1) % ids.length], splits: [] });
        }
      }
    }
    if (!segs.length) return [];

    const box = bbox(allPolys);
    const span = Math.max(box.maxX - box.minX, box.maxY - box.minY, 1);
    const nCell = Math.min(128, Math.max(8, Math.ceil(Math.sqrt(segs.length))));
    const cell = span / nCell;
    const buckets = new Map();
    segs.forEach((s, i) => {
      const pa = verts[s.a], pb = verts[s.b];
      const x0 = Math.floor((Math.min(pa[0], pb[0]) - box.minX) / cell);
      const x1 = Math.floor((Math.max(pa[0], pb[0]) - box.minX) / cell);
      const y0 = Math.floor((Math.min(pa[1], pb[1]) - box.minY) / cell);
      const y1 = Math.floor((Math.max(pa[1], pb[1]) - box.minY) / cell);
      for (let x = x0; x <= x1; x++) {
        for (let y = y0; y <= y1; y++) {
          const k = x + ',' + y;
          let bucket = buckets.get(k);
          if (!bucket) buckets.set(k, bucket = []);
          bucket.push(i);
        }
      }
    });
    const seen = new Set();
    for (const bucket of buckets.values()) {
      for (let i = 0; i < bucket.length; i++) {
        for (let j = i + 1; j < bucket.length; j++) {
          const a = bucket[i] < bucket[j] ? bucket[i] : bucket[j];
          const b = bucket[i] < bucket[j] ? bucket[j] : bucket[i];
          const k = a + ',' + b;
          if (seen.has(k)) continue;
          seen.add(k);
          const sa = segs[a], sb = segs[b];
          const pts = hits(verts[sa.a], verts[sa.b], verts[sb.a], verts[sb.b]);
          for (const p of pts) {
            const id = vid(p);
            if (id !== sa.a && id !== sa.b) sa.splits.push(id);
            if (id !== sb.a && id !== sb.b) sb.splits.push(id);
          }
        }
      }
    }

    const edgeKey = new Set();
    const half = [];
    function addHalf(from, to) {
      if (from === to) return;
      const lo = from < to ? from : to, hi = from < to ? to : from;
      const undirected = lo + ',' + hi;
      if (!edgeKey.has(undirected)) {
        edgeKey.add(undirected);
        const a = verts[from], b = verts[to];
        half.push({ from, to, angle: Math.atan2(b[1] - a[1], b[0] - a[0]), next: -1, used: false });
        half.push({ from: to, to: from, angle: Math.atan2(a[1] - b[1], a[0] - b[0]), next: -1, used: false });
      }
    }
    for (const s of segs) {
      const pts = s.splits.map(id => {
        const p = verts[id], a = verts[s.a], b = verts[s.b];
        const dx = b[0] - a[0], dy = b[1] - a[1];
        const len2 = dx * dx + dy * dy;
        const t = len2 > 0 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2 : 0;
        return { id, t };
      });
      pts.push({ id: s.a, t: 0 }, { id: s.b, t: 1 });
      pts.sort((p, q) => p.t - q.t || p.id - q.id);
      for (let i = 0; i < pts.length - 1; i++) addHalf(pts[i].id, pts[i + 1].id);
    }
    if (!half.length) return [];

    const out = Array.from({ length: verts.length }, () => []);
    half.forEach((h, i) => out[h.from].push(i));
    for (const list of out) list.sort((i, j) => half[i].angle - half[j].angle);

    for (let i = 0; i < half.length; i++) {
      const h = half[i];
      const list = out[h.to];
      let twin = -1;
      for (let k = 0; k < list.length; k++) {
        if (half[list[k]].to === h.from) { twin = k; break; }
      }
      if (twin < 0 || list.length < 2) continue;
      h.next = list[(twin - 1 + list.length) % list.length];
    }

    const faces = [];
    for (let i = 0; i < half.length; i++) {
      if (half[i].used || half[i].next < 0) continue;
      const ring = [];
      let k = i, guard = 0, closed = false;
      do {
        half[k].used = true;
        ring.push(verts[half[k].from]);
        k = half[k].next;
        if (k === i) { closed = true; break; }
      } while (k >= 0 && ++guard <= half.length);
      if (closed && ring.length >= 3) faces.push(ring);
    }
    return faces;
  }

  function sub(a, b) { return [a[0] - b[0], a[1] - b[1]]; }
  function add(a, b) { return [a[0] + b[0], a[1] + b[1]]; }
  function mul(a, s) { return [a[0] * s, a[1] * s]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1]; }
  function lerp(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]; }
  function vlen(a, b) { return Math.hypot(b[0] - a[0], b[1] - a[1]); }

  function segSegDist(p1, q1, p2, q2) {
    const d1 = sub(q1, p1);
    const d2 = sub(q2, p2);
    const r = sub(p1, p2);
    const aa = dot(d1, d1);
    const ee = dot(d2, d2);
    const f = dot(d2, r);
    let s, t;
    if (aa <= 1e-12 && ee <= 1e-12) {
      s = 0;
      t = 0;
    } else if (aa <= 1e-12) {
      s = 0;
      t = Math.max(0, Math.min(1, f / ee));
    } else {
      const c = dot(d1, r);
      if (ee <= 1e-12) {
        t = 0;
        s = Math.max(0, Math.min(1, -c / aa));
      } else {
        const b = dot(d1, d2);
        const denom = aa * ee - b * b;
        s = denom > 1e-12 ? Math.max(0, Math.min(1, (b * f - c * ee) / denom)) : 0;
        t = (b * s + f) / ee;
        if (t < 0) {
          t = 0;
          s = Math.max(0, Math.min(1, -c / aa));
        } else if (t > 1) {
          t = 1;
          s = Math.max(0, Math.min(1, (b - c) / aa));
        }
      }
    }
    const p = lerp(p1, q1, s);
    const q = lerp(p2, q2, t);
    return { dist: vlen(p, q), sc: s, tc: t, p, q };
  }

  function pointSegDist(p, a, b) {
    return segSegDist(p, p, a, b).dist;
  }

  function walkCells(a, b, cell, visit) {
    let ix = Math.floor(a[0] / cell);
    let iy = Math.floor(a[1] / cell);
    const ix1 = Math.floor(b[0] / cell);
    const iy1 = Math.floor(b[1] / cell);
    visit(ix, iy);
    if (ix === ix1 && iy === iy1) return;
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const stepX = dx > 0 ? 1 : dx < 0 ? -1 : 0;
    const stepY = dy > 0 ? 1 : dy < 0 ? -1 : 0;
    const tDeltaX = stepX === 0 ? Infinity : Math.abs(cell / dx);
    const tDeltaY = stepY === 0 ? Infinity : Math.abs(cell / dy);
    const nextX = (ix + (stepX > 0 ? 1 : 0)) * cell;
    const nextY = (iy + (stepY > 0 ? 1 : 0)) * cell;
    let tMaxX = stepX === 0 ? Infinity : Math.abs((nextX - a[0]) / dx);
    let tMaxY = stepY === 0 ? Infinity : Math.abs((nextY - a[1]) / dy);
    let guard = 0;
    while ((ix !== ix1 || iy !== iy1) && guard++ < 200000) {
      if (tMaxX < tMaxY) {
        ix += stepX;
        tMaxX += tDeltaX;
      } else if (tMaxY < tMaxX) {
        iy += stepY;
        tMaxY += tDeltaY;
      } else {
        ix += stepX;
        iy += stepY;
        tMaxX += tDeltaX;
        tMaxY += tDeltaY;
      }
      visit(ix, iy);
    }
  }

  function hashEdges(polys, gap) {
    const box = bbox(polys);
    const span = Math.max(box.maxX - box.minX, box.maxY - box.minY, gap, 1);
    const cell = Math.max(gap, span / 128);
    const pad = 1;
    const buckets = new Map();
    const edges = [];
    function addBucket(ix, iy, id) {
      const k = ix + ',' + iy;
      let bucket = buckets.get(k);
      if (!bucket) buckets.set(k, bucket = []);
      bucket.push(id);
    }
    for (let pi = 0; pi < polys.length; pi++) {
      for (const ring of polys[pi]) {
        for (let i = 0; i < ring.length; i++) {
          const a = ring[i], b = ring[(i + 1) % ring.length];
          if (vlen(a, b) < 1e-6) continue;
          const id = edges.length;
          edges.push({ a, b, poly: pi });
          walkCells(a, b, cell, (ix, iy) => {
            for (let dx = -pad; dx <= pad; dx++) {
              for (let dy = -pad; dy <= pad; dy++) addBucket(ix + dx, iy + dy, id);
            }
          });
        }
      }
    }
    return { cell, buckets, edges };
  }

  function edgeBoxesNear(a, b, c, d, gap) {
    if (Math.max(a[0], b[0]) + gap < Math.min(c[0], d[0])) return false;
    if (Math.max(c[0], d[0]) + gap < Math.min(a[0], b[0])) return false;
    if (Math.max(a[1], b[1]) + gap < Math.min(c[1], d[1])) return false;
    if (Math.max(c[1], d[1]) + gap < Math.min(a[1], b[1])) return false;
    return true;
  }

  function vertexInside(from, into) {
    for (const rings of from) {
      const p = rings[0] && rings[0][0];
      if (p && insideMulti(p, into)) return true;
    }
    return false;
  }

  // True when the shapes overlap, or any part of one is within `gap` metres
  // of the other. Coordinates are already a flat metre grid.
  function within(aIn, bIn, gap) {
    const a = cleanPolys(aIn), b = cleanPolys(bIn);
    if (!a.length || !b.length) return false;
    const ba = bbox(a), bb = bbox(b);
    if (ba.maxX + gap < bb.minX || bb.maxX + gap < ba.minX ||
        ba.maxY + gap < bb.minY || bb.maxY + gap < ba.minY) return false;
    if (vertexInside(a, b) || vertexInside(b, a)) return true;
    const index = hashEdges(b, gap);
    const seen = new Set();
    for (const rings of a) {
      for (const ring of rings) {
        for (let i = 0; i < ring.length; i++) {
          const p = ring[i], q = ring[(i + 1) % ring.length];
          if (vlen(p, q) < 1e-6) continue;
          let found = false;
          walkCells(p, q, index.cell, (ix, iy) => {
            if (found) return;
            const bucket = index.buckets.get(ix + ',' + iy);
            if (!bucket) return;
            for (const id of bucket) {
              if (seen.has(id)) continue;
              seen.add(id);
              const edge = index.edges[id];
              if (!edgeBoxesNear(p, q, edge.a, edge.b, gap)) continue;
              if (segSegDist(p, q, edge.a, edge.b).dist <= gap) found = true;
            }
          });
          seen.clear();
          if (found) return true;
        }
      }
    }
    return false;
  }

  function expandParam(a, b, c, d, s, dir, gap) {
    const end = dir > 0 ? 1 : 0;
    if (pointSegDist(lerp(a, b, end), c, d) <= gap) return end;
    let lo = s, hi = end;
    for (let i = 0; i < 18; i++) {
      const mid = (lo + hi) / 2;
      if (pointSegDist(lerp(a, b, mid), c, d) <= gap) lo = mid;
      else hi = mid;
    }
    return lo;
  }

  // A strip across a gap no wider than `gap`. The strip is pushed into each
  // shape along the shortest line between them, so the union welds the two
  // instead of leaving the filled gap as its own piece. A point-to-point gap
  // gets a short neck.
  function bridgeRing(a, b, c, d, hit, gap) {
    const sink = 1;
    let s0 = expandParam(a, b, c, d, hit.sc, -1, gap);
    let s1 = expandParam(a, b, c, d, hit.sc, 1, gap);
    let t0 = expandParam(c, d, a, b, hit.tc, -1, gap);
    let t1 = expandParam(c, d, a, b, hit.tc, 1, gap);
    if (s1 < s0) { const swap = s0; s0 = s1; s1 = swap; }
    if (t1 < t0) { const swap = t0; t0 = t1; t1 = swap; }
    const p0 = lerp(a, b, s0), p1 = lerp(a, b, s1);
    const q0 = lerp(c, d, t0), q1 = lerp(c, d, t1);
    const span = Math.max(vlen(p0, p1), vlen(q0, q1));
    const across = sub(hit.p, hit.q);
    const len = Math.hypot(across[0], across[1]) || 1;
    const intoA = mul(across, sink / len);
    const intoB = mul(across, -sink / len);
    if (span >= 0.3) {
      const a0 = add(p0, intoA), a1 = add(p1, intoA);
      const b0 = add(q0, intoB), b1 = add(q1, intoB);
      const ring = vlen(a0, b0) <= vlen(a0, b1) ? [a0, a1, b1, b0] : [a0, a1, b0, b1];
      if (Math.abs(signedArea(ring)) >= 0.05) {
        return { ring, span, p0, p1, q0, q1 };
      }
    }
    const neck = 0.5;
    const extra = sink;
    const v = sub(hit.q, hit.p);
    const nlen = Math.hypot(v[0], v[1]) || 1;
    const n = [-v[1] / nlen, v[0] / nlen];
    const hp = add(hit.p, mul(v, -extra / nlen));
    const hq = add(hit.q, mul(v, extra / nlen));
    const o = mul(n, neck);
    return {
      ring: [add(hp, o), add(hq, o), sub(hq, o), sub(hp, o)],
      span, p0, p1, q0, q1,
    };
  }

  const TOUCH_M = 0.02;

  function spanGaps(polys, gap) {
    if (polys.length < 2) return [];
    const index = hashEdges(polys, gap);
    const found = [];
    const seen = new Set();
    for (let i = 0; i < index.edges.length; i++) {
      const edge = index.edges[i];
      walkCells(edge.a, edge.b, index.cell, (ix, iy) => {
        const bucket = index.buckets.get(ix + ',' + iy);
        if (!bucket) return;
        for (const j of bucket) {
          if (j <= i) continue;
          const other = index.edges[j];
          if (other.poly === edge.poly) continue;
          const pair = i + ',' + j;
          if (seen.has(pair)) continue;
          seen.add(pair);
          if (!edgeBoxesNear(edge.a, edge.b, other.a, other.b, gap)) continue;
          const hit = segSegDist(edge.a, edge.b, other.a, other.b);
          if (hit.dist <= TOUCH_M || hit.dist > gap) continue;
          found.push(bridgeRing(edge.a, edge.b, other.a, other.b, hit, gap));
        }
      });
    }
    // A long shared border also reports its corners as short gaps. Those
    // corners already sit on the long strip, and a neck there bulges outside
    // the outline.
    found.sort((a, b) => b.span - a.span);
    const kept = [];
    for (const cand of found) {
      const covered = kept.some(prev => prev.span > cand.span + 1
        && (pointSegDist(cand.p0, prev.p0, prev.p1) <= gap
          || pointSegDist(cand.q0, prev.q0, prev.q1) <= gap
          || pointSegDist(cand.p0, prev.q0, prev.q1) <= gap
          || pointSegDist(cand.q0, prev.p0, prev.p1) <= gap));
      if (!covered) kept.push(cand);
    }
    return kept.map(cand => [cand.ring]);
  }

  // Cancel edges shared by two kept faces, then walk whatever boundary remains.
  function dissolve(faceRings) {
    const count = new Map();
    function addEdge(a, b) {
      if (a[0] === b[0] && a[1] === b[1]) return;
      const k = keyOf(a) + '|' + keyOf(b);
      const rev = keyOf(b) + '|' + keyOf(a);
      const back = count.get(rev);
      if (back) {
        back.n -= 1;
        if (back.n <= 0) count.delete(rev);
        return;
      }
      const cur = count.get(k);
      if (cur) cur.n += 1;
      else count.set(k, { a, b, n: 1 });
    }
    for (const ring of faceRings) {
      for (let i = 0; i < ring.length; i++) addEdge(ring[i], ring[(i + 1) % ring.length]);
    }
    const from = new Map();
    const edges = [];
    for (const rec of count.values()) {
      const aKey = keyOf(rec.a);
      for (let n = 0; n < rec.n; n++) {
        const edge = {
          a: rec.a, b: rec.b, aKey, bKey: keyOf(rec.b),
          angle: Math.atan2(rec.b[1] - rec.a[1], rec.b[0] - rec.a[0]),
          used: false,
        };
        edges.push(edge);
        let list = from.get(aKey);
        if (!list) from.set(aKey, list = []);
        list.push(edge);
      }
    }
    for (const list of from.values()) list.sort((p, q) => p.angle - q.angle);

    function linkNext(edge) {
      const list = from.get(edge.bKey);
      if (!list || !list.length) return null;
      const back = Math.atan2(edge.a[1] - edge.b[1], edge.a[0] - edge.b[0]);
      let best = null, bestDelta = Infinity;
      for (const cand of list) {
        let delta = back - cand.angle;
        while (delta <= 1e-8) delta += Math.PI * 2;
        if (delta < bestDelta) { bestDelta = delta; best = cand; }
      }
      return best;
    }

    const rings = [];
    for (const start of edges) {
      if (start.used) continue;
      const ring = [];
      let edge = start;
      let closed = false;
      do {
        edge.used = true;
        ring.push(edge.a);
        edge = linkNext(edge);
        if (edge === start) { closed = true; break; }
      } while (edge && !edge.used && ring.length <= edges.length);
      if (closed && ring.length >= 3) rings.push(ring);
    }
    return rings;
  }

  function packPolys(rings) {
    const shells = [];
    const holes = [];
    for (const ring of rings) {
      const area = signedArea(ring);
      if (Math.abs(area) < MIN_AREA) continue;
      if (area > 0) shells.push({ ring, area });
      else holes.push(ring);
    }
    const polys = shells.map(s => [closeRing(s.ring, true)]);
    for (const ring of holes) {
      if (Math.abs(signedArea(ring)) < MIN_AREA) continue;
      const probe = offsetPoint(ring, -1);
      if (!probe) continue;
      let best = -1, bestArea = Infinity;
      for (let i = 0; i < shells.length; i++) {
        if (shells[i].area < bestArea && pointInRing(probe, shells[i].ring)) {
          best = i;
          bestArea = shells[i].area;
        }
      }
      if (best >= 0) polys[best].push(closeRing(ring, false));
    }
    return polys;
  }

  function union(polysIn) {
    const polys = cleanPolys(polysIn);
    if (polys.length <= 1) return polys;
    const faces = traceFaces(polys);
    const inside = [];
    for (const ring of faces) {
      const area = signedArea(ring);
      if (Math.abs(area) < MIN_AREA) continue;
      const probe = offsetPoint(ring, 1);
      if (!probe || !insideMulti(probe, polys)) continue;
      inside.push(ring);
    }
    if (!inside.length) return polys;
    const packed = packPolys(dissolve(inside));
    return packed.length ? packed : polys;
  }

  // Join every polygon in the list. Gaps up to `gap` metres are filled so
  // the result is one outline where the shapes nearly touched.
  function unionWithin(polysIn, gap) {
    const polys = cleanPolys(polysIn);
    if (polys.length <= 1) return polys;
    return union(polys.concat(spanGaps(polys, gap)));
  }

  return { difference, within, unionWithin };
})();
