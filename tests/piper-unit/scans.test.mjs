import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
const reach = readFileSync(new URL('../piper-e2e/scans/reach.js', import.meta.url), 'utf8');
const clip = readFileSync(new URL('../piper-e2e/scans/clip.js', import.meta.url), 'utf8');
test('clip scan excludes intentionally clipped screen-reader text and still catches tiny visible labels', () => {
  const check = (width, hidden) => runInNewContext(clip, { innerHeight: 800,
    document: { querySelectorAll: () => [{ tagName: 'SPAN', className: 'pl', clientWidth: width, clientHeight: hidden ? 1 : 16,
      scrollWidth: 80, textContent: 'demo/hidden', getBoundingClientRect: () => ({ width, height: hidden ? 1 : 16, top: 10, bottom: 26 }) }] },
    getComputedStyle: () => ({ overflowX: 'hidden', textOverflow: 'ellipsis', display: 'block', position: hidden ? 'absolute' : 'static', clipPath: hidden ? 'inset(50%)' : 'none' })
  });
  assert.equal(check(1, true), 'noclip');
  assert.match(check(5, false), /^SLIVER /);
  assert.match(check(1, false), /^SLIVER /);
});
test('reach scan distinguishes a nested picker from an obstructed control inside it', () => {
  const body = {};
  const node = (parent, name = '') => ({ parentElement: parent, tagName: 'BUTTON', className: name,
    textContent: 'Fixture', getAttribute: () => '',
    getBoundingClientRect: () => ({ left: 10, right: 110, top: 10, bottom: 40, width: 100, height: 30 }),
    closest(selector) { if(selector==='.olayer')return body; return selector.startsWith('.wk-project-pop') ? (this.className === 'wk-project-pop' ? this : this.parentElement?.closest?.(selector)) : null; },
    contains(other) { for (let p = other; p; p = p.parentElement) if (p === this) return true; return false; }
  });
  const popup = node(body, 'wk-project-pop'), cover = node(popup);
  const check = (control, top) => runInNewContext(reach, { innerWidth: 1000, innerHeight: 800,
    document: { body, querySelectorAll: () => [control], elementFromPoint: () => top },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1', overflowX: 'visible', overflowY: 'visible', position: 'static' })
  });
  assert.equal(check(node(body), cover), 'reach-ok', 'an open picker may cover the dialog');
  assert.match(check(node(popup), cover), /^COVERED /, 'a control inside the picker must stay reachable');
  assert.match(check(node(body), node(body)), /^COVERED /, 'ordinary overlapping controls still fail');
});

test('a menu opened from a dialog may cover that dialog, while its own controls remain checked', () => {
  const body = {};
  const node = (parent, kind = '') => ({ parentElement: parent, kind, tagName: 'BUTTON', className: kind,
    textContent: 'Fixture', getAttribute: () => '',
    getBoundingClientRect: () => ({ left:10,right:110,top:10,bottom:40,width:100,height:30 }),
    closest(selector) {
      if (this.kind==='menu' && selector.includes('[role=menu]') || this.kind==='overlay' && selector.includes('#overlay')) return this;
      return this.parentElement?.closest?.(selector) ?? null;
    },
    contains(other) { for(let p=other;p;p=p.parentElement) if(p===this)return true;return false; }
  });
  const overlay=node(body,'overlay'), menu=node(body,'menu'), top=node(menu);
  const check=control=>runInNewContext(reach,{innerWidth:1000,innerHeight:800,
    document:{body,querySelectorAll:()=>[control],elementFromPoint:()=>top},
    getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1',overflowX:'visible',overflowY:'visible',position:'static'})});
  assert.equal(check(node(overlay)),'reach-ok');
  assert.match(check(node(menu)),/^COVERED /);
});

test('mobile inspector bottom sheet may cover the page, but its controls and desktop columns remain checked', () => {
  const body = {};
  const node = (parent, kind = '') => ({ parentElement: parent, kind, id: kind, tagName: 'BUTTON', className: kind,
    textContent: 'Fixture', getAttribute: () => '',
    getBoundingClientRect: () => ({ left: 80, right: 120, top: 600, bottom: 644, width: 40, height: 44 }),
    closest(selector) {
      if (this.kind === 'inspector' && (selector === '#inspector' || selector === '[id]')) return this;
      if (this.kind === 'main' && selector === '#main' || this.kind === 'menu' && selector.includes('[role=menu]')) return this;
      return this.parentElement?.closest?.(selector) ?? null;
    },
    contains(other) { for (let p = other; p; p = p.parentElement) if (p === this) return true; return false; }
  });
  const inspector = node(body, 'inspector'), cover = node(inspector), main = node(body, 'main'), pageControl = node(main);
  const check = (width, control, top = cover) => runInNewContext(reach, { innerWidth: width, innerHeight: 844,
    document: { body, querySelectorAll: () => [control], elementFromPoint: () => top },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1', overflowX: 'visible', overflowY: 'visible', position: 'static' })
  });
  assert.equal(check(390, pageControl), 'reach-ok', 'the designed mobile sheet can cover terminal keys');
  assert.equal(check(640, pageControl), 'reach-ok', 'the sheet includes the 640px breakpoint');
  assert.match(check(390, node(inspector)), /^COVERED /, 'controls in the sheet still must be reachable');
  assert.match(check(390, node(body)), /^COVERED /, 'the sheet must not cover the rail or dialogs');
  assert.match(check(390, node(node(main, 'menu'))), /^COVERED /, 'a menu above the detail must remain reachable');
  assert.match(check(641, pageControl), /^COVERED /, 'the inspector is a separate column above 640px');
  assert.match(check(390, pageControl, node(body)), /^COVERED /, 'ordinary mobile overlaps must still fail');
});
