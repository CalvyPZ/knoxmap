/* Live view of the debug profiler. The numbers come from /api/debug/profile. */

(function () {
  const state = { scope: "knox", sort: "cum", q: "", paused: false };
  const status = document.getElementById("status");
  const logPath = document.getElementById("logPath");
  const rows = document.getElementById("rows");
  const slow = document.getElementById("slow");
  const filter = document.getElementById("filter");
  const pause = document.getElementById("pause");
  const scopeKnox = document.getElementById("scopeKnox");
  const scopeAll = document.getElementById("scopeAll");
  let inflight = false;
  let timer = 0;

  function formatCount(n) {
    return Number(n || 0).toLocaleString("en-US");
  }

  function ms(seconds) {
    const value = Number(seconds || 0) * 1000;
    if (value >= 100) {
      return value.toLocaleString("en-US", { maximumFractionDigits: 0 });
    }
    if (value >= 10) {
      return value.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    }
    return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function seconds(value) {
    const n = Number(value || 0);
    if (n >= 10) return n.toFixed(1) + "s";
    return n.toFixed(2) + "s";
  }

  function uptime(value) {
    const whole = Math.max(0, Math.floor(Number(value || 0)));
    const minutes = Math.floor(whole / 60);
    const remain = whole % 60;
    if (minutes <= 0) return whole + "s";
    return minutes + "m " + String(remain).padStart(2, "0") + "s";
  }

  function query() {
    const params = new URLSearchParams({ scope: state.scope, sort: state.sort, limit: "300" });
    if (state.q) params.set("q", state.q);
    return "/api/debug/profile?" + params.toString();
  }

  function cell(className, text) {
    const td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = text;
    return td;
  }

  function paintRows(data) {
    const list = data.rows || [];
    if (!list.length) {
      const tr = document.createElement("tr");
      const td = cell("empty", state.q ? "Nothing matches that filter." : "No function calls yet.");
      td.colSpan = 5;
      tr.appendChild(td);
      rows.replaceChildren(tr);
      return;
    }
    const frag = document.createDocumentFragment();
    list.forEach(function (row) {
      const tr = document.createElement("tr");
      if (!row.knox) tr.className = "lib";
      const calls = row.calls || 0;
      tr.appendChild(cell("num", formatCount(calls)));
      tr.appendChild(cell("num", ms(row.self)));
      tr.appendChild(cell("num", ms(row.cum)));
      tr.appendChild(cell("num", ms(calls ? row.cum / calls : 0)));
      const td = document.createElement("td");
      const name = document.createElement("span");
      name.className = "fn";
      name.textContent = row.func || "";
      const where = document.createElement("span");
      where.className = "where";
      where.textContent = (row.file || "") + ":" + (row.line || "");
      td.title = where.textContent + "  " + name.textContent;
      td.appendChild(name);
      td.appendChild(where);
      tr.appendChild(td);
      frag.appendChild(tr);
    });
    rows.replaceChildren(frag);
  }

  function paintSlow(data) {
    const list = data.slow || [];
    if (!list.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No call has reached " + (data.slowMs || 50) + " ms yet.";
      slow.replaceChildren(li);
      return;
    }
    const frag = document.createDocumentFragment();
    list.forEach(function (row) {
      const li = document.createElement("li");
      const time = document.createElement("span");
      time.textContent = (row.t || "") + "  ";
      const taken = document.createElement("span");
      taken.className = "ms";
      taken.textContent = Number(row.ms || 0).toLocaleString("en-US", {
        minimumFractionDigits: 1, maximumFractionDigits: 1,
      }) + " ms  ";
      const what = document.createElement("span");
      let text = (row.file || "") + ":" + (row.line || "") + "  " + (row.func || "");
      if (row.caller) text += "  <- " + row.caller;
      if (row.pid) text += "  [pid " + row.pid + "]";
      what.textContent = text;
      li.appendChild(time);
      li.appendChild(taken);
      li.appendChild(what);
      frag.appendChild(li);
    });
    slow.replaceChildren(frag);
  }

  function paint(data) {
    if (!data || data.error) {
      status.textContent = data && data.error ? data.error : "The profiler did not answer.";
      return;
    }
    const heading = document.getElementById("slowHeading");
    if (heading && data.slowMs) {
      heading.textContent = "Calls of " + data.slowMs + " ms and longer";
    }
    state.paused = !!data.paused;
    pause.textContent = state.paused ? "Resume" : "Pause";
    pause.setAttribute("aria-pressed", state.paused ? "true" : "false");
    status.classList.toggle("is-paused", state.paused);
    const bits = [
      uptime(data.uptime),
      state.paused ? "paused" : "recording",
      formatCount(data.knoxCalls) + " KnoxMap calls (" + seconds(data.knoxSelfSeconds) + ")",
      formatCount(data.calls) + " total (" + seconds(data.selfSeconds) + ")",
    ];
    if (data.workers) bits.push(data.workers + (data.workers === 1 ? " worker" : " workers"));
    if (data.matched > data.shown) bits.push("showing " + data.shown + " of " + data.matched);
    status.textContent = bits.join(" · ");
    logPath.textContent = data.log || "";
    paintRows(data);
    paintSlow(data);
    document.querySelectorAll("button.sort").forEach(function (button) {
      if (button.getAttribute("data-sort") === state.sort) button.setAttribute("aria-sort", "descending");
      else button.removeAttribute("aria-sort");
    });
  }

  function refresh() {
    if (inflight) return;
    inflight = true;
    fetch(query(), { headers: { "Accept": "application/json" } })
      .then(function (res) {
        if (!res.ok) throw new Error("The profiler answered " + res.status + ".");
        return res.json();
      })
      .then(paint)
      .catch(function (err) {
        status.textContent = err && err.message ? err.message : "The profiler did not answer.";
      })
      .finally(function () { inflight = false; });
  }

  function post(body) {
    return fetch(query(), {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body),
    }).then(function (res) {
      if (!res.ok) throw new Error("The profiler answered " + res.status + ".");
      return res.json();
    }).then(paint);
  }

  function setScope(scope) {
    state.scope = scope;
    scopeKnox.setAttribute("aria-pressed", scope === "knox" ? "true" : "false");
    scopeAll.setAttribute("aria-pressed", scope === "all" ? "true" : "false");
    refresh();
  }

  scopeKnox.addEventListener("click", function () { setScope("knox"); });
  scopeAll.addEventListener("click", function () { setScope("all"); });
  document.querySelectorAll("button.sort").forEach(function (button) {
    button.addEventListener("click", function () {
      state.sort = button.getAttribute("data-sort") || "cum";
      refresh();
    });
  });
  filter.addEventListener("input", function () {
    state.q = filter.value.trim();
    window.clearTimeout(timer);
    timer = window.setTimeout(refresh, 200);
  });
  pause.addEventListener("click", function () {
    post({ paused: !state.paused }).catch(function (err) {
      status.textContent = err && err.message ? err.message : "Could not pause.";
    });
  });
  document.getElementById("clear").addEventListener("click", function () {
    post({ clear: true }).catch(function (err) {
      status.textContent = err && err.message ? err.message : "Could not clear.";
    });
  });
  document.getElementById("openLog").addEventListener("click", function () {
    post({ open: true }).catch(function (err) {
      status.textContent = err && err.message ? err.message : "Could not open the log.";
    });
  });

  refresh();
  window.setInterval(refresh, 1000);
})();
