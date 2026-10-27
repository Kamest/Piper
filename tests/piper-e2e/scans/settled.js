// Wait for fetches, finite animations and two mutation-free rendering frames.
// Infinite spinners/cursors must not prevent settling. A stuck UI fails explicitly.
(() => new Promise((resolve, reject) => {
  const state = window.__piperScan;
  if (!state) { reject(new Error('GUI error collector is not installed')); return; }
  let revision = 0, seen = -1, quiet = 0, frame, frames = 0;
  const animations = () => document.getAnimations().filter(a =>
    a.playState === 'running' && Number.isFinite(a.effect?.getComputedTiming().endTime));
  const observer = new MutationObserver(() => { revision++; });
  observer.observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
  const finish = (error) => {
    observer.disconnect();
    cancelAnimationFrame(frame);
    clearTimeout(timer);
    if (error) reject(error); else resolve(true);
  };
  const timer = setTimeout(() => finish(new Error('GUI did not settle within 4 seconds: ' + JSON.stringify({
    inflight: state.inflight, ready: document.readyState, animations: animations().length,
    mutations: revision, frames,
  }))), 4000);
  const tick = () => {
    frames++;
    const animating = animations().length > 0;
    quiet = !state.inflight && document.readyState === 'complete' && !animating && seen === revision ? quiet + 1 : 0;
    seen = revision;
    if (quiet >= 2) finish();
    else frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
}))()
