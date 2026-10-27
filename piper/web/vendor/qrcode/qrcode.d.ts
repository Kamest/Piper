/** ESM typing for the unchanged upstream dist/qrcode.mjs. */
export default function qrcode(typeNumber: 0, errorCorrectionLevel: 'L' | 'M' | 'Q' | 'H'): {
  addData(data: string): void;
  make(): void;
  getModuleCount(): number;
  isDark(row: number, column: number): boolean;
};
