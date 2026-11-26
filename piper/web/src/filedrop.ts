// Files tree drag & drop: what a drop carries, without the Files closure (unit-tested in filedrop.test.mjs).
// A row dragged inside the tree carries TREE_DRAG ({project, path}); a drop from Finder / the desktop carries
// files and folders (DataTransfer items -> FileSystemEntry), read here into a flat list of folders and files.

/** a tree row being dragged: its project and path (a custom type - never text for a prompt) */
export const TREE_DRAG = 'application/x-piper-tree';

/** what one Finder drop may bring at most (one file is bounded again by piperd, 50 MiB) */
export const DROP_MAX_ENTRIES = 1000;
export const DROP_MAX_BYTES = 200 << 20;

export interface Dropped { rel: string; dir: boolean; file?: File; abs?: string }

/** the folder a drop lands in: a folder row itself, a file row's folder, the empty tree the project root */
export function dropDir(nav: string | undefined, isDir: boolean): string {
  if (!nav) return '.';
  if (isDir) return nav;
  return nav.includes('/') ? nav.slice(0, nav.lastIndexOf('/')) : '.';
}

/** a tree move that changes nothing or cannot be done: onto itself, into its own folder, into its own subtree */
export function moveRefused(from: string, dir: string): string {
  const parent = from.includes('/') ? from.slice(0, from.lastIndexOf('/')) : '.';
  if (dir === parent) return 'already there';
  if (dir === from || dir.startsWith(from + '/')) return 'a folder cannot move into itself';
  return '';
}

/** the destination path of `from` moved into `dir` */
export const movedTo = (from: string, dir: string): string => `${dir === '.' ? '' : dir + '/'}${from.split('/').pop()}`;

/** a drop's top-level names that already exist in the target folder */
export function clashes(items: Dropped[], taken: Set<string>): string[] {
  return items.filter((x) => !x.rel.includes('/') && taken.has(x.rel)).map((x) => x.rel);
}

interface Entry { isFile: boolean; isDirectory: boolean; name: string; file?(ok: (f: File) => void, no: (e: unknown) => void): void; createReader?(): { readEntries(ok: (e: Entry[]) => void, no: (e: unknown) => void): void } }

/** the dropped files and folders, folders first (parents before children); throws past the limits */
export async function collectDropped(dt: DataTransfer, pathFor?: (f: File) => string): Promise<Dropped[]> {
  const out: Dropped[] = [];
  let bytes = 0;
  const add = (d: Dropped) => {
    out.push(d);
    if (out.length > DROP_MAX_ENTRIES) throw new Error(`more than ${DROP_MAX_ENTRIES} files and folders - copy them in Finder or with git`);
    bytes += d.file?.size ?? 0;
    if (bytes > DROP_MAX_BYTES) throw new Error(`more than ${DROP_MAX_BYTES >> 20} MiB - copy them in Finder or with git`);
  };
  const walk = async (e: Entry, rel: string): Promise<void> => {
    if (e.isDirectory && e.createReader) {
      add({ rel, dir: true });
      const reader = e.createReader();
      for (;;) {   // readEntries answers in batches until an empty one
        const batch = await new Promise<Entry[]>((ok, no) => reader.readEntries(ok, no));
        if (!batch.length) break;
        for (const c of batch) await walk(c, `${rel}/${c.name}`);
      }
    } else if (e.isFile && e.file) {
      const f = await new Promise<File>((ok, no) => e.file!(ok, no));
      add({ rel, dir: false, file: f });
    }
  };
  const items = [...(dt.items ?? [])].filter((i) => i.kind === 'file');
  const entries = items.map((i) => (i as DataTransferItem & { webkitGetAsEntry?(): Entry | null }).webkitGetAsEntry?.() ?? null);
  const files = [...(dt.files ?? [])];
  if (entries.length && entries.every(Boolean)) {
    for (const [i, e] of entries.entries()) {
      await walk(e!, e!.name);
      const top = out.find((d) => d.rel === e!.name);
      const f = files[i];
      if (top && f && pathFor) { const abs = pathFor(f); if (abs) top.abs = abs; }
    }
  } else {   // no entry API: plain files only
    for (const f of files) add({ rel: f.name, dir: false, file: f, ...(pathFor?.(f) ? { abs: pathFor(f) } : {}) });
  }
  return out;
}
