// Difference of two polygons, for the map's eraser.
//
// Both arguments are MultiPolygon coordinates in a flat metre grid:
// [ [ outer, hole, ... ], ... ], each ring [ [x, y], ... ].
// The result is the subject with the clip cut out, as the same kind of
// coordinates. Rings may be open or closed, and either winding is fine.
//
// `changed` is false when the clip misses the subject, so the caller can
// keep a rectangle or a circle instead of replacing it with a polygon.

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

  return { difference };
})();
