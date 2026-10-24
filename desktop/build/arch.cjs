'use strict';
const path = require('node:path');

// Each CI job prepares Python for its own architecture only. With every architecture listed in the targets,
// a job also built the other one's installers, silently without the bundled runtime. Restrict the targets to
// `arch` and refuse to build an architecture whose runtime is missing.
const sections = { mac: 'mac', win: 'win', linux: 'linux' };

function forArch(build, arch) {
  if (!arch) return build;
  const out = { ...build };
  for (const key of Object.keys(sections)) {
    if (!build[key]?.target) continue;
    out[key] = { ...build[key], target: build[key].target.map(t => ({ ...t, arch: t.arch.filter(a => a === arch) })).filter(t => t.arch.length) };
    if (!out[key].target.length) throw new Error(`No ${key} target for ${arch}`);
  }
  return out;
}

function missingRuntimes(build, platform, root, exists) {
  const os = { darwin: 'mac', win32: 'win', linux: 'linux' }[platform];
  const arches = new Set((build[os]?.target || []).flatMap(t => t.arch));
  return [...arches].map(a => path.join(root, 'vendor', `${os}-${a}`, 'python')).filter(dir => !exists(dir));
}

module.exports = { forArch, missingRuntimes };
