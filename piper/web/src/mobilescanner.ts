/** Camera frames stay on-device. Lazy jsQR also works where BarcodeDetector is absent (Safari). */
export function startQRScanner(video: HTMLVideoElement, read: (value: string) => void,
  error: (kind: 'denied' | 'unavailable' | 'interrupted') => void): () => void {
  let stopped = false, stream: MediaStream | null = null, timer = 0, deadline = 0;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    window.clearTimeout(timer); window.clearTimeout(deadline);
    document.removeEventListener('visibilitychange', hidden);
    window.removeEventListener('pagehide', leaving);
    stream?.getTracks().forEach(track => track.stop()); stream = null;
    video.pause(); video.srcObject = null;
  };
  const fail = (kind: 'denied' | 'unavailable' | 'interrupted') => { if (!stopped) { stop(); error(kind); } };
  const hidden = () => { if (document.hidden) fail('interrupted'); };
  const leaving = () => fail('interrupted');
  document.addEventListener('visibilitychange', hidden);
  window.addEventListener('pagehide', leaving);
  // Bound a permission/startup wait; a camera granted after cancellation is closed below too.
  deadline = window.setTimeout(() => fail('unavailable'), 20000);
  try {
    const camera = navigator.mediaDevices.getUserMedia({ audio: false,
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } } });
    void camera.then(async opened => {
      if (stopped) { opened.getTracks().forEach(track => track.stop()); return; }
      stream = opened;
      try {
        video.srcObject = stream;
        const [{ default: decode }] = await Promise.all([import('../vendor/jsqr/jsqr.js'), video.play()]);
        if (stopped) return;
        const canvas = document.createElement('canvas');
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) throw new Error('Camera frames unavailable');
        const frame = () => {
          if (stopped) return;
          try {
            if (video.readyState >= 2 && video.videoWidth && video.videoHeight) {
              window.clearTimeout(deadline);
              const scale = Math.min(1, 960 / Math.max(video.videoWidth, video.videoHeight));
              canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
              canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
              context.drawImage(video, 0, 0, canvas.width, canvas.height);
              const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
              const found = decode(pixels.data, pixels.width, pixels.height, { inversionAttempts: 'dontInvert' });
              if (found?.data) { stop(); read(found.data); }
            }
          } catch { fail('unavailable'); }
          if (!stopped) timer = window.setTimeout(frame, 200);
        };
        frame();
      } catch { fail('unavailable'); }
    }, cause => fail(cause?.name === 'NotAllowedError' || cause?.name === 'SecurityError' ? 'denied' : 'unavailable'));
  } catch { fail('unavailable'); }
  return stop;
}

/** Decode a selected photo locally; neither the image nor its contents leave this device. */
export async function readQRPhoto(file: File): Promise<string | null> {
  if (!file.type.startsWith('image/') || file.size > 20 * 1024 * 1024) throw new Error('Photo unavailable');
  const url = URL.createObjectURL(file);
  try {
    const image = new Image(); image.src = url;
    const [{ default: decode }] = await Promise.all([import('../vendor/jsqr/jsqr.js'), image.decode()]);
    const scale = Math.min(1, 1600 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Photo unavailable');
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    return decode(pixels.data, pixels.width, pixels.height)?.data ?? null;
  } finally { URL.revokeObjectURL(url); }
}
