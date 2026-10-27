// reach.js - visible controls a user cannot reach: clipped by a non-scrolling ancestor, off the viewport, or covered by another element at their centre (a sticky bar of a scroller that can still scroll, and what an open dialog / menu / toast covers, are not defects - the mobile inspector is a bottom sheet at ≤640 px; the agent drawer takes a column since N10, what it covers is one; known limit: it judges the current scroll position only; a centre within 1 px of a clipping edge counts as scrolled away, as clippedAway's edges do); returns 'reach-ok' or one finding per line
(()=>{const out=[];const W=innerWidth,H=innerHeight;
const vis=(el)=>{for(let e=el;e;e=e.parentElement){const cs=getComputedStyle(e);if(cs.display==='none'||cs.visibility==='hidden'||+cs.opacity===0)return false;}return true;};
// clipped by a scroll / overflow ancestor -> scrolled away, not a defect
const clippedAway=(el,r)=>{for(let e=el.parentElement;e&&e!==document.body;e=e.parentElement){const cs=getComputedStyle(e);if(/(auto|scroll|hidden|clip)/.test(cs.overflowX+cs.overflowY)){const p=e.getBoundingClientRect();if(r.bottom<=p.top+1||r.top>=p.bottom-1||r.right<=p.left+1||r.left>=p.right-1)return /(auto|scroll)/.test(cs.overflowX+cs.overflowY)?'scroll':e;}}return false;};
for(const el of document.querySelectorAll('button,a[href],input,select,textarea,[role=button],[role=tab],[role=menuitem],summary')){
 if(el.disabled||el.closest('[inert],.xterm,.cm-editor'))continue;{const d=el.closest('details:not([open])');if(d&&!(el.closest('summary')&&el.closest('summary').parentElement===d))continue;}const r=el.getBoundingClientRect();if(!r.width||!r.height||!vis(el))continue;
 {const c=clippedAway(el,r);if(c&&c!=='scroll'){if(!c.closest('[inert],[aria-hidden=true]')&&!/(^| )(sr-only|hidden)( |$)/.test(c.className))out.push('CLIPPED '+el.tagName+'.'+String(el.className).slice(0,25)+' '+JSON.stringify((el.getAttribute('aria-label')||el.textContent||'').trim().slice(0,25))+' by '+c.tagName+'.'+String(c.className).slice(0,30));continue;}if(c)continue;}
 const inScroll=(()=>{for(let e=el.parentElement;e&&e!==document.body;e=e.parentElement){const cs=getComputedStyle(e);if(/(auto|scroll)/.test(cs.overflowX+cs.overflowY))return true;}return false;})();
 if(!inScroll&&(r.right>W+1||r.bottom>H+1||r.left<-1||r.top<-1)){out.push('OFFVIEW '+el.tagName+'.'+String(el.className).slice(0,25)+' '+JSON.stringify((el.getAttribute('aria-label')||el.textContent||'').trim().slice(0,25))+' '+[r.left,r.top,r.right,r.bottom].map(Math.round));continue;}
 const cx=r.left+r.width/2,cy=r.top+r.height/2;const t=document.elementFromPoint(cx,cy);
 if(!t||el.contains(t)||t.contains(el))continue;
// an open dialog / menu covers everything under it - the floating approval cards and toasts too (#pending is z 16 under #overlay's 20 since 2026-09-30); a card or toast covers only the page
 {const menu=t.closest('.ctxmenu,[role=menu]');if(menu&&!menu.contains(el))continue;}
 if(t.closest('#overlay')&&!el.closest('#overlay'))continue;
 // At the CSS mobile breakpoint the context panel intentionally overlays the detail as a bottom sheet.
 if(W<=640&&t.closest('#inspector')&&el.closest('#main')&&!el.closest('#overlay,#pending,.toast,#toasts,.peek,.ctxmenu,[role=menu]'))continue;
 const popup=t.closest('.wk-project-pop:not(.hidden)'),popupLayer=popup&&popup.closest('.olayer');
 if(popupLayer&&popupLayer===el.closest('.olayer')&&!popup.contains(el))continue;
 if(t.closest('#pending,.toast,#toasts,.peek')&&!el.closest('#pending,.toast,#toasts,#overlay,.ctxmenu,[role=menu],.peek'))continue;
 if(inScroll&&(cy>H||cx>W))continue;
 {let out=false;for(let e=el.parentElement;e&&e!==document.body;e=e.parentElement){const cs=getComputedStyle(e);if(/(auto|scroll|hidden|clip)/.test(cs.overflowX+cs.overflowY)){const p=e.getBoundingClientRect();if(cx<p.left+1||cx>p.right-1||cy<p.top+1||cy>p.bottom-1){out=true;break;}}}if(out)continue;}
 {let st=null;for(let e=t;e&&e!==document.body;e=e.parentElement){if(getComputedStyle(e).position==='sticky'){st=e;break;}}if(st){let sc=null;for(let e=el.parentElement;e&&e!==document.body;e=e.parentElement){if(e.contains(st)&&/(auto|scroll)/.test(getComputedStyle(e).overflowY)&&e.scrollHeight>e.clientHeight){sc=e;break;}}if(sc&&(getComputedStyle(st).bottom!=='auto'?sc.scrollTop+sc.clientHeight<sc.scrollHeight-1:sc.scrollTop>0))continue;}}
 if(t.closest('label')&&t.closest('label').contains(el))continue;
 if(el.tagName==='INPUT'&&el.labels&&[...el.labels].some(l=>l.contains(t)))continue;
 out.push('COVERED '+el.tagName+'.'+String(el.className).slice(0,25)+' '+JSON.stringify((el.getAttribute('aria-label')||el.textContent||el.placeholder||'').trim().slice(0,25))+' by '+t.tagName+'.'+String(t.className).slice(0,30)+(t.closest('[id]')?' in #'+t.closest('[id]').id:''));}
return out.slice(0,15).join('\n')||'reach-ok'})()
