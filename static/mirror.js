/* FireDeck · espejo en vivo
 *
 * Recibe el H.264 en crudo (Annex B) que envía el servidor, lo convierte al
 * formato que espera WebCodecs (AVCC + avcC) y pinta cada fotograma en un
 * canvas. Sin dependencias: ni ffmpeg ni librerías de terceros.
 */
(function () {
  "use strict";

  const HEX = (value) => value.toString(16).padStart(2, "0");

  function concat(left, right) {
    const out = new Uint8Array(left.length + right.length);
    out.set(left, 0);
    out.set(right, left.length);
    return out;
  }

  /* Devuelve las posiciones de los códigos de inicio Annex B (00 00 01 o
   * 00 00 00 01) dentro de `data`. */
  function startCodes(data) {
    const found = [];
    let i = 0;
    while (i + 3 < data.length) {
      if (data[i] === 0 && data[i + 1] === 0) {
        if (data[i + 2] === 1) {
          found.push({ at: i, size: 3 });
          i += 3;
          continue;
        }
        if (data[i + 2] === 0 && data[i + 3] === 1) {
          found.push({ at: i, size: 4 });
          i += 4;
          continue;
        }
      }
      i += 1;
    }
    return found;
  }

  async function start(options) {
    const canvas = options.canvas;
    const context = canvas.getContext("2d", { alpha: false });
    const onStatus = options.onStatus || (() => {});
    const onFrame = options.onFrame || (() => {});
    const onError = options.onError || (() => {});

    if (typeof VideoDecoder === "undefined") {
      throw new Error("Este navegador no soporta WebCodecs (Chrome/Edge 94+).");
    }

    const response = await fetch(options.url, {
      signal: options.signal,
      cache: "no-store",
    });
    if (!response.ok) {
      let message = `El espejo no está disponible (${response.status}).`;
      try {
        const data = await response.json();
        message = data.error || message;
      } catch {
        /* la respuesta no era JSON */
      }
      throw new Error(message);
    }
    if (!response.body) {
      throw new Error("El navegador no permite leer el flujo del espejo.");
    }

    let buffer = new Uint8Array(0);
    let sps = null;
    let pps = null;
    let decoder = null;
    let decoderKey = "";
    let accessUnit = [];
    let flushTimer = null;
    let trailTimer = null;
    let timestamp = 0;
    let frames = 0;
    let framesAt = performance.now();
    let reportedFps = 0;
    let stopped = false;

    function drawFrame(frame) {
      if (stopped) {
        frame.close();
        return;
      }
      if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
        canvas.width = frame.displayWidth;
        canvas.height = frame.displayHeight;
      }
      context.drawImage(frame, 0, 0, canvas.width, canvas.height);
      frame.close();
      frames += 1;
      const now = performance.now();
      if (now - framesAt >= 1000) {
        reportedFps = Math.round((frames * 1000) / (now - framesAt));
        frames = 0;
        framesAt = now;
        onStatus({ state: "live", fps: reportedFps });
      }
      onFrame();
    }

    function ensureDecoder() {
      if (!sps || !pps) return;
      const codec = `avc1.${HEX(sps[1])}${HEX(sps[2])}${HEX(sps[3])}`;
      if (decoder && decoderKey === codec) return;

      const description = new Uint8Array(11 + sps.length + pps.length);
      description[0] = 1;
      description[1] = sps[1];
      description[2] = sps[2];
      description[3] = sps[3];
      description[4] = 0xff;
      description[5] = 0xe1;
      description[6] = sps.length >> 8;
      description[7] = sps.length & 0xff;
      description.set(sps, 8);
      description[8 + sps.length] = 1;
      description[9 + sps.length] = pps.length >> 8;
      description[10 + sps.length] = pps.length & 0xff;
      description.set(pps, 11 + sps.length);

      const config = {
        codec,
        description,
        optimizeForLatency: true,
      };

      if (decoder) {
        try {
          decoder.close();
        } catch {
          /* ya estaba cerrado */
        }
        decoder = null;
      }
      decoder = new VideoDecoder({
        output: drawFrame,
        error(error) {
          if (stopped) return;
          stopped = true;
          onError(error instanceof Error ? error : new Error(String(error)));
        },
      });
      decoder.configure(config);
      decoderKey = codec;
    }

    function flushAccessUnit(keyFrame) {
      if (!accessUnit.length) return;
      ensureDecoder();
      if (!decoder || decoder.state !== "configured") {
        accessUnit = [];
        return;
      }
      let size = 0;
      for (const nal of accessUnit) size += 4 + nal.length;
      const data = new Uint8Array(size);
      let offset = 0;
      for (const nal of accessUnit) {
        data[offset] = (nal.length >>> 24) & 0xff;
        data[offset + 1] = (nal.length >>> 16) & 0xff;
        data[offset + 2] = (nal.length >>> 8) & 0xff;
        data[offset + 3] = nal.length & 0xff;
        data.set(nal, offset + 4);
        offset += 4 + nal.length;
      }
      accessUnit = [];
      timestamp += 33333;
      try {
        decoder.decode(
          new EncodedVideoChunk({
            type: keyFrame ? "key" : "delta",
            timestamp,
            data,
          })
        );
      } catch {
        /* un fotograma suelto no debe tumbar el espejo */
      }
    }

    function handleNal(nal) {
      if (!nal.length) return;
      const type = nal[0] & 0x1f;
      if (type === 7) {
        sps = nal.slice();
        ensureDecoder();
        return;
      }
      if (type === 8) {
        pps = nal.slice();
        ensureDecoder();
        return;
      }
      if (type !== 1 && type !== 5) return;
      // first_mb_in_slice == 0 marca el comienzo de un fotograma nuevo.
      const firstSlice = (nal[1] & 0x80) === 0;
      if (firstSlice && accessUnit.length) {
        const key = (accessUnit[0][0] & 0x1f) === 5;
        flushAccessUnit(key);
      }
      accessUnit.push(nal);
      // El Fire TV solo emite fotogramas cuando la pantalla cambia. Si llega
      // uno y no hay más, hay que decodificarlo igualmente: se vacía el
      // fotograma pendiente tras un breve silencio.
      clearTimeout(flushTimer);
      flushTimer = setTimeout(() => {
        if (!accessUnit.length) return;
        const key = (accessUnit[0][0] & 0x1f) === 5;
        flushAccessUnit(key);
      }, 120);
    }

    function flushTrailing() {
      if (!buffer.length) return;
      const marks = startCodes(buffer);
      if (marks.length) {
        const last = marks[marks.length - 1];
        const nal = buffer.subarray(last.at + last.size);
        if (nal.length) handleNal(nal.slice());
      }
      buffer = new Uint8Array(0);
    }

    function consume(chunk) {
      buffer = concat(buffer, chunk);
      const marks = startCodes(buffer);
      // El último NAL puede estar incompleto: se guarda para el siguiente trozo.
      for (let index = 0; index < marks.length - 1; index += 1) {
        const start = marks[index].at + marks[index].size;
        handleNal(buffer.subarray(start, marks[index + 1].at));
      }
      if (marks.length) {
        buffer = buffer.slice(marks[marks.length - 1].at);
      } else if (buffer.length > 1_000_000) {
        buffer = new Uint8Array(0);
      }
      // Si el Fire TV no manda más (pantalla quieta), el último NAL se da por
      // terminado tras un silencio breve.
      clearTimeout(trailTimer);
      trailTimer = setTimeout(flushTrailing, 250);
    }

    function stop() {
      stopped = true;
      clearTimeout(flushTimer);
      clearTimeout(trailTimer);
      if (decoder) {
        try {
          decoder.close();
        } catch {
          /* ya estaba cerrado */
        }
        decoder = null;
      }
    }

    options.signal.addEventListener("abort", stop, { once: true });

    onStatus({ state: "connecting", fps: 0 });

    (async () => {
      const reader = response.body.getReader();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done || stopped) break;
          consume(value);
        }
        if (!stopped) {
          onStatus({ state: "ended", fps: reportedFps });
        }
      } catch (error) {
        if (!stopped && error.name !== "AbortError") onError(error);
      } finally {
        stop();
        try {
          reader.releaseLock();
        } catch {
          /* ya liberado */
        }
      }
    })();

    return { stop };
  }

  window.FireDeckMirror = { start };
})();
