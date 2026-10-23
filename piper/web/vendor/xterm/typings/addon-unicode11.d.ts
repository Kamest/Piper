/**
 * Copyright (c) 2017 The xterm.js authors. All rights reserved.
 * @license MIT
 */


declare module '@xterm/addon-unicode11' {
  import { Terminal, ITerminalAddon } from '@xterm/xterm';
  export class Unicode11Addon implements ITerminalAddon {
    constructor();
    public activate(terminal: Terminal): void;
    public dispose(): void;
  }
}
