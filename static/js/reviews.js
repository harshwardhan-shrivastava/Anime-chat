// Community Reviews page — extracted from the inline <script> of reviews.html.
// Dislikes are plain C+ votes (D can only like green/neutral); the Reply War handles replies.
(function () {
    "use strict";
    function $(s){ return document.querySelector(s); }
    function $$(s, root){ return Array.prototype.slice.call((root || document).querySelectorAll(s)); }

    // ---- Tabs ----
    $$(".rv-tab").forEach(function (tab) {
        tab.addEventListener("click", function () {
            $$(".rv-tab").forEach(function (t) { t.classList.remove("active"); });
            tab.classList.add("active");
            var name = tab.getAttribute("data-rtab");
            activeKind = name;
            $$(".rv-feed").forEach(function (p) { p.style.display = "none"; });
            var pane = $("#rvtab-" + name);
            if (pane) {
                pane.style.display = "flex";
                pane.scrollIntoView({ behavior: "smooth", block: "start" });
            }
            applyFilter();
        });
    });

    // ---- Expand long comments ----
    $$(".rv-comment").forEach(function (p) {
        if (p.scrollHeight <= 96 && p.textContent.length <= 220) return;
        p.classList.add("clamp", "clamped");
    });
    document.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-more]");
        if (!btn) return;
        var wrap = btn.previousElementSibling;
        if (wrap && wrap.classList && wrap.classList.contains("rv-comment")) {
            var open = !wrap.classList.contains("clamp");
            wrap.classList.toggle("clamp", !open);
            btn.textContent = open ? "Show less" : "Show more";
        }
    });

    // ---- Search + sort ----
    var activeKind = "anime";
    function activePane() { return $("#rvtab-" + activeKind); }
    function applyFilter() {
        var pane = activePane();
        var q = ($("#rvSearch").value || "").toLowerCase().trim();
        var sort = $("#rvSort").value;
        var cards = $$(".rv-card", pane);
        cards.forEach(function (c) {
            var hit = !q || (c.dataset.title || "").indexOf(q) !== -1 || (c.dataset.user || "").indexOf(q) !== -1;
            c.style.display = hit ? "" : "none";
        });
        var visible = cards.filter(function (c) { return c.style.display !== "none"; });
        if (sort === "new") {
            visible.sort(function (a, b) { return (b.dataset.created || "").localeCompare(a.dataset.created || ""); });
        } else if (sort === "rated") {
            visible.sort(function (a, b) { return (parseInt(b.dataset.votes,10)||0) - (parseInt(a.dataset.votes,10)||0); });
        } else {
            visible.sort(function (a, b) { return (parseInt(a.dataset.rank,10)||5) - (parseInt(b.dataset.rank,10)||5); });
        }
        visible.forEach(function (c) { pane.appendChild(c); });
    }
    $("#rvSearch").addEventListener("input", applyFilter);
    $("#rvSort").addEventListener("change", applyFilter);

    // ---- Optimistic voting ----
    $$(".rv-vote").forEach(function (bar) {
        var reviewId = bar.dataset.reviewId;
        var reviewType = bar.dataset.reviewType || "anime";
        var busy = false;
        bar.querySelectorAll(".gvote").forEach(function (btn) {
            btn.addEventListener("click", function () {
                if (busy) return; busy = true;
                var isLike = btn.dataset.kind === "like";
                // Locked vote buttons (server-rendered per rank/band): never
                // send the request — dislikes and RED-review likes stay locked
                // below C rank; D can still like green/neutral reviews.
                if (btn.classList.contains("rv-like-locked") || btn.classList.contains("rv-dislike-locked")) {
                    busy = false;
                    return;
                }
                var likeBtn = bar.querySelector('[data-kind="like"]');
                var disBtn = bar.querySelector('[data-kind="dislike"]');
                var prevLike = likeBtn.classList.contains("voted-like");
                var prevDislike = disBtn.classList.contains("voted-dislike");
                var prevLikeC = parseInt(likeBtn.querySelector(".rv-vote-count").textContent||"0",10);
                var prevDisC = parseInt(disBtn.querySelector(".rv-vote-count").textContent||"0",10);
                var wasActive = isLike ? prevLike : prevDislike;
                bar.querySelectorAll(".gvote").forEach(function (b) {
                    b.classList.remove("voted-like","voted-dislike");
                    if (!wasActive && b.dataset.kind === (isLike?"like":"dislike")) b.classList.add(isLike?"voted-like":"voted-dislike");
                });
                var lD=0, dD=0;
                if (wasActive) { if(isLike) lD=-1; else dD=-1; }
                else { if(prevLike) lD=-1; if(prevDislike) dD=-1; if(isLike) lD+=1; else dD+=1; }
                likeBtn.querySelector(".rv-vote-count").textContent = Math.max(0,prevLikeC+lD);
                disBtn.querySelector(".rv-vote-count").textContent = Math.max(0,prevDisC+dD);
                btn.querySelector(".vote-emoji").style.animation = "rvPop 0.3s ease";
                fetch("/api/review/" + reviewId + "/vote", {
                    method:"POST", headers:{"Content-Type":"application/json"},
                    body: JSON.stringify({ is_like: isLike, review_type: reviewType })
                }).then(function(r){return r.json();}).then(function(data){
                    if (!data.success) { revert(); return; }
                    bar.querySelectorAll(".gvote").forEach(function(b){
                        var kind=b.dataset.kind; var active=data.user_vote===(kind==="like"?1:0);
                        b.classList.toggle("voted-like", kind==="like"&&active);
                        b.classList.toggle("voted-dislike", kind==="dislike"&&active);
                        b.querySelector(".rv-vote-count").textContent = kind==="like"?data.likes:data.dislikes;
                    });
                }).catch(revert).finally(function(){busy=false;});
                function revert(){
                    likeBtn.querySelector(".rv-vote-count").textContent=prevLikeC;
                    disBtn.querySelector(".rv-vote-count").textContent=prevDisC;
                    bar.querySelectorAll(".gvote").forEach(function(b){
                        var k=b.dataset.kind;
                        b.classList.toggle("voted-like", k==="like"&&prevLike);
                        b.classList.toggle("voted-dislike", k==="dislike"&&prevDislike);
                    });
                }
            });
        });
    });

    function escHtml(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    // ---- Deep link from a war page: #rv-<kind>-<id> opens the right tab
    // and scrolls to + flashes that exact review so the war context is clear ----
    (function () {
        var m = (location.hash || "").match(/^#rv-(anime|episode)-(\d+)$/);
        if (!m) return;
        var kind = m[1], id = m[2];
        var tab = document.querySelector('.rv-tab[data-rtab="' + kind + '"]');
        if (tab) tab.click();
        var card = document.getElementById("rv-" + kind + "-" + id);
        if (!card) return;
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("war-jump");
        setTimeout(function () { card.classList.remove("war-jump"); }, 3600);
    })();
    // ---- Share (copy link) ----
    document.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-share]");
        if (!btn) return;
        var url = window.location.origin + btn.getAttribute("data-share");
        var done = function(){ var old=btn.innerHTML; btn.innerHTML='<i class="fas fa-check"></i> Copied!'; setTimeout(function(){btn.innerHTML=old;},1600); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(done, function(){ /* below */ });
        }
        var ta = document.createElement("textarea"); ta.value=url; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); done(); } catch(e){}
        document.body.removeChild(ta);
    });

    // ---- Reviewer profile modal (Threads-style) ----
    function rvEsc(s) {
        return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function rvFmtJoinDate(s) {
        if (!s) return "";
        var m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (!m) return s;
        var months = ["January","February","March","April","May","June","July","August","September","October","November","December"];
        var day = parseInt(m[3], 10);
        var ord = (day % 10 === 1 && day !== 11) ? "st" : (day % 10 === 2 && day !== 12) ? "nd" : (day % 10 === 3 && day !== 13) ? "rd" : "th";
        return months[parseInt(m[2], 10) - 1] + " " + day + ord + ", " + m[1];
    }

    function rvFillProfile(res) {
        var u = res.user || {};
        var av = document.getElementById("rvProfAvatar");
        if (av) {
            if (u.avatar) {
                av.innerHTML = '<img src="/static/images/avatars/' + encodeURIComponent(u.avatar) + '" alt="">';
                av.style.background = "none";
            } else {
                av.innerHTML = "";
                av.textContent = (u.username || "?").charAt(0).toUpperCase();
                av.style.background = "linear-gradient(135deg," + (u.avatar_color || "#64748b") + ",#475569)";
            }
        }
        document.getElementById("rvProfName").textContent = "@" + (u.username || "unknown");
        document.getElementById("rvProfSub").textContent = res.rank ? res.rank + " Rank" : "";
        var badge = document.getElementById("rvProfBadge");
        badge.innerHTML = res.rank
            ? '<span class="rank-badge rank-' + rvEsc(res.rank) + '"><span class="rank-text">' + rvEsc(res.rank) + "</span></span>"
            : "";
        var xp = parseInt(res.xp || 0, 10) || 0;
        document.getElementById("rvProfXp").textContent = xp.toLocaleString() + " XP";
        var pct = Math.max(0, Math.min(100, parseInt(res.xp_pct || 0, 10) || 0));
        document.getElementById("rvProfPct").textContent = pct + "%";
        document.getElementById("rvProfXpFill").style.width = pct + "%";
        document.getElementById("rvProfJoined").innerHTML = '<i class="fas fa-calendar"></i> Joined ' + (rvFmtJoinDate(res.joined_at) || "unknown");
        var g = document.getElementById("rvProfGuilds");
        var guilds = res.guilds || [];
        if (!guilds.length) {
            g.innerHTML = '<span class="rv-prof-guild-empty">No guilds yet</span>';
        } else {
            g.innerHTML = guilds.map(function (x) {
                return '<span class="rv-prof-guild"><i class="fas fa-hashtag"></i> ' + rvEsc(x.name || "?") +
                    (x.role === "owner" ? ' <span class="rv-prof-guild-owner">OWNER</span>' : "") + "</span>";
            }).join("");
        }
        document.getElementById("rvProfHistory").href = "/user/" + encodeURIComponent(u.username || "") + "/history";
    }

    function rvOpenProfile(uid) {
        fetch("/threads/api/users/" + encodeURIComponent(uid) + "/profile", { credentials: "include" })
            .then(function (r) {
                if (!r.ok) throw new Error("http " + r.status);
                return r.json();
            })
            .then(function (res) {
                if (!res || !res.success) throw new Error("bad payload");
                rvFillProfile(res);
                var ov = document.getElementById("rvProfOverlay");
                ov.classList.remove("hidden");
                document.body.style.overflow = "hidden";
            })
            .catch(function () {
                alert("Could not load this profile.");
            });
    }

    function rvCloseProfile() {
        var ov = document.getElementById("rvProfOverlay");
        if (!ov) return;
        ov.classList.add("hidden");
        document.body.style.overflow = "";
    }

    document.addEventListener("click", function (e) {
        var trig = e.target.closest("[data-profile-uid]");
        if (trig) {
            e.preventDefault();
            var uid = trig.getAttribute("data-profile-uid");
            if (uid) rvOpenProfile(uid);
            return;
        }
        if (e.target.closest("#rvProfClose")) { rvCloseProfile(); return; }
        if (e.target === document.getElementById("rvProfOverlay")) rvCloseProfile();
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && document.getElementById("rvProfOverlay") &&
            !document.getElementById("rvProfOverlay").classList.contains("hidden")) {
            rvCloseProfile();
        }
    });
})();
