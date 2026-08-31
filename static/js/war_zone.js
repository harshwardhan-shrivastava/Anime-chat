// Standalone War Zone board - countdown, voting, live leader refresh, enter.
(function () {
    "use strict";

    var pathMatch = window.location.pathname.match(/^\/war\/zone\/(\d+)/);
    var warId = pathMatch ? pathMatch[1] : "";

    // ---- Countdown for every [data-ends] element ----
    function tick() {
        var now = Math.floor(Date.now() / 1000);
        var endedBig = false;
        document.querySelectorAll("[data-ends]").forEach(function (el) {
            var ends = parseInt(el.getAttribute("data-ends") || "0", 10);
            var left = ends - now;
            if (left <= 0) {
                if (el.getAttribute("data-big")) endedBig = true;
                el.textContent = "war over";
                return;
            }
            var h = Math.floor(left / 3600);
            var m = Math.floor((left % 3600) / 60);
            var s = left % 60;
            var pad = function (n) { return n < 10 ? "0" + n : String(n); };
            el.textContent = h > 0 ? h + "h " + pad(m) + "m " + pad(s) + "s" : m + "m " + pad(s) + "s";
        });
        if (endedBig) { window.location.reload(); return; }
    }
    if (document.querySelector("[data-ends]")) {
        tick();
        setInterval(tick, 1000);
    }

    // ---- Vote on a battler ----
    document.addEventListener("click", function (e) {
        var vbtn = e.target.closest(".war-vbtn");
        if (!vbtn) return;
        var voteEl = vbtn.closest(".war-vote");
        var entryId = voteEl.getAttribute("data-entry-id");
        var isLike = vbtn.getAttribute("data-kind") === "like";
        fetch("/api/warzone/" + entryId + "/vote", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ is_like: isLike })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) {
                if (data.error && data.error.indexOf("Log in") !== -1) { window.location.href = "/auth"; return; }
                var msg = data.error || "Could not vote.";
                var errEl = document.querySelector(".war-composer-err");
                if (errEl) errEl.textContent = msg;
                else alert(msg);
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
        var banner = document.getElementById("warLeaderBanner");
        var rows = document.querySelectorAll(".war-entry");
        var best = null, bestRatio = -1, bestLikes = -1;
        rows.forEach(function (row) {
            var c = entryCounts(row);
            row.classList.remove("leader");
            var crown = row.querySelector(".war-entry-head .war-crown");
            if (crown) crown.remove();
            if (c.total >= 3 && (c.ratio > bestRatio || (c.ratio === bestRatio && c.likes > bestLikes))) {
                best = row; bestRatio = c.ratio; bestLikes = c.likes;
            }
            var foot = row.querySelector(".war-entry-foot");
            if (foot) {
                var ratioWrap = foot.querySelector(".war-entry-ratio");
                if (ratioWrap) ratioWrap.innerHTML = "Ratio <b>" + (c.total ? Math.round(c.ratio * 100) + "%" : "0%") + "</b> (" + c.likes + "👍 / " + c.dislikes + "👎)";
            }
        });
        if (!banner) return;
        if (best) {
            best.classList.add("leader");
            var head = best.querySelector(".war-entry-head");
            if (head) head.insertAdjacentHTML("beforeend", '<span class="war-crown">👑</span>');
            var name = best.querySelector(".war-entry-name").textContent;
            var txt = best.querySelector(".war-entry-text").textContent;
            banner.innerHTML = '<span class="war-crown">👑</span> <b>' + escHtml(name) + '</b> leads with <b>' + Math.round(bestRatio * 100) + '%</b> (' + bestLikes + '👍): <span class="war-leader-take">"' + escHtml(txt) + '"</span>';
        } else {
            banner.innerHTML = '<span class="war-crown">👑</span> No leader yet — <b>3+ votes</b> crown the best like-ratio battler.';
        }
    }

    function escHtml(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    // ---- Enter the war ----
    var submit = document.querySelector(".war-composer-submit");
    if (submit) {
        submit.addEventListener("click", function () {
            var comp = submit.closest(".war-composer");
            var input = comp.querySelector(".war-composer-input");
            var errEl = comp.querySelector(".war-composer-err");
            var text = (input.value || "").trim();
            if (text.length < 2) { errEl.textContent = "Give your battler a bit more juice."; return; }
            submit.disabled = true;
            fetch("/api/warzone/" + warId + "/enter", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ content: text })
            }).then(function (r) { return r.json(); }).then(function (data) {
                submit.disabled = false;
                if (!data.success) { errEl.textContent = data.error || "Could not enter the war."; return; }
                window.location.reload();
            }).catch(function () {
                submit.disabled = false;
                errEl.textContent = "Network error - try again.";
            });
        });
    }

    // ---- Create-a-war panel (only on the zone index page) ----
    var createToggle = document.getElementById("wzCreateToggle");
    var createPanel = document.getElementById("wzCreatePanel");
    if (createToggle && createPanel) {
        createToggle.addEventListener("click", function () {
            createPanel.classList.toggle("open");
        });
    }
    var createBtn = document.querySelector(".wz-create-submit");
    if (createBtn) {
        createBtn.addEventListener("click", function () {
            var box = document.getElementById("wzCreateBox");
            var title = (box.querySelector(".wz-title").value || "").trim();
            var decl = (box.querySelector(".wz-decl-input").value || "").trim();
            var errEl = box.querySelector(".wz-create-err");
            var hours = parseInt(box.querySelector(".wz-hours").value || "24", 10);
            var isPrivate = box.querySelector(".wz-private").checked;
            var topicType = box.querySelector(".wz-topic").value;
            var animeSlug = (box.querySelector(".wz-anime-slug").value || "").trim();
            var episodeRef = (box.querySelector(".wz-episode-ref").value || "").trim();
            var gifUrl = (box.querySelector(".wz-gif").value || "").trim();
            if (title.length < 3) { errEl.textContent = "Give your war a short title."; return; }
            if (decl.length < 2) { errEl.textContent = "Write a declaration - the position everyone fights over."; return; }
            createBtn.disabled = true;
            fetch("/api/warzone/create", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title: title, declaration: decl, hours: hours, is_private: isPrivate, topic_type: topicType, anime_slug: animeSlug, episode_ref: episodeRef, gif_url: gifUrl })
            }).then(function (r) { return r.json(); }).then(function (data) {
                createBtn.disabled = false;
                if (!data.success) { errEl.textContent = data.error || "Could not create the war."; return; }
                window.location.href = data.url;
            }).catch(function () {
                createBtn.disabled = false;
                errEl.textContent = "Network error - try again.";
            });
        });
        // Open the create panel if ?create=1 (e.g. from a Thread -> War button)
        if (window.location.search.indexOf("create=1") !== -1) {
            var panel = document.getElementById("wzCreatePanel");
            if (panel) {
                panel.classList.add("open");
                panel.scrollIntoView({ behavior: "smooth", block: "center" });
            }
        }
        // Prefill from ?anime=<slug>
        var q = new URLSearchParams(window.location.search);
        var slug = q.get("anime");
        if (slug) {
            var a = document.querySelector(".wz-anime-slug");
            if (a) a.value = slug;
        }
    }
})();