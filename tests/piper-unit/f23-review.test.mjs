// node --test: F23 Review - the diff first: one header row, ⋯, Ask Claude ▾ beside the block, one foot.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { reviewPrimary } from '../../piper/web/dist/gitpanel.js';

const git = readFileSync(new URL('../../piper/web/src/gitpanel.ts', import.meta.url), 'utf8');
const cm = readFileSync(new URL('../../piper/web/src/cmeditor.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const review = git.slice(git.indexOf('async function showReview('), git.indexOf('\n  let busy = false;'));

test('F23: one header row - Review, the scope (explained in its tooltip), File n/N p j, Change n/N ⇧F7 F7, Viewed v, ⋯, ×', () => {
  const bar = review.slice(review.indexOf("h('div', { class: 'review-toolbar row' }"), review.indexOf("scopeHint, loadingStatus"));
  const order = ['scopePick', 'fileAt', "h('kbd', {}, 'p')", "h('kbd', {}, 'j')", 'changeAt', "h('kbd', {}, '⇧F7')", "h('kbd', {}, 'F7')", 'viewed', 'moreButton', "'Close review'"];
  const at = order.map((k) => bar.indexOf(k));
  assert.ok(at.every((i) => i > 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at, 'the board\'s order');
  assert.match(review, /deps\.modal\('Review', root\);/);
  assert.match(review, /if \(heading\) root\.querySelector\('\.review-toolbar'\)!\.prepend\(heading\);/, 'the title sits in the same row');
  assert.match(review, /scopePick\.title = markBidi\(\[scopeHint\.textContent, pinnedLine\]\.filter\(Boolean\)\.join/, 'the two explanation lines became the tooltip');
  assert.match(review, /fileAt\.textContent = markBidi\(ordered\(\)\.length \? `File \$\{fileIndex \+ 1\}\/\$\{ordered\(\)\.length\}` : 'File'\);/);
  assert.match(review, /changeAt\.textContent = markBidi\(pos\?\.total \? `Change \$\{pos\.index\}\/\$\{pos\.total\}` : 'Change'\);/);
  assert.match(review, /viewedLabel\.textContent = file && isViewed\(file\) \? '✓ Viewed' : 'Viewed';/);
});

test('F23: ⋯ holds Revert, Delete code, Accept, Undo, Add to Git, Open in Files, Refresh, then Files / Review items / Working focus / Fullscreen', () => {
  assert.match(review, /h\('div', \{ class: 'review-more hidden', role: 'group', 'aria-label': 'More review actions' \},\n\s+restore, remove, accept, undo, stage, open, refreshButton, h\('hr', \{\}\), fileToggle, actionToggle, workingToggle, fullscreenToggle\)/);
  assert.match(review, /event\.key === 'Escape' && !more\.classList\.contains\('hidden'\)/);
  assert.match(review, /if \(!moreButton\.parentElement!\.contains\(event\.target as Node\)\) setMore\(false\);/, 'a click elsewhere closes it');
});

test('Review exposes numbered actions above the diff and retains numeric shortcuts', () => {
  assert.match(review, /scopeHint, loadingStatus, banner, palette,/);
  assert.match(review, /const digit = reviewDigit\(event\);/);
  assert.match(review, /actionMenu\(templates, found\?\.actions \?\? \[\], choose\)/);
  assert.doesNotMatch(review, /review-ask-pop hidden/);
});

test('F23: one foot - viewed progress, the review items as a count (the list on click), message, Commit N files, Prepare task (n) ⌘⏎', () => {
  assert.match(review, /h\('div', \{ class: 'review-footer row' \}, count, itemsLink, progress, h\('span', \{ class: 'grow' \}\), commitNote, commitMessage, commit, send\)/);
  assert.doesNotMatch(review, /commitBox/, 'no second commit row');
  assert.match(review, /itemsLink\.textContent = markBidi\(`\$\{items\} review \$\{items === 1 \? 'item' : 'items'\}`\);/);
  assert.match(review, /progress\.max = Math\.max\(1, visible\); progress\.value = seen;/);
  assert.match(review, /layout\.actions = false;/, 'the review items never open by themselves');
  assert.match(review, /JSON\.stringify\(\{ \.\.\.layout, actions: false \}\)/);
  assert.equal(reviewPrimary(2), 'send');
  assert.equal(reviewPrimary(0), 'commit');
  assert.match(review, /send\.classList\.toggle\('primary', primary === 'send'\);\n\s+commit\.classList\.toggle\('primary', primary === 'commit'\);/, 'one green button');
});

test('F23: the file list - the name (path in the tooltip), the letter in its colour, ✓ = viewed; Lock · Generated · n more folded', () => {
  assert.match(review, /layout\.tree \? showBidi\(file\.path\.slice/);
  assert.match(review, /stale\.has\(file\.path\) \? 'Changed' : isViewed\(file\) \? '✓' : ''\)/, 'no ○ for every unviewed file');
  assert.match(review, /const rest = reviewGroups\.slice\(2\)/);
  assert.match(review, /`\$\{rest\.map\(\(group\) => group\.replace\(' files', ''\)\)\.join\(' · '\)\} · \$\{restFiles\} more`/);
  assert.match(review, /function unfoldGroup\(group: string\): void \{ expanded\.add\(group\); if \(reviewGroups\.indexOf\(group\) > 1\) expanded\.add\('Other'\); \}/,
    'navigating into a folded group opens the fold');
});

test('F23: CSS - one block, the ⋯ card, Ask Claude beside the block, the foot on one row', () => {
  const block = css.slice(css.indexOf('/* ==== 9.87 F23'), css.indexOf('/* ==== end F23 ==== */'));
  assert.ok(block.length > 500);
  assert.match(block, /\.review-more \{ position: absolute; right: 0;/);
  assert.match(block, /\.reviewpane > \.review-palette \{ flex: none;/);
  assert.match(block, /\.reviewpane \.review-footer \{ flex-wrap: nowrap;/);
  assert.match(block, /\.reviewpane \.review-banner:empty \{ display: none; \}/, 'the routine Pinned line left the page');
  assert.doesNotMatch(css, /\.review-heading \{|\.review-layout \{|\.review-commit \.warn/, 'the old header / commit row rules are gone');
});
