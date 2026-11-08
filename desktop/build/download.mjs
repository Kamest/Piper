import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';

const run = promisify(execFile);
const repositories = new Set(['electron/electron', 'astral-sh/python-build-standalone', 'electron-userland/electron-builder-binaries']);

export function releaseAsset(url) {
  const u = new URL(url);
  const match = /^\/([^/]+\/[^/]+)\/releases\/download\/([^/]+)\/([^/]+)$/.exec(u.pathname);
  if (u.origin !== 'https://github.com' || u.username || u.password || u.search || u.hash || !match || !repositories.has(match[1])) {
    throw new Error('Expected an official build dependency release asset');
  }
  const [, repo, tag, name] = match.map(decodeURIComponent);
  if ([tag, name].some(value => /[/\\]|\.\./.test(value))) throw new Error('Unsafe release asset name');
  return { repo, tag, name };
}

async function ghApi(endpoint, binary = false) {
  try {
    const { stdout } = await run('gh', ['api', ...(binary ? ['-H', 'Accept: application/octet-stream'] : []), endpoint],
      { encoding: 'buffer', maxBuffer: 256 * 1024 * 1024, timeout: 180_000 });
    return stdout;
  } catch {
    // gh handles credentials and redirects. Never echo its environment or potentially sensitive stderr.
    throw new Error('Official GitHub build dependency download failed');
  }
}

export function verify(bytes, checksum) {
  if (!/^[a-f0-9]{64}$/i.test(checksum || '') || createHash('sha256').update(bytes).digest('hex') !== checksum.toLowerCase()) {
    throw new Error('Build dependency SHA-256 mismatch or missing checksum');
  }
  return bytes;
}

export async function githubAsset(url, checksum, api = ghApi) {
  const { repo, tag, name } = releaseAsset(url);
  const release = JSON.parse((await api(`repos/${repo}/releases/tags/${encodeURIComponent(tag)}`)).toString());
  const asset = release.assets?.find(item => item.name === name && item.state === 'uploaded');
  if (!Number.isSafeInteger(asset?.id) || asset.id <= 0) throw new Error('Official build dependency asset missing');
  const digest = asset.digest?.match(/^sha256:([a-f0-9]{64})$/i)?.[1];
  const expected = checksum || digest;
  if (!/^[a-f0-9]{64}$/i.test(expected || '')) throw new Error('Build dependency checksum missing');
  if (checksum && digest && checksum.toLowerCase() !== digest.toLowerCase()) throw new Error('Official build dependency checksum disagrees with pin');
  const bytes = await api(`repos/${repo}/releases/assets/${asset.id}`, true);
  if (bytes.length !== asset.size) throw new Error('Build dependency download incomplete');
  return { bytes: verify(bytes, expected), checksum: expected.toLowerCase() };
}

export async function download(url, checksum, { fetcher = fetch, api = ghApi } = {}) {
  releaseAsset(url);
  let response;
  try { response = await fetcher(url, { signal: AbortSignal.timeout(60_000) }); } catch { /* official API fallback */ }
  if (response?.ok) return verify(Buffer.from(await response.arrayBuffer()), checksum);
  if (response && response.status < 500) throw new Error(`Build dependency HTTP ${response.status}`);
  return (await githubAsset(url, checksum, api)).bytes;
}
