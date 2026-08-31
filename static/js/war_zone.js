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
        var box = document.getElementById("wzCreateBox");
        var modeSel = box.querySelector(".wz-mode-select");
        var guildSel = box.querySelector(".wz-guild-select");

        function showGuildPicker() {
            guildSel.closest("label").style.display = (modeSel.value === "guild" || modeSel.value === "gvg") ? "block" : "none";
        }
        modeSel.addEventListener("change", showGuildPicker);
        // Load the caller's guilds for the guild/GvG modes.
        fetch("/api/warzone/my-guilds").then(function (r) { return r.json(); }).then(function (d) {
            if (!d.success) return;
            var opts = ["<option value=''>Choose a guild…</option>"];
            (d.guilds || []).forEach(function (gl) {
                opts.push("<option value='" + gl.id + "'" + (gl.members >= 50 ? "" : " data-small='1'") + ">" + escHtml(gl.name) + " (" + gl.members + " mem)" + (gl.owner ? " · owner" : "") + "</option>");
            });
            guildSel.innerHTML = opts.join("");
            var hint = box.querySelector(".wz-guild-hint");
            if (hint) hint.textContent = "(50+ members to declare GvG)";
        });

        createBtn.addEventListener("click", function () {
            var title = (box.querySelector(".wz-title").value || "").trim();
            var decl = (box.querySelector(".wz-decl-input").value || "").trim();
            var errEl = box.querySelector(".wz-create-err");
            var hours = parseInt(box.querySelector(".wz-hours").value || "24", 10);
            var isPrivate = box.querySelector(".wz-private").checked;
            var topicType = box.querySelector(".wz-topic").value;
            var animeSlug = (box.querySelector(".wz-anime-slug").value || "").trim();
            var episodeRef = (box.querySelector(".wz-episode-ref").value || "").trim();
            var gifUrl = (box.querySelector(".wz-gif").value || "").trim();
            var mode = modeSel.value;
            var guildId = guildSel.value;
            if (title.length < 3) { errEl.textContent = "Give your war a short title."; return; }
            if (decl.length < 2) { errEl.textContent = "Write a declaration - the position everyone fights over."; return; }
            if ((mode === "guild" || mode === "gvg") && !guildId) { errEl.textContent = "Choose your guild."; return; }
            var endpoint = mode === "guild" ? "/api/warzone/guild" : (mode === "gvg" ? "/api/warzone/gvg" : "/api/warzone/create");
            var payload = { title: title, declaration: decl, hours: hours, is_private: isPrivate, topic_type: topicType, anime_slug: animeSlug, episode_ref: episodeRef, gif_url: gifUrl };
            if (mode !== "friendly") payload.guild_id = guildId ? parseInt(guildId, 10) : null;
            createBtn.disabled = true;
            fetch(endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            }).then(function (r) { return r.json(); }).then(function (data) {
                createBtn.disabled = false;
                if (!data.success) { errEl.textContent = data.error || "Could not create the war."; return; }
                window.location.href = data.url;
            }).catch(function () {
                createBtn.disabled = false;
                errEl.textContent = "Network error - try again.";
            });
        });
        // Default the mode picker to the query hint and open if ?create=1
        if (window.location.search.indexOf("create=1") !== -1) {
            var panel = document.getElementById("wzCreatePanel");
            if (panel) {
                panel.classList.add("open");
                panel.scrollIntoView({ behavior: "smooth", block: "center" });
            }
        }
        // ---- Topic pickers (anime / episode / GIF) ----
        (function initTopicPickers(pbox) {
            var topicSel = pbox.querySelector(".wz-topic");
            var animeSlug = pbox.querySelector(".wz-anime-slug");
            var episodeRef = pbox.querySelector(".wz-episode-ref");
            var gifUrl = pbox.querySelector(".wz-gif");
            var panes = { anime: pbox.querySelector(".wz-pick-anime"), episode: pbox.querySelector(".wz-pick-episode"), gif: pbox.querySelector(".wz-pick-gif") };
            var cards = Array.prototype.slice.call(pbox.querySelectorAll(".wz-topic-card"));
            var selAnime = null; // {slug, title} for the episode picker
            var deb = {};

            function showPane() {
                var t = topicSel.value;
                Object.keys(panes).forEach(function (k) { if (panes[k]) panes[k].classList.toggle("hidden", k !== t); });
                cards.forEach(function (c) { c.classList.toggle("active", c.getAttribute("data-topic") === t); });
            }
            cards.forEach(function (c) {
                c.addEventListener("click", function () {
                    topicSel.value = c.getAttribute("data-topic");
                    topicSel.dispatchEvent(new Event("change"));
                });
            });
            topicSel.addEventListener("change", showPane);
            showPane();

            function clearPick(kind) {
                if (kind === "anime" || kind === "episode") { animeSlug.value = ""; episodeRef.value = ""; }
                if (kind === "gif") { gifUrl.value = ""; }
            }
            function picked(host, html) {
                var p = host.querySelector(".wz-pick-picked");
                p.innerHTML = html || "";
            }
            function pickTag(host, inner) {
                picked(host, "<div class='wz-picker-tag'><span class='wz-pick-check'>✓</span>" + inner + "<button type='button' class='wz-pick-clear' title='Change'>✕</button></div>");
                var clear = host.querySelector(".wz-pick-clear");
                if (clear) {
                    clear.addEventListener("click", function () {
                        clearPick(host.getAttribute("data-kind") || "");
                        picked(host, "");
                        var s = host.querySelector(".wz-pick-search");
                        if (s) { s.value = ""; s.focus(); }
                    });
                }
            }
            function debounce(k, fn, ms) {
                clearTimeout(deb[k]); deb[k] = setTimeout(fn, ms || 300);
            }
            function searchAnime(q, cb) {
                fetch("/api/search?q=" + encodeURIComponent(q)).then(function (r) { return r.json(); }).then(function (d) {
                    var list = (d.success && d.results) ? d.results.map(function (r) {
                        return { slug: r.slug, title: r.title, image: r.image, meta: (r.year || "") + (r.rating ? " · " + r.rating : "") };
                    }) : [];
                    cb(list);
                }).catch(function () { cb([]); });
            }
            function renderAnimeList(box, items, cb) {
                box.innerHTML = "";
                if (!items.length) { box.innerHTML = "<div class='wz-picker-empty'>No results — try another search.</div>"; return; }
                items.forEach(function (it) {
                    var d = document.createElement("div");
                    d.className = "wz-picker-item";
                    d.innerHTML = (it.image ? "<img src='" + escHtml(it.image) + "' onerror='this.style.display=\"none\"'>" : "<div class='wz-picker-noimg'>🎬</div>") +
                        "<div class='wz-picker-main'><div class='wz-picker-title'>" + escHtml(it.title) + "</div>" +
                        "<div class='wz-picker-meta'>" + (it.season != null ? escHtml(it.season) : "") + (it.number ? " E" + it.number : "") + (it.meta ? " " + escHtml(it.meta) : "") + "</div></div>" +
                        "<span class='wz-pick-use'>Pick</span>";
                    d.addEventListener("click", function () { cb(it); });
                    box.appendChild(d);
                });
            }

            // --- Anime pane ---
            panes.anime.setAttribute("data-kind", "anime");
            var aniIn = panes.anime.querySelector(".wz-pick-anime-search");
            aniIn.classList.add("wz-pick-search");
            aniIn.addEventListener("input", function () {
                var q = aniIn.value.trim();
                if (!q) { panes.anime.querySelector(".wz-pick-results").innerHTML = ""; return; }
                debounce("anime", function () {
                    searchAnime(q, function (list) {
                        renderAnimeList(panes.anime.querySelector(".wz-pick-results"), list, function (it) {
                            clearPick("anime");
                            animeSlug.value = it.slug;
                            pickTag(panes.anime, "<img src='" + escHtml(it.image || "") + "' onerror='this.style.display=\"none\"'><span class='wz-pick-title-main'>" + escHtml(it.title) + "</span>");
                            panes.anime.querySelector(".wz-pick-results").innerHTML = "";
                        });
                    });
                });
            });

            // --- Episode pane ---
            panes.episode.setAttribute("data-kind", "episode");
            var selA = panes.episode.querySelector(".wz-pick-sel-anime");
            var epIn = panes.episode.querySelector(".wz-pick-ep-search");
            var epBox = panes.episode.querySelector(".wz-pick-results");
            var epCache = { slug: null, episodes: [] };
            selA.classList.add("wz-pick-search");
            selA.addEventListener("input", function () {
                var q = selA.value.trim();
                if (!q) { epBox.innerHTML = ""; return; }
                debounce("selanime", function () {
                    searchAnime(q, function (list) {
                        renderAnimeList(epBox, list, function (it) {
                            selA.value = it.title;
                            selAnime = { slug: it.slug, title: it.title, image: it.image };
                            epIn.disabled = false;
                            epIn.placeholder = "2) Search S1E1 / title…";
                            epCache = { slug: it.slug, episodes: [] };
                            epBox.innerHTML = "";
                            pickTag(panes.episode, "<img src='" + escHtml(it.image || "") + "' onerror='this.style.display=\"none\"'><span class='wz-pick-title-main'>" + escHtml(it.title) + "</span>");
                            epIn.focus();
                        });
                    });
                });
            });
            epIn.addEventListener("input", function () {
                var q = epIn.value.trim();
                if (!selAnime) { epBox.innerHTML = "<div class='wz-picker-empty'>Pick the anime first.</div>"; return; }
                debounce("ep", function () {
                    function show() {
                        var all = epCache.episodes;
                        var list = q ? all.filter(function (e) {
                            return ("S" + e.season_index + "E" + e.number).indexOf(q.toUpperCase()) !== -1 || (e.title || "").toLowerCase().indexOf(q.toLowerCase()) !== -1 || String(e.global).indexOf(q) !== -1;
                        }) : all;
                        epBox.className = "wz-pick-results wz-ep-grid";
                        epBox.innerHTML = "";
                        if (!list.length) { epBox.innerHTML = "<div class='wz-picker-empty'>No episodes match.</div>"; return; }
                        list.forEach(function (it) {
                            var d = document.createElement("div");
                            d.className = "wz-ep-card";
                            d.innerHTML = (it.image ? "<img src='" + escHtml(it.image) + "' loading='lazy' onerror='this.style.display=\"none\"'>" : "") +
                                "<div class='wz-ep-meta'><b>S" + it.season_index + "E" + it.number + "</b><span>" + (it.title ? escHtml(it.title) : "") + "</span></div>";
                            d.addEventListener("click", function () {
                                clearPick("episode");
                                animeSlug.value = selAnime.slug;
                                episodeRef.value = "S" + it.season_index + "E" + it.number + " " + (it.title || "");
                                pickTag(panes.episode, "<img src='" + escHtml(it.image || "") + "' onerror='this.style.display=\"none\"'><span class='wz-pick-title-main'><b>" + escHtml(selAnime.title) + "</b> · S" + it.season_index + "E" + it.number + (it.title ? " — " + escHtml(it.title) : "") + "</span>");
                                epBox.innerHTML = "";
                            });
                            epBox.appendChild(d);
                        });
                    }
                    if (epCache.episodes.length) { show(); return; }
                    epBox.className = "wz-pick-results";
                    epBox.innerHTML = "<div class='wz-picker-empty'>Loading episodes…</div>";
                    fetch("/api/warzone/episodes?slug=" + encodeURIComponent(selAnime.slug)).then(function (r) { return r.json(); }).then(function (d) {
                        epCache.episodes = (d.success && d.episodes) ? d.episodes : [];
                        show();
                    }).catch(function () { epBox.innerHTML = "<div class='wz-picker-empty'>Couldn't load episodes.</div>"; });
                });
            });

            // --- GIF pane ---
            panes.gif.setAttribute("data-kind", "gif");
            var gifIn = panes.gif.querySelector(".wz-pick-gif-search");
            gifIn.classList.add("wz-pick-search");
            function loadGifs(q) {
                fetch("/api/gif-search" + (q ? "?q=" + encodeURIComponent(q) : "")).then(function (r) { return r.json(); }).then(function (d) {
                    var box = panes.gif.querySelector(".wz-pick-results");
                    box.className = "wz-pick-results wz-gif-grid";
                    var list = (d.success && d.gifs) ? d.gifs : (d.results || []);
                    box.innerHTML = "";
                    if (!list.length) { box.innerHTML = "<div class='wz-picker-empty'>No GIFs found.</div>"; return; }
                    list.forEach(function (gif) {
                        var url = gif.url || (gif.images && gif.images.fixed_height && gif.images.fixed_height.url);
                        var prev = gif.preview || (gif.images && gif.images.fixed_width_small && gif.images.fixed_width_small.url) || url;
                        if (!url) return;
                        var cell = document.createElement("div");
                        cell.className = "wz-gif-cell";
                        var img = document.createElement("img");
                        img.src = prev; img.alt = gif.title || "gif"; img.loading = "lazy";
                        cell.appendChild(img);
                        var ov = document.createElement("span");
                        ov.className = "wz-gif-ov"; ov.textContent = "+ Pick";
                        cell.appendChild(ov);
                        cell.addEventListener("click", function () {
                            clearPick("gif");
                            gifUrl.value = url;
                            pickTag(panes.gif, "<img src='" + escHtml(prev) + "'><span class='wz-pick-title-main'>GIF selected</span>");
                            gifIn.value = ""; panes.gif.querySelector(".wz-pick-results").innerHTML = "";
                        });
                        box.appendChild(cell);
                    });
                }).catch(function () { panes.gif.querySelector(".wz-pick-results").innerHTML = "<div class='wz-picker-empty'>Couldn't reach GIF search.</div>"; });
            }
            gifIn.addEventListener("focus", function () { if (!gifIn.value.trim()) loadGifs(""); });
            gifIn.addEventListener("input", function () { var q = gifIn.value.trim(); debounce("gif", function () { loadGifs(q); }, 350); });
        })(box);

        // Prefill from ?anime=<slug>
        var q = new URLSearchParams(window.location.search);
        var slug = q.get("anime");
        if (slug) {
            var a = box.querySelector(".wz-anime-slug");
            if (a) a.value = slug;
        }
        showGuildPicker();
    }

    // ---- GvG declared: load your guilds into the claim picker ----
    document.querySelectorAll(".wz-claim-guild").forEach(function (sel) {
        fetch("/api/warzone/my-guilds").then(function (r) { return r.json(); }).then(function (d) {
            if (!d.success) return;
            var opts = ["<option value=''>Your guilds…</option>"];
            (d.guilds || []).forEach(function (gl) {
                opts.push("<option value='" + gl.id + "'" + (gl.owner ? "" : " disabled") + ">" + escHtml(gl.name) + " (" + gl.members + ")" + (gl.owner ? " · owner" : "") + "</option>");
            });
            sel.innerHTML = opts.join("");
        });
        var claimWrap = sel.closest(".wz-claim-ctrl");
        var claimBtn = claimWrap ? claimWrap.querySelector(".wz-claim-btn") : null;
        if (claimBtn) {
            sel.addEventListener("change", function () {
                claimBtn.setAttribute("data-guild", sel.value);
            });
        }
    });

    // ---- GvG: claim a declaration + owner picks a rival ----
    document.addEventListener("click", function (e) {
        var claimBtn = e.target.closest(".wz-claim-btn");
        if (claimBtn) {
            var warId = claimBtn.getAttribute("data-war");
            var guildId = claimBtn.getAttribute("data-guild");
            if (!guildId) { alert("Pick your guild first."); return; }
            fetch("/api/warzone/" + warId + "/claim", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ guild_id: parseInt(guildId, 10) })
            }).then(function (r) { return r.json(); }).then(function (d) {
                if (!d.success) { alert(d.error || "Could not claim."); return; }
                window.location.reload();
            });
            return;
        }
        var pickBtn = e.target.closest(".wz-pick-btn");
        if (pickBtn) {
            var warId2 = pickBtn.getAttribute("data-war");
            var guildId2 = pickBtn.getAttribute("data-guild");
            fetch("/api/warzone/" + warId2 + "/pick", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ guild_id: parseInt(guildId2, 10) })
            }).then(function (r) { return r.json(); }).then(function (d) {
                if (!d.success) { alert(d.error || "Could not pick."); return; }
                window.location.href = d.url;
            });
            return;
        }
    });
})();