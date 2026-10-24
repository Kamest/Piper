import { lstat, readdir, readlink, unlink, rmdir, cp } from 'node:fs/promises';
import path from 'node:path';

// Windows builds ship Linux Python that WSL copies with `cp -R`. Windows tar turns its relative links
// (bin/python3 -> python3.13) into Windows symlinks, created as directory links when the target is not
// extracted yet; 7-Zip then aborts the NSIS build. Replace every link inside `root` with a copy of its target.
export async function inlineSymlinks(root) {
  const resolve = async file => {
    for (let hops = 0; hops < 40; hops++) {
      if (!(await lstat(file)).isSymbolicLink()) return file;
      file = path.resolve(path.dirname(file), await readlink(file));
      if (path.relative(root, file).startsWith('..')) throw new Error(`${file} points outside ${root}`);
    }
    throw new Error(`${file}: symlink loop`);
  };
  const links = [];
  const walk = async dir => {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name);
      if (entry.isSymbolicLink()) links.push(file);
      else if (entry.isDirectory()) await walk(file);
    }
  };
  await walk(root);
  const targets = await Promise.all(links.map(resolve));
  for (const [i, link] of links.entries()) {
    // a link to a directory is removed with rmdir on Windows
    await unlink(link).catch(() => rmdir(link));
    await cp(targets[i], link, { recursive: true, dereference: true, preserveTimestamps: true });
  }
  return links.length;
}
