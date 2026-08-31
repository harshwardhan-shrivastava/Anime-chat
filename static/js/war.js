// War Zone + War detail — countdowns, voting, live leader, join composer.
(function () {
    "use strict";

    // ---- Countdown: ends-until for every [data-ends] element ----
    function tickCountdowns() {
        var now = Math.floor(Date.now() / 1000);
        var done = false;
        document.querySelectorAll("[data-ends]").forEach(function (el) {
            var ends = parseInt(el.getAttribute("data-ends") || "0", 10);
            var left = ends - now;
            if (left <= 0) {
                if (el.getAttribute("data-big")) done = true;
                el.textContent = "war over";
                return;
            }
            var h = Math.floor(left / 3600);
            var m = Math.floor((left % 3600) / 60);
            var s = left % 60;
            var pad = function (n) { return n < 10 ? "0" + n : String(n); };
            el.textContent = h > 0
                ? h + "h " + pad(m) + "m " + pad(s) + "s"
                : m + "m " + pad(s) + "s";
        });
        if (done) {
            // The war just ended — show the final podium.
            window.location.reload();
            return;
        }
    }
    tickCountdowns();
    setInterval(tickCountdowns, 1000);

    // ---- Detail page only ----
    if (!document.querySelector(".war-page.detail")) return;

    var pathMatch = window.location.pathname.match(/^\/war\/(anime|episode)\//);
    var reviewType = pathMatch ? pathMatch[1] : "anime";
    var reviewIdEl = document.querySelector(".war-page.detail .war-original");
    var reviewId = reviewIdEl ? parseInt(reviewIdEl.getAttribute("data-review-id") || "0", 10) : 0;
    if (!reviewId) {
        var warEl = document.querySelector(".war-entries");
        var first = warEl && warEl.querySelector(".war-entry");
        var m2 = first && first.getAttribute("data-entry-id");
        // Fall back: find the review id from the "All wars" back link? Not
        // needed in practice — the original card always renders.
    }

    // ---- Vote on a war entry ----
    document.addEventListener("click", function (e) {
        var vbtn = e.target.closest(".war-vbtn");
        if (!vbtn) return;
        var voteEl = vbtn.closest(".war-vote");
        var entryId = voteEl.getAttribute("data-entry-id");
        var isLike = vbtn.getAttribute("data-kind") === "like";
        fetch("/api/war/" + entryId + "/vote", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ is_like: isLike })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) {
                if (data.error && data.error.indexOf("war is over") !== -1) window.location.reload();
                return;
            }
            voteEl.querySelector(".war-vl").textContent = data.likes;
            voteEl.querySelector(".war-vd").textContent = data.dislikes;
            voteEl.querySelectorAll(".war-vbtn").forEach(function (b) {
                b.classList.toggle("voted", data.user_vote === (b.getAttribute("data-kind") === "like" ? 1 : 0));
            });
            refreshLeader();
        });
    });

    function entryCounts(el) {
        var likes = parseInt(el.querySelector(".war-vl").textContent || "0", 10);
        var dislikes = parseInt(el.querySelector(".war-vd").textContent || "0", 10);
        var total = likes + dislikes;
        return { likes: likes, dislikes: dislikes, total: total, ratio: total ? likes / total : 0 };
    }

    function refreshLeader() {
        // Best like-ratio among entries with 3+ votes (tie -> more likes).
        var entries = Array.prototype.slice.call(document.querySelectorAll(".war-entry"));
        var best = null, bestRatio = -1, bestLikes = -1;
        entries.forEach(function (row) {
            var c = entryCounts(row);
            row.classList.remove("leader");
            var crown = row.querySelector(".war-entry-head .war-crown");
            if (crown) crown.remove();
            if (c.total >= 3 && (c.ratio > bestRatio || (c.ratio === bestRatio && c.likes > bestLikes))) {
                best = row; bestRatio = c.ratio; bestLikes = c.likes;
            }
            var ratioEl = row.querySelector(".war-entry-ratio b");
            if (ratioEl) ratioEl.textContent = c.total ? Math.round(c.ratio * 100) + "%" : "0%";
            var foot = row.querySelector(".war-entry-foot");
            if (foot) {
                var txt = "Ratio <b>" + (c.total ? Math.round(c.ratio * 100) + "%" : "0%") + "</b> (" + c.likes + "👍 / " + c.dislikes + "👎)";
                var ratioWrap = foot.querySelector(".war-entry-ratio");
                if (ratioWrap) ratioWrap.innerHTML = txt;
            }
        });
        var banner = document.getElementById("warLeaderBanner");
        if (!banner) return;
        if (best) {
            best.classList.add("leader");
            var head = best.querySelector(".war-entry-head");
            if (head) head.insertAdjacentHTML("beforeend", '<span class="war-crown">👑</span>');
            var name = best.querySelector(".war-entry-name").textContent;
            var txt = best.querySelector(".war-entry-text").textContent;
            var rb = best.querySelector(".rank-badge");
            var rank = rb ? '<span class="rank-badge ' + rb.className + '" style="font-size:0.62rem;padding:1px 7px;">' + escHtml(rb.textContent) + "</span>" : "";
            var dev = "";
            var devEl = best.querySelector(".dev-tag");
            if (devEl) dev = '<span class="dev-tag"><i class="fas fa-code"></i> Developer</span>';
            var stEl = best.querySelector(".war-stance");
            var stance = stEl ? ' <span class="war-stance ' + stEl.className.replace("war-stance", "").trim() + '">' + stEl.textContent.trim() + "</span>" : "";
            banner.innerHTML = '<span class="war-crown">👑</span> <b>' + escHtml(name) + "</b> " + rank + dev + stance + " leads with <b>" + Math.round(bestRatio * 100) + "%</b> (" + bestLikes + "👍): <span class=\"war-leader-take\">“" + escHtml(txt) + "”</span>";
        } else {
            banner.innerHTML = '<span class="war-crown">👑</span> No leader yet — <b>3+ votes</b> crown the best like-ratio take.';
        }
    }

    function escHtml(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    // ---- Click the original review box -> the exact review on /reviews ----
    var origBox = document.querySelector(".war-page.detail .war-original");
    if (origBox && reviewId) {
        origBox.classList.add("clickable");
        origBox.addEventListener("click", function (e) {
            if (e.target.closest("a, button")) return;          // History / Share still work
            if (window.getSelection && window.getSelection().toString()) return; // don't steal text select
            window.location.href = "/reviews#rv-" + reviewType + "-" + reviewId;
        });
    }

    // ---- Join the war: your reply (Positive or Negative stance) ----
    var submitBtn = document.querySelector(".war-composer-submit");
    if (submitBtn) {
        // Stance toggle: pick Positive or Negative before posting.
        var stanceBtns = document.querySelectorAll(".war-stance-btn");
        var chosenStance = "positive";
        stanceBtns.forEach(function (b) {
            b.addEventListener("click", function () {
                stanceBtns.forEach(function (x) { x.classList.remove("active"); });
                b.classList.add("active");
                chosenStance = b.getAttribute("data-stance") || "positive";
            });
        });
        submitBtn.addEventListener("click", function () {
            var comp = submitBtn.closest(".war-composer");
            var input = comp.querySelector(".war-composer-input");
            var errEl = comp.querySelector(".war-composer-err");
            var reason = (input.value || "").trim();
            if (reason.length < 2) { errEl.textContent = "Give a short reply — it's your war take."; return; }
            submitBtn.disabled = true;
            fetch("/api/war/" + reviewType + "/" + reviewId + "/enter", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ content: reason, stance: chosenStance })
            }).then(function (r) { return r.json(); }).then(function (data) {
                submitBtn.disabled = false;
                if (!data.success) { errEl.textContent = data.error || "Could not enter the war."; return; }
                window.location.reload();
            }).catch(function () {
                submitBtn.disabled = false;
                errEl.textContent = "Network error — try again.";
            });
        });
    }
})();
