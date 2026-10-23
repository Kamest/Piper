// errors.js - page init script (agent-browser --init-script): records JS errors, unhandled rejections, console.error/warn, fetch/XHR answers >= 400 or failed, and failed resource loads; evaluated again it returns {errors, inflight} as JSON and clears the list ('installed' = the init script did not run)
(() => {
  const S = window.__piperScan;
  if (S) return JSON.stringify({ errors: S.errors.splice(0), inflight: S.inflight });
  const st = window.__piperScan = { errors: [], inflight: 0 };
  const add = (kind, msg) => { if (st.errors.length < 200) st.errors.push(kind + ' ' + String(msg).slice(0, 300)); };
  const text = (x) => { if (x instanceof Error) return x.message; if (typeof x === 'string') return x; try { return JSON.stringify(x); } catch (e) { return String(x); } };
  const path = (u) => { try { const x = new URL(u, location.href); return x.origin === location.origin ? x.pathname + x.search : x.href; } catch (e) { return String(u); } };
  addEventListener('error', (e) => {
    const t = e.target;
    if (t && t !== window && (t.src || t.href)) add('RESOURCE', path(t.src || t.href));
    else add('JS', (e.message || '') + ' @' + path(e.filename || '') + ':' + (e.lineno || ''));
  }, true);
  addEventListener('unhandledrejection', (e) => add('REJECTION', e.reason && (e.reason.stack || e.reason.message) || e.reason));
  for (const lvl of ['error', 'warn']) {
    const orig = console[lvl].bind(console);
    console[lvl] = (...a) => { add('CONSOLE.' + lvl, a.map(text).join(' ')); orig(...a); };
  }
  const ofetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = path(typeof input === 'string' || input instanceof URL ? String(input) : input.url);
    const m = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    st.inflight++;
    try {
      const r = await ofetch.apply(this, arguments);
      if (r.status >= 400) add('HTTP ' + r.status, m + ' ' + url);
      return r;
    } catch (e) {
      if (!(e && e.name === 'AbortError')) add('FETCH-FAILED', m + ' ' + url + ' ' + text(e));
      throw e;
    } finally { st.inflight--; }
  };
  const oopen = XMLHttpRequest.prototype.open, osend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__pe = String(m).toUpperCase() + ' ' + path(u); return oopen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    st.inflight++;
    this.addEventListener('abort', () => { this.__peAbort = true; });
    this.addEventListener('loadend', () => {
      st.inflight--;
      if (this.status >= 400) add('HTTP ' + this.status, this.__pe);
      else if (this.status === 0 && !this.__peAbort) add('XHR-FAILED', this.__pe);
    });
    return osend.apply(this, arguments);
  };
  return 'installed';
})()
