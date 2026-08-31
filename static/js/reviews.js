// Community Reviews page — extracted from the inline <script> of reviews.html,
// plus the dislike-with-reason (anti-bombing) flow.
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
                // Dislike on a gated card is handled by the reason flow below.
                if (!isLike && bar.closest(".rv-card").querySelector(".rv-reason-box")) {
                    busy = false;
                    return;
                }
                // D-rank locked dislike: never send the request.
                if (!isLike && btn.classList.contains("rv-dislike-locked")) {
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

    // ---- Dislike with reason (anti-bombing) ----
    function escHtml(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
        });
    }
    function buildReasonRow(re) {
        var mine = re.my_reason ? ' data-mine="1"' : "";
        var good = !!re.ratio_ok;
        var tag = good
            ? '<span class="rv-reason-tag good"><i class="fas fa-circle-check"></i> Valid — counts</span>'
            : '<span class="rv-reason-tag bad"><i class="fas fa-circle-xmark"></i> Contested — doesn\'t count</span>';
        var remove = re.my_reason ? '<button type="button" class="rv-reason-remove" data-remove-reason title="Remove your dislike">✕ Remove</button>' : "";
        return '<div class="rv-reason-row ' + (good ? "valid" : "contested") + '" data-reason-row="' + re.id + '"' + mine + '>'
            + '<div class="rv-reason-text"><b>' + escHtml(re.username) + '</b> disliked: <span>' + escHtml(re.reason) + '</span></div>'
            + '<div class="rv-reason-meta"><div class="rv-reason-vote" data-reason-id="' + re.id + '">'
            + '<button type="button" class="rv-reason-vbtn" data-kind="like">👍 <span class="rv-rl">' + (re.likes || 0) + '</span></button>'
            + '<button type="button" class="rv-reason-vbtn" data-kind="dislike">👎 <span class="rv-rd">' + (re.dislikes || 0) + '</span></button>'
            + '</div>' + tag + remove + '</div></div>';
    }
    function removeReason(card, bar, reviewId, reviewType) {
        fetch("/api/review/" + reviewId + "/remove-reason", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ review_type: reviewType })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) return;
            var likeBtn = bar.querySelector('[data-kind="like"]');
            var disBtn = bar.querySelector('[data-kind="dislike"]');
            likeBtn.querySelector(".rv-vote-count").textContent = data.likes;
            disBtn.querySelector(".rv-vote-count").textContent = data.dislikes;
            likeBtn.classList.remove("voted-like");
            disBtn.classList.remove("voted-dislike");
            var row = card.querySelector('.rv-reason-row[data-mine="1"]');
            if (row) row.remove();
            var chip = card.querySelector(".rv-contested-chip");
            if (chip) chip.remove();
        });
    }
    function submitReason(box, card, bar, reviewId, reviewType) {
        var input = box.querySelector(".rv-reason-input");
        var errEl = box.querySelector(".rv-reason-err");
        var submitBtn = box.querySelector(".rv-reason-submit");
        var reason = (input.value || "").trim();
        if (reason.length < 2) {
            errEl.textContent = "Please give a short reason for your dislike.";
            return;
        }
        submitBtn.disabled = true;
        fetch("/api/review/" + reviewId + "/dislike-reason", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ review_type: reviewType, reason: reason })
        }).then(function (r) { return r.json(); }).then(function (data) {
            submitBtn.disabled = false;
            if (!data.success) { errEl.textContent = data.error || "Could not submit."; return; }
            var likeBtn = bar.querySelector('[data-kind="like"]');
            var disBtn = bar.querySelector('[data-kind="dislike"]');
            likeBtn.querySelector(".rv-vote-count").textContent = data.likes;
            disBtn.querySelector(".rv-vote-count").textContent = data.dislikes;
            likeBtn.classList.remove("voted-like");
            disBtn.classList.add("voted-dislike");
            var reasons = card.querySelector(".rv-reasons");
            if (reasons && data.reason) {
                var re = Object.assign({}, data.reason, { my_reason: true });
                reasons.insertAdjacentHTML("afterbegin", buildReasonRow(re));
            }
            box.style.display = "none";
            input.value = "";
            errEl.textContent = "";
            box.querySelectorAll(".rv-reason-chip").forEach(function (c) { c.classList.remove("active"); });
        }).catch(function () {
            submitBtn.disabled = false;
            errEl.textContent = "Network error — try again.";
        });
    }

    document.addEventListener("click", function (e) {
        // Dislike button on a gated card -> open the reason box (or remove mine).
        var dbtn = e.target.closest('.rv-vote [data-kind="dislike"]');
        if (dbtn) {
            var bar = dbtn.closest(".rv-vote");
            var card = bar.closest(".rv-card");
            var box = card.querySelector(".rv-reason-box");
            if (box) {
                e.preventDefault();
                var reviewId = bar.dataset.reviewId;
                var reviewType = bar.dataset.reviewType || "anime";
                var myRow = card.querySelector('.rv-reason-row[data-mine="1"]');
                if (myRow) { removeReason(card, bar, reviewId, reviewType); return; }
                var open = box.style.display !== "none";
                $$(".rv-reason-box").forEach(function (b) { if (b !== box) b.style.display = "none"; });
                if (open) { box.style.display = "none"; return; }
                box.style.display = "block";
                var inp = box.querySelector(".rv-reason-input");
                if (inp) inp.focus();
            }
            return;
        }
        // Reason chips: fill the textarea.
        var chip = e.target.closest(".rv-reason-chip");
        if (chip) {
            var boxC = chip.closest(".rv-reason-box");
            var inputC = boxC.querySelector(".rv-reason-input");
            boxC.querySelectorAll(".rv-reason-chip").forEach(function (c) { c.classList.remove("active"); });
            chip.classList.add("active");
            inputC.value = chip.textContent.trim();
            inputC.focus();
            return;
        }
        // Cancel: close the box.
        var cancel = e.target.closest(".rv-reason-cancel");
        if (cancel) {
            var boxX = cancel.closest(".rv-reason-box");
            boxX.style.display = "none";
            boxX.querySelector(".rv-reason-input").value = "";
            boxX.querySelector(".rv-reason-err").textContent = "";
            boxX.querySelectorAll(".rv-reason-chip").forEach(function (c) { c.classList.remove("active"); });
            return;
        }
        // Submit the dislike reason.
        var submit = e.target.closest(".rv-reason-submit");
        if (submit) {
            var boxS = submit.closest(".rv-reason-box");
            var cardS = boxS.closest(".rv-card");
            var barS = cardS.querySelector(".rv-vote");
            submitReason(boxS, cardS, barS, barS.dataset.reviewId, barS.dataset.reviewType || "anime");
            return;
        }
        // Remove my own reason.
        var removeBtn = e.target.closest("[data-remove-reason]");
        if (removeBtn) {
            var cardR = removeBtn.closest(".rv-card");
            var barR = cardR.querySelector(".rv-vote");
            removeReason(cardR, barR, barR.dataset.reviewId, barR.dataset.reviewType || "anime");
            return;
        }
        // Vote on a reason (decides if that dislike counts).
        var rvBtn = e.target.closest(".rv-reason-vbtn");
        if (rvBtn) {
            var voteBar = rvBtn.closest(".rv-reason-vote");
            var rid = voteBar.dataset.reasonId;
            var isLike = rvBtn.dataset.kind === "like";
            fetch("/api/reason/" + rid + "/vote", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ is_like: isLike })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) return;
                voteBar.querySelectorAll(".rv-reason-vbtn").forEach(function (b) {
                    var k = b.dataset.kind;
                    b.classList.toggle("voted", data.user_vote === (k === "like" ? 1 : 0));
                    b.querySelector(k === "like" ? ".rv-rl" : ".rv-rd").textContent = k === "like" ? data.likes : data.dislikes;
                });
                var row = voteBar.closest(".rv-reason-row");
                var good = data.likes > data.dislikes;
                row.classList.toggle("valid", good);
                row.classList.toggle("contested", !good);
                var tag = row.querySelector(".rv-reason-tag");
                if (tag) {
                    tag.className = "rv-reason-tag " + (good ? "good" : "bad");
                    tag.innerHTML = good
                        ? '<i class="fas fa-circle-check"></i> Valid — counts'
                        : '<i class="fas fa-circle-xmark"></i> Contested — doesn\'t count';
                }
            });
            return;
        }
    });

    // ---- Reply War (C+ entry, crowd votes, best ratio wins) ----
    function cardReviewType(card) {
        var share = card.querySelector("[data-share]");
        if (share && (share.getAttribute("data-share") || "").indexOf("/episode/") !== -1) return "episode";
        return "anime";
    }
    function warEntryHtml(we) {
        var av = we.avatar
            ? '<img class="rv-reply-avatar" src="/static/images/avatars/' + escHtml(we.avatar) + '" alt="">'
            : '<span class="rv-reply-avatar" style="background:' + (we.avatar_color || "#374151") + '">' + escHtml((we.username || "?")[0]).toUpperCase() + '</span>';
        return '<div class="rv-war-entry">'
            + av
            + '<span class="rank-badge rank-' + escHtml(we.rank || "D") + '" style="font-size:0.6rem;padding:1px 6px;">' + escHtml(we.rank || "D") + '</span>'
            + '<b class="rv-reply-name">' + escHtml(we.username) + '</b>'
            + '<span class="rv-reply-text">' + escHtml(we.content) + '</span>'
            + '<span class="rv-reply-time">' + escHtml(we.created_at || "") + '</span>'
            + '</div>';
    }
    function refreshWarLeader(card) {
        // Compute the live leader from the arena's current counts: best
        // like-ratio among entries with 3+ votes (tie -> most likes).
        var warEl = card.querySelector(".rv-war");
        var leaderBox = warEl.querySelector(".rv-war-leader");
        var best = null, bestRatio = -1, bestLikes = -1;
        warEl.querySelectorAll(".rv-war-entry").forEach(function (row) {
            var likes = parseInt(row.querySelector(".rv-wl").textContent || "0", 10);
            var dislikes = parseInt(row.querySelector(".rv-wd").textContent || "0", 10);
            var total = likes + dislikes;
            var ratio = total ? likes / total : 0;
            row.classList.remove("leader");
            var crown = row.querySelector(".rv-war-crown");
            if (crown) crown.remove();
            if (total >= 3 && (ratio > bestRatio || (ratio === bestRatio && likes > bestLikes))) {
                best = row; bestRatio = ratio; bestLikes = likes;
            }
        });
        if (best) {
            best.classList.add("leader");
            best.insertAdjacentHTML("beforeend", '<span class="rv-war-crown">👑</span>');
            var name = best.querySelector(".rv-reply-name").textContent;
            var txt = best.querySelector(".rv-reply-text").textContent;
            var rank = "";
            var rb = best.querySelector(".rank-badge");
            if (rb) rank = '<span class="rank-badge ' + rb.className + '" style="font-size:0.6rem;padding:1px 6px;">' + escHtml(rb.textContent) + '</span>';
            leaderBox.className = "rv-war-leader";
            leaderBox.innerHTML = '<span class="rv-war-crown">👑</span> <b>' + escHtml(name) + '</b> ' + rank + ' wins the war: <span class="rv-war-leader-txt">' + escHtml(txt) + '</span>';
        } else {
            leaderBox.className = "rv-war-leader none";
            leaderBox.innerHTML = '<i class="fas fa-hourglass-half"></i> No champion yet — <b>3+ votes</b> crown the best like-ratio take.';
        }
    }
    document.addEventListener("click", function (e) {
        // War toggle: open/close the arena.
        var tgl = e.target.closest(".rv-war-toggle");
        if (tgl) {
            var war = tgl.closest(".rv-war");
            var arena = war.querySelector(".rv-war-arena");
            var open = arena.style.display !== "none";
            arena.style.display = open ? "none" : "block";
            tgl.querySelector(".rv-war-toggle-txt").textContent = open ? "War" : "Close";
            return;
        }
        // Submit a war entry.
        var wbtn = e.target.closest(".rv-war-submit");
        if (wbtn) {
            var comp = wbtn.closest(".rv-war-composer");
            var card = comp.closest(".rv-card");
            var input = comp.querySelector(".rv-war-input");
            var content = (input.value || "").trim();
            if (content.length < 2) { input.focus(); return; }
            wbtn.disabled = true;
            var war = card.querySelector(".rv-war");
            var reviewId = war.getAttribute("data-review-id");
            fetch("/api/review/" + reviewId + "/war", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ review_type: cardReviewType(card), content: content })
            }).then(function (r) { return r.json(); }).then(function (data) {
                wbtn.disabled = false;
                if (!data.success) {
                    var errEl = comp.querySelector(".rv-war-err");
                    if (!errEl) { errEl = document.createElement("p"); errEl.className = "rv-war-err"; comp.appendChild(errEl); }
                    errEl.textContent = data.error || "Could not enter the war.";
                    return;
                }
                input.value = "";
                var errEl = comp.querySelector(".rv-war-err");
                if (errEl) errEl.remove();
                var arena = war.querySelector(".rv-war-arena");
                var empty = arena.querySelector(".rv-war-empty");
                if (empty) empty.remove();
                arena.insertAdjacentHTML("beforeend", warEntryHtml(data.entry));
                var cnt = war.querySelector(".rv-war-count");
                if (cnt) cnt.textContent = (parseInt(cnt.textContent, 10) || 0) + 1 + " battlers";
            }).catch(function () { wbtn.disabled = false; });
            return;
        }
        // Vote on a war entry (the crowd decides).
        var wvb = e.target.closest(".rv-war-vbtn");
        if (wvb) {
            var voteEl = wvb.closest(".rv-war-vote");
            var entryId = voteEl.getAttribute("data-war-entry");
            var isLike = wvb.dataset.kind === "like";
            fetch("/api/war/" + entryId + "/vote", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ is_like: isLike })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) return;
                voteEl.querySelector(".rv-wl").textContent = data.likes;
                voteEl.querySelector(".rv-wd").textContent = data.dislikes;
                voteEl.querySelectorAll(".rv-war-vbtn").forEach(function (b) {
                    b.classList.toggle("voted", data.user_vote === (b.dataset.kind === "like" ? 1 : 0));
                });
                refreshWarLeader(voteEl.closest(".rv-card"));
            });
            return;
        }
    });

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
})();
