/**
 * FireBadge — Canvas-based fire effect for rank badges.
 * Uses the classic Doom fire algorithm: fire pixels propagate upward
 * and fade, creating realistic-looking flames on badge edges.
 *
 * Activated on any element with class "fire-badge" or "rank-S".
 */
(function () {
    "use strict";

    // Blue fire palette: from hottest (white-blue) to coldest (transparent dark)
    var BLUE_FIRE_PALETTE = [
        [0, 0, 0, 0],        // 0 — empty
        [0, 0, 0, 0],
        [5, 5, 20, 10],
        [10, 15, 40, 40],
        [15, 30, 80, 80],
        [20, 50, 120, 140],
        [30, 70, 160, 200],
        [40, 90, 190, 230],
        [60, 120, 210, 245],
        [80, 150, 225, 250],
        [100, 170, 235, 255],
        [130, 190, 245, 255],
        [160, 210, 250, 255],
        [190, 225, 255, 255],
        [210, 235, 255, 255],
        [230, 245, 255, 255],  // 15 — hottest (white-blue)
    ];

    var FIRE_W = 64;  // fire buffer width
    var FIRE_H = 48;  // fire buffer height
    var FPS = 20;

    function FireBadge(el) {
        this.el = el;
        this.fire = new Uint8Array(FIRE_W * FIRE_H);
        this.running = false;
        this.timer = null;
        this._init();
    }

    FireBadge.prototype._init = function () {
        // Make the badge position relative so we can layer the canvas
        var cs = getComputedStyle(this.el);
        if (cs.position === "static") {
            this.el.style.position = "relative";
        }
        this.el.style.overflow = "visible";

        // Create canvas
        this.canvas = document.createElement("canvas");
        this.canvas.width = FIRE_W;
        this.canvas.height = FIRE_H;
        this.canvas.style.cssText =
            "position:absolute;pointer-events:none;z-index:10;" +
            "left:-8px;right:-8px;top:-32px;bottom:-8px;" +
            "width:calc(100% + 16px);height:calc(100% + 40px);" +
            "image-rendering:pixelated;mix-blend-mode:screen;opacity:0.85;";
        this.el.appendChild(this.canvas);
        this.ctx = this.canvas.getContext("2d");

        // Seed the bottom row with max fire
        for (var x = 0; x < FIRE_W; x++) {
            this.fire[(FIRE_H - 1) * FIRE_W + x] = 15;
        }
    };

    FireBadge.prototype._spread = function () {
        var fire = this.fire;
        for (var x = 0; x < FIRE_W; x++) {
            for (var y = 0; y < FIRE_H - 1; y++) {
                var src = y * FIRE_W + x;
                // Randomly spread fire upward with wind bias
                var decay = Math.floor(Math.random() * 3);
                var wind = Math.floor(Math.random() * 3) - 1; // -1, 0, 1
                var sx = Math.min(FIRE_W - 1, Math.max(0, x + wind));
                var sy = Math.min(FIRE_H - 1, y + 1);
                var val = fire[sy * FIRE_W + sx];
                fire[src] = Math.max(0, val - decay);
            }
        }
    };

    FireBadge.prototype._render = function () {
        var imgData = this.ctx.createImageData(FIRE_W, FIRE_H);
        var data = imgData.data;
        for (var i = 0; i < FIRE_W * FIRE_H; i++) {
            var idx = this.fire[i];
            var c = BLUE_FIRE_PALETTE[idx] || BLUE_FIRE_PALETTE[0];
            data[i * 4]     = c[0];
            data[i * 4 + 1] = c[1];
            data[i * 4 + 2] = c[2];
            data[i * 4 + 3] = c[3];
        }
        this.ctx.putImageData(imgData, 0, 0);
    };

    FireBadge.prototype.start = function () {
        if (this.running) return;
        this.running = true;
        var self = this;
        function tick() {
            if (!self.running) return;
            self._spread();
            self._render();
            self.timer = setTimeout(tick, 1000 / FPS);
        }
        tick();
    };

    FireBadge.prototype.stop = function () {
        this.running = false;
        if (this.timer) {
            clearTimeout(this.timer);
            this.timer = null;
        }
    };

    // Auto-init on DOMContentLoaded
    function init() {
        var els = document.querySelectorAll(".rank-S, .fire-badge");
        for (var i = 0; i < els.length; i++) {
            if (!els[i]._fireBadge) {
                els[i]._fireBadge = new FireBadge(els[i]);
                els[i]._fireBadge.start();
            }
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    // Re-init when reviews load dynamically (anime page uses AJAX)
    var observer = new MutationObserver(function (mutations) {
        for (var m = 0; m < mutations.length; m++) {
            var nodes = mutations[m].addedNodes;
            for (var n = 0; n < nodes.length; n++) {
                var node = nodes[n];
                if (node.nodeType !== 1) continue;
                var badges = node.querySelectorAll
                    ? node.querySelectorAll(".rank-S, .fire-badge")
                    : [];
                for (var b = 0; b < badges.length; b++) {
                    if (!badges[b]._fireBadge) {
                        badges[b]._fireBadge = new FireBadge(badges[b]);
                        badges[b]._fireBadge.start();
                    }
                }
            }
        }
    });
    observer.observe(document.body, { childList: true, subtree: true });
})();
