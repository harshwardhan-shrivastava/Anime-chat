/**
 * Canvas Fire Badge Renderer — Doom-style fire on badge edges
 * Creates tall, visible blue flames that wrap around the S rank badge
 */
(function () {
  const FIRE_HEIGHT = 40;
  const FIRE_WIDTH_UPSCALE = 2;
  const FIRE_INTENSITY = 255;
  const FPS = 24;

  // Blue fire palette: transparent → deep blue → bright blue → white
  const palette = [];
  for (let i = 0; i < FIRE_INTENSITY; i++) {
    const t = i / FIRE_INTENSITY;
    if (t < 0.15) {
      // nearly invisible base
      const a = t / 0.15;
      palette.push([10, 20, 60, Math.floor(a * 60)]);
    } else if (t < 0.35) {
      // dark blue
      const a = (t - 0.15) / 0.20;
      palette.push([
        Math.floor(10 + a * 30),
        Math.floor(20 + a * 80),
        Math.floor(80 + a * 120),
        Math.floor(60 + a * 120)
      ]);
    } else if (t < 0.55) {
      // medium blue
      const a = (t - 0.35) / 0.20;
      palette.push([
        Math.floor(40 + a * 40),
        Math.floor(100 + a * 80),
        200 + Math.floor(a * 55),
        180 + Math.floor(a * 40)
      ]);
    } else if (t < 0.75) {
      // bright blue
      const a = (t - 0.55) / 0.20;
      palette.push([
        80 + Math.floor(a * 100),
        180 + Math.floor(a * 50),
        255,
        220 + Math.floor(a * 35)
      ]);
    } else {
      // hot white-blue
      const a = (t - 0.75) / 0.25;
      palette.push([
        180 + Math.floor(a * 75),
        220 + Math.floor(a * 35),
        255,
        255
      ]);
    }
  }

  function createFireBadge(el) {
    if (el.dataset.fireInit) return;
    el.dataset.fireInit = '1';
    el.style.position = 'relative';

    const badge = el.querySelector('.rank-badge-inner') || el;
    const badgeRect = badge.getBoundingClientRect();

    // Canvas extends well beyond the badge for tall flames
    const flameMargin = FIRE_HEIGHT + 20;
    const canvasW = Math.ceil(badgeRect.width + flameMargin * 2);
    const canvasH = Math.ceil(badgeRect.height + flameMargin * 2);

    const canvas = document.createElement('canvas');
    canvas.width = canvasW;
    canvas.height = canvasH;
    canvas.style.cssText = `
      position: absolute;
      top: -${flameMargin}px;
      left: -${flameMargin}px;
      width: ${canvasW}px;
      height: ${canvasH}px;
      pointer-events: none;
      z-index: 10;
      mix-blend-mode: screen;
    `;
    el.appendChild(canvas);
    const ctx = canvas.getContext('2d');

    const fireW = Math.ceil(canvasW / FIRE_WIDTH_UPSCALE);
    const fireH = FIRE_HEIGHT + 20;
    const firePixels = new Uint8Array(fireW * fireH);

    // Badge edges in fire-pixel coordinates (offset by flameMargin / FIRE_WIDTH_UPSCALE)
    const offset = flameMargin / FIRE_WIDTH_UPSCALE;
    const badgeFireW = Math.ceil(badgeRect.width / FIRE_WIDTH_UPSCALE);
    const badgeFireH = Math.ceil(badgeRect.height / FIRE_WIDTH_UPSCALE);

    function seedBottom() {
      // Bottom edge fire — strong
      for (let x = 0; x < fireW; x++) {
        firePixels[(fireH - 1) * fireW + x] = FIRE_INTENSITY - 1;
      }
    }

    function propagate() {
      for (let x = 0; x < fireW; x++) {
        for (let y = 1; y < fireH; y++) {
          const src = y * fireW + x;
          const rand = Math.round(Math.random() * 3) & 3;
          const fromX = (x - rand + 1 + fireW) % fireW;
          const fromY = y - 1;
          const decay = (Math.random() * 4) | 0;
          const val = Math.max(0, firePixels[fromY * fireW + fromX] - decay);
          firePixels[src] = val;
        }
      }

      // Seed edges based on badge position — so fire surrounds the badge
      // Bottom edge of badge
      const botY = Math.min(fireH - 1, Math.ceil(offset + badgeFireH));
      for (let x = Math.floor(offset); x < Math.floor(offset + badgeFireW); x++) {
        if (Math.random() > 0.3) {
          firePixels[botY * fireW + x] = FIRE_INTENSITY - 1;
        }
      }

      // Top edge of badge — fire rises UP from here
      const topY = Math.max(0, Math.floor(offset) - 1);
      for (let x = Math.floor(offset); x < Math.floor(offset + badgeFireW); x++) {
        if (Math.random() > 0.5) {
          firePixels[topY * fireW + x] = Math.floor(FIRE_INTENSITY * 0.7 + Math.random() * FIRE_INTENSITY * 0.3);
        }
      }

      // Left edge
      const leftX = Math.max(0, Math.floor(offset) - 1);
      for (let y = Math.floor(offset); y < Math.floor(offset + badgeFireH); y++) {
        if (Math.random() > 0.4) {
          firePixels[y * fireW + leftX] = FIRE_INTENSITY - 1;
        }
      }

      // Right edge
      const rightX = Math.min(fireW - 1, Math.ceil(offset + badgeFireW));
      for (let y = Math.floor(offset); y < Math.floor(offset + badgeFireH); y++) {
        if (Math.random() > 0.4) {
          firePixels[y * fireW + rightX] = FIRE_INTENSITY - 1;
        }
      }
    }

    function render() {
      const imgData = ctx.createImageData(canvasW, canvasH);
      const d = imgData.data;
      for (let fy = 0; fy < fireH; fy++) {
        for (let fx = 0; fx < fireW; fx++) {
          const val = firePixels[fy * fireW + fx];
          if (val < 5) continue;
          const [r, g, b, a] = palette[val];
          // Scale fire pixel to canvas pixel area
          const cx = fx * FIRE_WIDTH_UPSCALE;
          const cy = fy;
          for (let dy = 0; dy < FIRE_WIDTH_UPSCALE + 1 && cy + dy < canvasH; dy++) {
            for (let dx = 0; dx < FIRE_WIDTH_UPSCALE + 1 && cx + dx < canvasW; dx++) {
              const idx = ((cy + dy) * canvasW + (cx + dx)) * 4;
              d[idx] = r;
              d[idx + 1] = g;
              d[idx + 2] = b;
              d[idx + 3] = a;
            }
          }
        }
      }
      ctx.putImageData(imgData, 0, 0);
    }

    let running = true;
    function loop() {
      if (!running) return;
      seedBottom();
      propagate();
      render();
      requestAnimationFrame(() => setTimeout(loop, 1000 / FPS));
    }

    // Stop when scrolled out of view
    const obs = new IntersectionObserver((entries) => {
      running = entries[0].isIntersecting;
      if (running) loop();
    }, { threshold: 0 });
    obs.observe(el);

    loop();
  }

  function scan() {
    document.querySelectorAll('.rank-S').forEach(createFireBadge);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scan);
  } else {
    scan();
  }

  // Watch for dynamically added badges
  const mo = new MutationObserver(scan);
  mo.observe(document.body, { childList: true, subtree: true });
})();
