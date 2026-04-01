/**
 * TokenGauge — Reusable TPM gauge factory.
 *
 * Creates self-contained gauge instances with:
 *   - 10s sliding window for true TPM (M tok/min)
 *   - EWMA-smoothed needle (τ=1.5s)
 *   - Flash glow on new events (hold 50ms + decay τ=300ms)
 *   - Asymmetric auto-scale (up=immediate, down=5s cooldown)
 *
 * Usage:
 *   const gauge = TokenGauge.create(container, {
 *     id: 'cache-read',
 *     label: 'Cache Read',
 *     unit: 'M tok/min',
 *     costPerMTok: 0.30,
 *     segments: TokenGauge.SEGMENTS.GREEN_TO_RED,
 *     stats: [
 *       { label: 'Sessions actives', id: 'sessions', initial: '0' },
 *       { label: 'Total', id: 'total', initial: '0' },
 *     ],
 *   });
 *
 *   gauge.push(120000);        // inject 120K tokens
 *   gauge.read();              // { raw: 1.2, display: 1.1, peak: 1.5 }
 *   gauge.stat('sessions', 3); // update stat value
 *   gauge.reset();             // clear buffer + needle
 *   gauge.destroy();           // stop animation, remove DOM
 */

const TokenGauge = (() => {
  // ── Defaults ──────────────────────────────────────────────────────────────
  const DEFAULTS = {
    windowMs: 10000,         // 10s sliding window
    needleTau: 1500,         // EWMA τ for needle (ms)
    flashHoldMs: 50,         // flash hold before decay
    flashDecayTau: 300,      // flash glow decay τ (ms)
    scaleDownCooldown: 5000, // 5s min between scale-downs
    tickMs: 50,              // 20fps
    initialMax: 2,           // M tok/min starting scale
    canvasWidth: 440,        // physical canvas px (2x for retina)
    canvasHeight: 260,
    displayWidth: 220,       // CSS display size
    displayHeight: 130,
    zeroThreshold: 0.005,    // below this → display 0
    costPerMTok: null,       // $/MTok — null hides cost stat
    unit: 'M tok/min',
    label: 'Gauge',
  };

  // ── Preset color segments ────────────────────────────────────────────────
  const SEGMENTS = {
    GREEN_TO_RED: [
      { from: 0,    to: 0.25, color: '#22c55e' },
      { from: 0.25, to: 0.50, color: '#84cc16' },
      { from: 0.50, to: 0.70, color: '#eab308' },
      { from: 0.70, to: 0.85, color: '#f97316' },
      { from: 0.85, to: 1.00, color: '#ef4444' },
    ],
    BLUE_TO_PURPLE: [
      { from: 0,    to: 0.25, color: '#3b82f6' },
      { from: 0.25, to: 0.50, color: '#6366f1' },
      { from: 0.50, to: 0.70, color: '#8b5cf6' },
      { from: 0.70, to: 0.85, color: '#a855f7' },
      { from: 0.85, to: 1.00, color: '#ef4444' },
    ],
    AMBER_MONO: [
      { from: 0,    to: 0.50, color: '#f59e0b' },
      { from: 0.50, to: 0.75, color: '#f97316' },
      { from: 0.75, to: 1.00, color: '#ef4444' },
    ],
    COOL_MONO: [
      { from: 0,    to: 0.50, color: '#22c55e' },
      { from: 0.50, to: 0.75, color: '#10b981' },
      { from: 0.75, to: 1.00, color: '#14b8a6' },
    ],
  };

  // ── DOM builder ──────────────────────────────────────────────────────────
  function buildDOM(container, id, opts) {
    const section = document.createElement('div');
    section.className = 'gauge-section';
    section.id = `gauge-section-${id}`;

    const wrap = document.createElement('div');
    wrap.className = 'gauge-wrap';

    const canvas = document.createElement('canvas');
    canvas.id = `gaugeCanvas-${id}`;
    canvas.width = opts.canvasWidth;
    canvas.height = opts.canvasHeight;
    canvas.style.width = opts.displayWidth + 'px';
    canvas.style.height = opts.displayHeight + 'px';

    const valueDiv = document.createElement('div');
    valueDiv.className = 'gauge-value';
    valueDiv.id = `gaugeValue-${id}`;
    valueDiv.innerHTML = `0<span class="gauge-unit">${opts.unit}</span>`;

    wrap.appendChild(canvas);
    wrap.appendChild(valueDiv);
    section.appendChild(wrap);

    // Stats panel
    const stats = document.createElement('div');
    stats.className = 'gauge-stats';

    // Title row
    const titleRow = document.createElement('div');
    titleRow.className = 'gauge-stat-row';
    titleRow.innerHTML = `<span class="gauge-stat-label" style="font-size:13px;font-weight:600;text-transform:none;letter-spacing:0;color:var(--text)">${opts.label}</span>`;
    stats.appendChild(titleRow);

    // Custom stats
    if (opts.stats) {
      for (const s of opts.stats) {
        const row = document.createElement('div');
        row.className = 'gauge-stat-row';
        const cls = s.style ? ` ${s.style}` : '';
        row.innerHTML = `<span class="gauge-stat-label">${s.label}</span><span class="gauge-stat-value${cls}" id="gaugeStat-${id}-${s.id}">${s.initial || '—'}</span>`;
        stats.appendChild(row);
      }
    }

    // Cost row (auto-generated if costPerMTok set)
    if (opts.costPerMTok !== null) {
      const costRow = document.createElement('div');
      costRow.className = 'gauge-stat-row';
      costRow.innerHTML = `<span class="gauge-stat-label">Coût/min</span><span class="gauge-stat-value warm" id="gaugeCost-${id}">$0</span>`;
      stats.appendChild(costRow);
    }

    section.appendChild(stats);
    container.appendChild(section);

    return { canvas, valueDiv, section };
  }

  // ── Draw function (exact same style as original) ─────────────────────────
  function drawGauge(ctx, W, H, rate, gaugeMax, segments, flashTs, flashHoldMs, flashDecayTau) {
    const cx = W / 2, cy = H - 20;
    const R = Math.min(cx - 20, cy - 10);
    const startAngle = Math.PI;

    ctx.clearRect(0, 0, W, H);

    // Dim arc background
    ctx.lineWidth = 18;
    ctx.lineCap = 'butt';
    for (const seg of segments) {
      ctx.beginPath();
      ctx.arc(cx, cy, R, startAngle + seg.from * Math.PI, startAngle + seg.to * Math.PI);
      ctx.strokeStyle = seg.color + '40';
      ctx.stroke();
    }

    // Filled arc
    const pct = Math.min(rate / gaugeMax, 1);
    const filledEnd = startAngle + pct * Math.PI;
    ctx.lineWidth = 18;
    ctx.lineCap = 'round';
    for (const seg of segments) {
      const a1 = startAngle + seg.from * Math.PI;
      const a2 = startAngle + seg.to * Math.PI;
      if (a1 >= filledEnd) break;
      ctx.beginPath();
      ctx.arc(cx, cy, R, a1, Math.min(a2, filledEnd));
      ctx.strokeStyle = seg.color;
      ctx.shadowColor = seg.color;
      ctx.shadowBlur = 8;
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    // Glow + flash
    if (pct > 0.01) {
      const tipX = cx + Math.cos(filledEnd) * R;
      const tipY = cy + Math.sin(filledEnd) * R;
      const tipSeg = segments.find(s => pct <= s.to) || segments[segments.length - 1];

      let flashIntensity = 0;
      const flashAge = performance.now() - flashTs;
      if (flashAge < flashHoldMs) {
        flashIntensity = 1.0;
      } else {
        flashIntensity = Math.exp(-(flashAge - flashHoldMs) / flashDecayTau);
      }
      const glowRadius = 14 + flashIntensity * 10;
      const glowAlpha = Math.round(0x80 + flashIntensity * 0x7f).toString(16).padStart(2, '0');

      const glow = ctx.createRadialGradient(tipX, tipY, 0, tipX, tipY, glowRadius);
      glow.addColorStop(0, tipSeg.color + glowAlpha);
      glow.addColorStop(1, tipSeg.color + '00');
      ctx.beginPath();
      ctx.arc(tipX, tipY, glowRadius, 0, 2 * Math.PI);
      ctx.fillStyle = glow;
      ctx.fill();
    }

    // Needle
    const needleAngle = startAngle + pct * Math.PI;
    const needleLen = R - 25;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(needleAngle) * needleLen, cy + Math.sin(needleAngle) * needleLen);
    ctx.strokeStyle = '#e2e8f0';
    ctx.lineWidth = 2.5;
    ctx.lineCap = 'round';
    ctx.shadowColor = '#e2e8f0';
    ctx.shadowBlur = 6;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Center dot
    ctx.beginPath();
    ctx.arc(cx, cy, 5, 0, 2 * Math.PI);
    ctx.fillStyle = '#e2e8f0';
    ctx.fill();

    // Scale labels
    ctx.font = '11px Inter, system-ui, sans-serif';
    ctx.fillStyle = '#64748b';
    ctx.textAlign = 'center';
    const steps = [0, 0.1, 0.25, 0.5, 0.75, 1].map(f => {
      const v = f * gaugeMax;
      return v >= 10 ? Math.round(v) : +v.toFixed(1);
    });
    for (const v of steps) {
      const a = startAngle + (v / gaugeMax) * Math.PI;
      ctx.fillText(v + '', cx + Math.cos(a) * (R + 16), cy + Math.sin(a) * (R + 16) + 4);
    }
  }

  // ── Factory ──────────────────────────────────────────────────────────────
  function create(container, userOpts = {}) {
    const opts = { ...DEFAULTS, ...userOpts };
    const id = opts.id || 'gauge-' + Math.random().toString(36).slice(2, 8);
    const segments = opts.segments || SEGMENTS.GREEN_TO_RED;

    // State
    const buffer = [];           // [{tokens, ts}]
    let rawTPM = 0;
    let displayRate = 0;
    let gaugeMax = opts.initialMax;
    let peakRate = 0;
    let flashTs = 0;
    let lastScaleChangeTs = 0;
    let scaleDownCount = 0;
    let lastScaleCheckTs = 0;
    const SCALE_CHECK_INTERVAL = 2000;  // check scale every 2s (matches poll cadence)
    let intervalId = null;

    // DOM
    const dom = buildDOM(container, id, opts);
    const canvasCtx = dom.canvas.getContext('2d');
    const W = dom.canvas.width;
    const H = dom.canvas.height;
    const costEl = opts.costPerMTok !== null ? document.getElementById(`gaugeCost-${id}`) : null;

    // ── Tick ────────────────────────────────────────────────────────────────
    function tick() {
      const now = performance.now();

      // 1. Prune buffer
      let cutoff = 0;
      while (cutoff < buffer.length && (now - buffer[cutoff].ts) > opts.windowMs) {
        cutoff++;
      }
      if (cutoff > 0) buffer.splice(0, cutoff);

      // 2. True TPM
      if (buffer.length > 0) {
        const totalTokens = buffer.reduce((sum, s) => sum + s.tokens, 0);
        const oldestAge = now - buffer[0].ts;
        const windowMs = Math.max(oldestAge, 1000);
        const windowMin = windowMs / 60000;
        rawTPM = totalTokens / windowMin / 1e6;
      } else {
        rawTPM = 0;
      }

      // 3. EWMA needle
      const alpha = 1 - Math.exp(-opts.tickMs / opts.needleTau);
      displayRate = displayRate + alpha * (rawTPM - displayRate);
      if (displayRate < opts.zeroThreshold) displayRate = 0;

      // 4. Track peak
      if (rawTPM > peakRate) peakRate = rawTPM;

      // 5. Auto-scale — checked every 2s (not every tick), asymmetric hysteresis
      //    Scale-up is always immediate regardless of cadence (safety valve)
      if (displayRate > gaugeMax * 0.7) {
        gaugeMax = Math.max(opts.initialMax, Math.ceil(displayRate * 2));
        lastScaleChangeTs = now;
        lastScaleCheckTs = now;
        scaleDownCount = 0;
      } else if ((now - lastScaleCheckTs) >= SCALE_CHECK_INTERVAL) {
        lastScaleCheckTs = now;
        if (displayRate < gaugeMax * 0.2 && gaugeMax > opts.initialMax) {
          scaleDownCount++;
          if (scaleDownCount >= 6 && (now - lastScaleChangeTs) > opts.scaleDownCooldown) {
            gaugeMax = Math.max(opts.initialMax, Math.ceil(displayRate * 4) || opts.initialMax);
            lastScaleChangeTs = now;
            scaleDownCount = 0;
          }
        } else {
          scaleDownCount = 0;
        }
      }

      // 6. Draw
      drawGauge(canvasCtx, W, H, displayRate, gaugeMax, segments, flashTs, opts.flashHoldMs, opts.flashDecayTau);

      // 7. Update value display
      dom.valueDiv.innerHTML = `${rawTPM.toFixed(1)}<span class="gauge-unit">${opts.unit}</span>`;

      // 8. Cost (cached DOM ref)
      if (costEl) {
        const costMin = rawTPM * opts.costPerMTok;
        costEl.textContent = costMin > 0.005 ? `$${costMin.toFixed(2)}/min` : '$0';
      }
    }

    // ── Start animation ────────────────────────────────────────────────────
    function start() {
      if (!intervalId) {
        intervalId = setInterval(tick, opts.tickMs);
      }
    }

    // ── Public API ─────────────────────────────────────────────────────────
    const instance = {
      /** Inject a token count (raw tokens, not millions). */
      push(tokens) {
        if (tokens > 0) {
          buffer.push({ tokens, ts: performance.now() });
          flashTs = performance.now();
        }
      },

      /** Read current state. */
      read() {
        return { raw: rawTPM, display: displayRate, peak: peakRate, max: gaugeMax, bufferSize: buffer.length };
      },

      /** Update a named stat value. */
      stat(statId, value) {
        const el = document.getElementById(`gaugeStat-${id}-${statId}`);
        if (el) el.textContent = value;
      },

      /** Reset gauge to zero. */
      reset() {
        buffer.length = 0;
        rawTPM = 0;
        displayRate = 0;
        peakRate = 0;
        gaugeMax = opts.initialMax;
        scaleDownCount = 0;
        flashTs = 0;
      },

      /** Stop animation, release resources, remove DOM. */
      destroy() {
        if (intervalId) { clearInterval(intervalId); intervalId = null; }
        buffer.length = 0;
        dom.section.remove();
      },

      /** Access the DOM section element for layout control. */
      get el() { return dom.section; },

      /** Access gauge max for external scaling coordination. */
      get max() { return gaugeMax; },
      set max(v) { gaugeMax = v; lastScaleChangeTs = performance.now(); },
    };

    // Auto-start
    start();
    // Initial draw
    drawGauge(canvasCtx, W, H, 0, gaugeMax, segments, 0, opts.flashHoldMs, opts.flashDecayTau);

    return instance;
  }

  return { create, SEGMENTS, DEFAULTS };
})();

// Export for both module and script contexts
if (typeof module !== 'undefined' && module.exports) {
  module.exports = TokenGauge;
}
