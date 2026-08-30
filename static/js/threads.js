// ============================================================
// THREADS — Messages tab (Phase 1)
// Full-screen chat client for Otakul. Pure vanilla JS, no
// build step. Polls the /threads/api/* endpoints (the app has
// no websockets), mirrors the legacy chat's polling approach.
// ============================================================
(function () {
    "use strict";

    // ----------------------------------------------------------
    // State
    // ----------------------------------------------------------
    var State = {
        me: null,
        conversations: [],
        active: null,          // {type, id, conv}
        messages: [],
        loadingHistory: false,  // suppresses auto-scroll during initial load
        seenIds: {},
        afterId: 0,
        firstId: 0,
        hasMore: true,
        loadingOlder: false,
        reqSeq: 0,             // incremented per conversation switch; stale responses are dropped
        newSinceId: 0,         // server read-marker at open time -> drives the "New messages" divider
        members: [],
        memberMap: {},         // id -> member row
        presence: {},          // id -> {status, online}
        settings: { read_receipts: true, typing_indicators: true },
        replyTo: null,
        attach: null,          // {kind, url, preview, name}
        pendingAnime: null,    // {slug, title, image, year, rating}
        editingId: null,
        typingSentAt: 0,
        notifUnread: 0,
        convFilter: "",
        lastSeenText: "",
        reqCount: 0,
        reqIncoming: [],
        reqOutgoing: [],
        activeTab: "messages",
        communities: [],
        activeCommunity: null,
        discoverMode: false,
        discoverList: [],
        commFilter: "",
        polls: [],
        parties: [],
        communityDetail: null,
        myCommunityRole: "member",
    };

    // Per-conversation message cache: reopening a chat renders instantly from
    // here (like community chat) and only polls for newer messages.
    var msgCache = {};

    // ----------------------------------------------------------
    // Tiny helpers
    // ----------------------------------------------------------
    function $(sel) { return document.querySelector(sel); }
    function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

    var _escDiv = document.createElement("div");
    function escapeHtml(s) {
        // Fast path: reuse a single cached div element instead of creating
        // a new one per call (the old version created 200+ elements per
        // message-list render, which was the main FPS bottleneck).
        _escDiv.textContent = s == null ? "" : String(s);
        return _escDiv.innerHTML;
    }

    function parseIso(iso) {
        if (!iso) return null;
        var d = new Date(String(iso).replace(" ", "T") + "Z");
        return isNaN(d.getTime()) ? null : d;
    }

    function fmtClock(iso) {
        var d = parseIso(iso);
        if (!d) return "";
        return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    }

    function fmtConvTime(iso) {
        var d = parseIso(iso);
        if (!d) return "";
        var now = new Date();
        var sameDay = d.toDateString() === now.toDateString();
        if (sameDay) return fmtClock(iso);
        var yesterday = new Date(now);
        yesterday.setDate(now.getDate() - 1);
        if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
        return d.toLocaleDateString([], { month: "short", day: "numeric" });
    }

    function dayKey(iso) {
        var d = parseIso(iso);
        return d ? d.toDateString() : "";
    }

    function fmtDay(iso) {
        var d = parseIso(iso);
        if (!d) return "";
        var now = new Date();
        if (d.toDateString() === now.toDateString()) return "Today";
        return d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
    }

    function initials(name) {
        return String(name || "?").slice(0, 2).toUpperCase();
    }

    // Inner content of an avatar circle: the user's profile picture when
    // they have one, otherwise their initials.
    function avatarInner(user) {
        user = user || {};
        if (user.avatar) {
            return '<img class="thr-avatar-img" src="/static/images/avatars/' +
                escapeHtml(user.avatar) + '" alt="">';
        }
        return escapeHtml(initials(user.username));
    }

    // ---- Rank badge + XP (shared with community chat) ----
    var _thrRankCache = {};

    function thrXpProgressPct(xp) {
        var ranges = { F: [-999,0], D: [0,500], C: [500,1000], B: [1000,2000], A: [2000,5000], S: [5000,15000], "S+": [15000,15000] };
        var tier = (xp >= 15000) ? "S+" : (xp >= 5000) ? "S" : (xp >= 2000) ? "A" : (xp >= 1000) ? "B" : (xp >= 500) ? "C" : (xp >= 0) ? "D" : "F";
        var lo = ranges[tier][0], hi = ranges[tier][1];
        if (hi <= lo) return 100;
        return Math.min(100, Math.max(0, Math.round((xp - lo) / (hi - lo) * 100)));
    }
    function thrRankBadgeHtml(userId, rank, xp) {
        if (!rank) return "";
        var xpVal = (xp != null) ? xp : (_thrRankCache[userId] ? _thrRankCache[userId].xp : 0);
        var pct = thrXpProgressPct(xpVal || 0);
        return '<span class="rank-badge rank-' + rank + '" style="font-size:1rem;padding:4px 18px;letter-spacing:2px;">' + rank + '</span><span class="xp-bar rank-' + rank + '"><span class="xp-bar-fill" style="width:' + pct + '%"></span><span class="xp-bar-text">' + (xpVal||0).toLocaleString() + ' XP</span></span>';
    }

    function fetchThrRanks(messages) {
        var ids = [];
        messages.forEach(function (m) {
            var uid = m.sender && m.sender.id;
            if (uid && !_thrRankCache[uid]) ids.push(uid);
        });
        if (!ids.length) return Promise.resolve(0);
        return api("/api/user-ranks", { json: { user_ids: ids } }).then(function (data) {
            var fetched = 0;
            if (data && data.ranks) {
                Object.keys(data.ranks).forEach(function (uid) {
                    _thrRankCache[uid] = data.ranks[uid];
                    fetched++;
                });
            }
            return fetched;
        }).catch(function () { return 0; });
    }

    function toast(msg, type) {
        var box = $("#thrToast");
        box.textContent = msg;
        box.className = "thr-toast show thr-toast-" + (type || "success");
        clearTimeout(box._t);
        box._t = setTimeout(function () { box.classList.remove("show"); }, 3000);
    }

    function api(path, opts) {
        opts = opts || {};
        if (opts.json !== undefined) {
            opts.method = opts.method || "POST";
            opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
            opts.body = JSON.stringify(opts.json);
        }
        opts.credentials = "include";
        return fetch(path, opts).then(function (r) {
            if (!r.ok) {
                if (r.status === 401 || r.status === 403) {
                    window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
                    return Promise.reject(new Error("auth"));
                }
                throw new Error("HTTP " + r.status);
            }
            return r.json();
        }).catch(function (err) {
            if (err && err.message === "auth") throw err;
            console.error("API error:", path, err);
            throw err;
        });
    }

    function handleApiError(res) {
        if (res && res.error === "login") {
            window.location.href = "/login?next=" + encodeURIComponent("/threads");
            return true;
        }
        if (res && res.error && res.error !== "login") {
            toast(res.error === "not_member" ? "You don't have access to that." : res.error, "error");
            return true;
        }
        return false;
    }

    function memberById(id) { return State.memberMap[id] || null; }

    function meIsOwner() {
        var me = memberById(State.me.id);
        return me && (me.role === "owner" || me.role === "admin");
    }

    // ----------------------------------------------------------
    // Conversation list
    // ----------------------------------------------------------
    function convDisplayName(c) {
        if (c.type === "dm") return c.other ? c.other.username : "Unknown";
        return c.name || "Group";
    }

    function convSubtitle(c) {
        if (c.type === "group") {
            var n = (c.members || []).length;
            return n + (n === 1 ? " member" : " members");
        }
        return "Direct message";
    }

    function convPreview(c) {
        var lm = c.last_message || {};
        if (!lm.content && !lm.kind) return "No messages yet";
        if (lm.kind === "gif") return "\uD83C\uDF81 GIF";
        if (lm.kind === "anime") return "\uD83D\uDCFA Anime";
        if (lm.kind === "image") return "\uD83D\uDDBC Image";
        if (lm.kind === "video") return "\uD83C\uDFA5 Video";
        var prefix = "";
        if (c.type === "group" && lm.sender_id && lm.sender_id !== State.me.id) {
            var s = State.convMemberName(c, lm.sender_id);
            prefix = s ? s + ": " : "";
        }
        var text = lm.content || "";
        return prefix + (text.length > 60 ? text.slice(0, 60) + "…" : text);
    }

    function convAvatarHtml(c, size) {
        var sizeCls = size || "";
        if (c.type === "dm") {
            var other = c.other || {};
            return '<span class="thr-avatar ' + sizeCls + '" style="background:' +
                escapeHtml(other.avatar_color || "#8b5cf6") + '">' +
                avatarInner(other) + "</span>";
        }
        return '<span class="thr-avatar ' + sizeCls + '" style="background:' +
            escapeHtml(c.avatar_color || "#8b5cf6") + '">' +
            escapeHtml(initials(c.name)) + "</span>";
    }

    function renderConversations() {
        var list = $("#convList");
        var filter = State.convFilter.toLowerCase();
        var html = "";
        var unreadTotal = 0;
        State.conversations.forEach(function (c) {
            var name = convDisplayName(c).toLowerCase();
            if (filter && name.indexOf(filter) === -1) return;
            var isActive = State.active && State.active.id === c.id && State.active.type === c.type;
            unreadTotal += c.unread || 0;
            var pres = "";
            if (c.type === "dm" && c.other) {
                var p = State.presence[c.other.id];
                if (p) pres = '<span class="thr-dot ' + (p.online ? "online" : p.status === "away" ? "away" : "offline") + '"></span>';
            }
            html +=
                '<div class="thr-conv' + (isActive ? " active" : "") + '" data-id="' + c.id + '" data-type="' + c.type + '">' +
                '<div class="thr-conv-avatar-wrap">' + convAvatarHtml(c, "thr-avatar-md") + pres + "</div>" +
                '<div class="thr-conv-mid">' +
                '<div class="thr-conv-top"><span class="thr-conv-name">' + escapeHtml(convDisplayName(c)) + "</span>" +
                '<span class="thr-conv-time">' + fmtConvTime(c.last_activity_at) + "</span></div>" +
                '<div class="thr-conv-bottom"><span class="thr-conv-preview">' + escapeHtml(convPreview(c)) + "</span>" +
                (c.muted ? '<i class="fas fa-bell-slash thr-muted-icon" title="Muted"></i>' : "") +
                (c.unread ? '<span class="thr-unread-badge">' + (c.unread > 99 ? "99+" : c.unread) + "</span>" : "") +
                "</div></div></div>";
        });
        list.innerHTML = html || '<div class="thr-conv-empty">' +
            (filter ? "No conversations match." : "No conversations yet — start one!") + "</div>";

        var unread = unreadTotal;
        document.title = unread > 0 ? "(" + unread + ") Threads | Otakul" : "Threads | Otakul";
    }

    // State.convMemberName: name a member of a group conversation
    State.convMemberName = function (c, uid) {
        var m = (c.members || []).filter(function (x) { return x.id === uid; })[0];
        return m ? m.username : null;
    };

    function refreshConversations() {
        api("/threads/api/conversations").then(function (res) {
            if (res.success) {
                var prevActive = State.active;
                State.conversations = res.conversations;
                renderConversations();
                if (prevActive) {
                    // keep the active conversation object fresh (mute, unread)
                    var fresh = null;
                    State.conversations.forEach(function (c) {
                        if (c.id === prevActive.id && c.type === prevActive.type) fresh = c;
                    });
                    if (fresh) State.active = { type: fresh.type, id: fresh.id, conv: fresh };
                    else { /* conversation gone (left group) */ }
                }
            }
        });
    }

    // ----------------------------------------------------------
    // Opening a conversation
    // ----------------------------------------------------------
    function openConversation(type, id) {
        var conv = null;
        State.conversations.forEach(function (c) {
            if (c.id === id && c.type === type) conv = c;
        });
        if (!conv) {
            // Try to fetch a fresh list (e.g. freshly created DM)
            api("/threads/api/conversations").then(function (res) {
                if (res.success) {
                    State.conversations = res.conversations;
                    renderConversations();
                    conv = null;
                    State.conversations.forEach(function (c) {
                        if (c.id === id && c.type === type) conv = c;
                    });
                    if (conv) openConversation(type, id);
                }
            });
            return;
        }
        State.active = { type: type, id: id, conv: conv };
        State.replyTo = null;
        State.attach = null;
        State.editingId = null;
        State.reqSeq++;
        var seq = State.reqSeq;
        var key = type + ":" + id;
        // Read marker captured BEFORE we mark anything read -> drives the
        // red "New messages" divider for everything that arrived since the
        // user last left this conversation.
        State.newSinceId = conv.last_read_message_id || 0;
        State.messages = [];
        State.seenIds = {};
        State.afterId = 0;
        State.firstId = 0;
        State.hasMore = true;
        State.loadingOlder = false;

        // CRITICAL: clear the DOM immediately so old messages from the
        // previous context don't flash before loadHistory resolves.
        $("#msgList").innerHTML = '';
        $("#emptyState").classList.add("hidden");
        $("#convView").classList.remove("hidden");
        renderChatHead();

        var cached = msgCache[key];
        if (cached && cached.messages.length) {
            // Instant render from cache — no spinner, no full refetch.
            State.messages = cached.messages;
            State.messages.forEach(function (m) { State.seenIds[m.id] = true; });
            State.afterId = cached.afterId;
            State.firstId = cached.firstId;
            State.hasMore = cached.hasMore;
            State.members = cached.members || [];
            State.memberMap = {};
            State.members.forEach(function (m) { State.memberMap[m.id] = m; });
            State.pins = cached.pins || [];
            State.polls = cached.polls || [];
            State.parties = cached.parties || [];
            // Restore rendered HTML instantly — no rebuild needed.
            // This is how community chat works: messages are already in the DOM.
            if (cached.html) {
                var _ml = $("#msgList");
                _ml.innerHTML = cached.html;
                _ml.scrollTop = _ml.scrollHeight;
                updateSeenText();
                // If the cached HTML was saved before rank badges loaded,
                // fetch them now and re-render so badges/XP never disappear.
                fetchThrRanks(State.messages).then(function (n) {
                    if (n > 0 && seq === State.reqSeq && $("#msgList") && $("#msgList").innerHTML) {
                        renderMessages(false);
                    }
                });
            } else {
                if (seq === State.reqSeq) {
                    renderMessages(true);
                    renderPins(State.pins);
                    updateSeenText();
                }
                fetchThrRanks(State.messages).then(function () {
                    if (seq === State.reqSeq && $("#msgList") && $("#msgList").innerHTML) {
                        renderMessages(false);
                    }
                });
            }
            pollMessages();          // only fetches messages newer than afterId
            markActiveRead();
            $("#msgInput").focus();
            return;
        }

        State.loadingHistory = true;
        loadHistory();
        markActiveRead();
        $("#msgInput").focus();
    }

    function isChannelOpen() { return State.active && State.active.type === "channel"; }

    function isCommMod() {
        return State.myCommunityRole === "owner" || State.myCommunityRole === "moderator";
    }

    function renderChatHead() {
        var conv = State.active.conv;
        var activeType = State.active && State.active.type;

        // ---- Channel (Guilds tab) ----
        if (activeType === "channel") {
            var comm = State.activeCommunity;
            $("#chatAvatar").innerHTML = "#";
            $("#chatAvatar").style.background = (comm && comm.icon_color) || "#8b5cf6";
            $("#chatName").textContent = "#" + (conv.name || "channel");
            $("#chatSub").textContent = conv.topic || (comm ? comm.name : "");
            $("#chatPresence").className = "thr-presence-dot";
            $("#btnMute").style.display = "none";
            $("#btnMembers").style.display = "";
            $("#btnParty").classList.remove("hidden");
            $("#btnNewPoll").classList.remove("hidden");
            $("#seenText").textContent = "";
            return;
        }

        $("#btnParty").classList.add("hidden");
        $("#btnNewPoll").classList.add("hidden");
        $("#btnMute").style.display = "";

        var isDm = activeType === "dm";
        var name = convDisplayName(conv);
        $("#chatAvatar").innerHTML = isDm
            ? avatarInner(conv.other || {})
            : escapeHtml(initials(conv.name));
        $("#chatAvatar").style.background = isDm
            ? (conv.other ? conv.other.avatar_color : "#8b5cf6")
            : (conv.avatar_color || "#8b5cf6");
        $("#chatName").textContent = name;
        $("#chatSub").textContent = convSubtitle(conv);
        if (isDm && conv.other) {
            var p = State.presence[conv.other.id];
            var dot = $("#chatPresence");
            if (p) {
                dot.className = "thr-presence-dot " + (p.online ? "online" : p.status === "away" ? "away" : "offline");
                dot.title = p.online ? "Online" : p.status === "away" ? "Away" : "Offline";
            } else {
                dot.className = "thr-presence-dot offline";
                dot.title = "Offline";
            }
        } else {
            $("#chatPresence").className = "thr-presence-dot";
        }
        var muteBtn = $("#btnMute");
        muteBtn.classList.toggle("active", !!conv.muted);
        muteBtn.title = conv.muted ? "Unmute conversation" : "Mute conversation";
        $("#btnMembers").style.display = activeType === "group" ? "" : "none";
    }

    function loadHistory() {
        var ctype = State.active.type, cid = State.active.id;
        var seq = State.reqSeq;
        var key = ctype + ":" + cid;
        api("/threads/api/messages?ctx=" + ctype + ":" + cid + "&limit=30").then(function (res) {
            if (seq !== State.reqSeq) return;   // user switched away — drop stale response
            if (!res.success) { handleApiError(res); return; }
            State.messages = res.messages;
            State.seenIds = {};
            State.messages.forEach(function (m) { State.seenIds[m.id] = true; });
            if (State.messages.length) {
                State.afterId = State.messages[State.messages.length - 1].id;
                State.firstId = State.messages[0].id;
                State.hasMore = res.messages.length >= 30;
            } else {
                State.afterId = 0;
                State.firstId = 0;
                State.hasMore = false;
            }
            State.members = res.members || [];
            State.memberMap = {};
            State.members.forEach(function (m) { State.memberMap[m.id] = m; });
            State.settings = res.settings || State.settings;
            syncSettingsUI();
            if (res.polls) State.polls = res.polls;
            if (res.parties) State.parties = res.parties;
            // Store in cache so reopening this chat is instant.
            msgCache[key] = {
                messages: State.messages,
                afterId: State.afterId,
                firstId: State.firstId,
                hasMore: State.hasMore,
                members: State.members,
                pins: res.pins || [],
                polls: State.polls,
                parties: State.parties,
                html: "",
                at: Date.now(),
            };
            // Keep the cache bounded.
            var keys = Object.keys(msgCache);
            if (keys.length > 30) {
                keys.sort(function (a, b) { return msgCache[a].at - msgCache[b].at; });
                delete msgCache[keys[0]];
            }
            // Render messages IMMEDIATELY — don't wait for rank fetch.
            State.loadingHistory = false;
            if (seq === State.reqSeq) {
                renderMessages(true);
                renderPins(res.pins || []);
                if (isChannelOpen()) renderPartyStrip();
                refreshPresence();
            }
            // Fetch ranks in background to update badges (non-blocking).
            fetchThrRanks(State.messages).then(function () {
                if (seq === State.reqSeq && $("#msgList") && $("#msgList").innerHTML) {
                    renderMessages(false);
                }
            });
        });
    }

    function renderMessages(scrollToBottom) {
        var list = $("#msgList");
        var html = "";
        var msgCount = State.messages.length;
        var lastDay = null;
        var limit = State.messages.length;
        var polls = isChannelOpen() ? State.polls : [];
        var pi = 0;
        var dividerPlaced = !(State.newSinceId > 0);
        for (var i = 0; i < limit; i++) {
            var m = State.messages[i];
            while (pi < polls.length && String(polls[pi].created_at || "") <= String(m.created_at || "")) {
                html += renderPollCard(polls[pi]);
                pi++;
            }
            var day = dayKey(m.created_at);
            if (day !== lastDay) {
                html += '<div class="thr-day-divider"><span>' + escapeHtml(fmtDay(m.created_at)) + "</span></div>";
                lastDay = day;
            }
            if (!dividerPlaced && m.id > State.newSinceId && !m.deleted_at) {
                html += '<div class="thr-new-divider"><span>New messages</span></div>';
                dividerPlaced = true;
            }
            html += renderMessage(m);
        }
        while (pi < polls.length) {
            html += renderPollCard(polls[pi]);
            pi++;
        }
        list.innerHTML = html;

        // Cache the rendered HTML so reopening this conversation is instant
        // (like community chat where messages are already in the DOM).
        if (State.active) {
            var hk = State.active.type + ":" + State.active.id;
            if (msgCache[hk]) msgCache[hk].html = html;
        }

        // Always scroll to bottom — like community chat.
        list.scrollTop = list.scrollHeight;
        updateSeenText();
    }

    function renderMessage(m) {
        var cls = "thr-msg";
        if (m.deleted_at) return '<div class="thr-msg deleted"><span class="thr-deleted-text">Message deleted</span></div>';
        if (m.kind === "system") {
            return '<div class="thr-system-pill">' + escapeHtml(m.content) + "</div>";
        }

        var mine = m.sender && m.sender.id === State.me.id;
        if (mine) cls += " mine";

        var sender = m.sender || {};
        var content = m.content || "";
        var mentions = escapeHtml(content).replace(
            /@([A-Za-z0-9_]{3,20})/g,
            '<span class="thr-mention">@$1</span>'
        );

        var attach = "";
        if (m.kind === "anime") {
            try {
                var adata = JSON.parse(m.content);
                var aimg = adata.image ? '<img src="' + escapeHtml(adata.image) + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">' : '';
                var ameta = [adata.year, adata.rating].filter(Boolean).join(' \u2022 ');
                attach = '<a href="/anime/' + escapeHtml(adata.slug) + '" target="_blank" class="thr-anime-msg-card">' +
                    '<div class="thr-anime-msg-img">' + aimg + '</div>' +
                    '<div class="thr-anime-msg-info">' +
                    '<div class="thr-anime-msg-title">' + escapeHtml(adata.title) + '</div>' +
                    (ameta ? '<div class="thr-anime-msg-meta">' + escapeHtml(ameta) + '</div>' : '') +
                    '</div>' +
                    '<div class="thr-anime-msg-arrow"><i class="fas fa-external-link-alt"></i></div>' +
                    '</a>';
            } catch (e) {
                attach = '<p>' + escapeHtml(m.content) + '</p>';
            }
        } else if (m.attachment_url || (m.kind === "gif" && /^https?:\/\//.test(content))) {
            if (m.kind === "image" || m.kind === "gif") {
                attach = '<div class="thr-attach"><img src="' + escapeHtml(m.attachment_url || content) +
                    '" alt="attachment" loading="lazy"></div>';
            } else if (m.kind === "video") {
                attach = '<div class="thr-attach"><video src="' + escapeHtml(m.attachment_url) + '" controls></video></div>';
            }
        }

        var parentRef = "";
        if (m.parent) {
            var ptext = m.parent.content || "";
            parentRef = '<div class="thr-reply-ref" data-jump="' + m.parent.id + '">' +
                '<i class="fas fa-reply"></i> <span class="thr-reply-ref-name">@' +
                escapeHtml(m.parent.sender_username) + "</span> " +
                escapeHtml(ptext.length > 60 ? ptext.slice(0, 60) + "…" : ptext) + "</div>";
        }

        var flags = "";
        if (m.edited_at) flags += '<span class="thr-flag">(edited)</span>';
        if (m.is_pinned) flags += '<i class="fas fa-thumbtack thr-pin-flag" title="Pinned"></i>';

        var actions = "";
        actions += '<button class="thr-msg-act" data-act="reply" data-id="' + m.id + '" title="Reply"><i class="fas fa-reply"></i></button>';
        actions += '<button class="thr-msg-act' + (m.is_pinned ? " on" : "") + '" data-act="pin" data-id="' + m.id + '" title="' + (m.is_pinned ? "Unpin" : "Pin") + '"><i class="fas fa-thumbtack"></i></button>';
        if (mine) {
            actions += '<button class="thr-msg-act" data-act="edit" data-id="' + m.id + '" title="Edit"><i class="fas fa-pen"></i></button>';
            actions += '<button class="thr-msg-act danger" data-act="delete" data-id="' + m.id + '" title="Delete"><i class="fas fa-trash"></i></button>';
        } else if (isChannelOpen()) {
            actions += '<button class="thr-msg-act" data-act="report" data-id="' + m.id + '" title="Report"><i class="fas fa-flag"></i></button>';
            if (isCommMod()) {
                actions += '<button class="thr-msg-act danger" data-act="mod-delete" data-id="' + m.id + '" title="Delete (moderator)"><i class="fas fa-trash"></i></button>';
            }
        }

        var body;
        if (State.editingId === m.id) {
            body = '<div class="thr-edit-box">' +
                '<textarea class="thr-edit-input" data-edit-id="' + m.id + '" rows="2">' + escapeHtml(content) + "</textarea>" +
                '<div class="thr-edit-actions"><button class="thr-btn thr-btn-sm thr-btn-primary" data-act="save-edit" data-id="' + m.id + '">Save</button>' +
                '<button class="thr-btn thr-btn-sm" data-act="cancel-edit">Cancel</button></div></div>';
        } else {
            // Hide raw text when a rich attachment (anime card, image, gif) is
            // already rendered — prevents showing JSON or URLs as plain text.
            var showText = content && !attach;
            body = parentRef + (showText ? '<div class="thr-msg-content">' + mentions + "</div>" : "") +
                attach +
                '<div class="thr-msg-meta"><span class="thr-msg-time">' + fmtClock(m.created_at) + "</span>" + flags + "</div>";
        }

        return '<div class="' + cls + '" data-mid="' + m.id + '">' +
            '<div class="thr-msg-avatar thr-profile-open" data-uid="' + sender.id + '" style="background:' + escapeHtml(sender.avatar_color || "#8b5cf6") + '">' +
            avatarInner(sender) + "</div>" +
            '<div class="thr-msg-main">' +
            '<div class="thr-msg-head"><span class="thr-msg-user thr-profile-open" data-uid="' + sender.id + '">' + escapeHtml(sender.username || "unknown") + "</span>" +
            thrRankBadgeHtml(sender.id, _thrRankCache[sender.id] ? _thrRankCache[sender.id].rank : null, _thrRankCache[sender.id] ? _thrRankCache[sender.id].xp : 0) +
            '<span class="thr-msg-time">' + fmtClock(m.created_at) + "</span></div>" +
            body +
            '<div class="thr-msg-actions">' + actions + "</div>" +
            "</div></div>";
    }

    function renderPins(pins) {
        var badge = $("#pinBadge");
        badge.textContent = pins.length || "";
        badge.classList.toggle("hidden", !pins.length);
        $("#pinnedStrip").classList.toggle("hidden", !pins.length);
        if (pins.length) {
            var p = pins[0];
            var txt = p.content || (p.kind === "gif" ? "GIF" : p.kind === "image" ? "Image" : p.kind === "video" ? "Video" : p.kind === "anime" ? (function(){try{var d=JSON.parse(p.content);return "\uD83D\uDCFA "+d.title}catch(e){return "Anime"}})() : "");
            $("#pinnedStripText").textContent = "@" + (p.sender ? p.sender.username : "") + ": " +
                (txt.length > 70 ? txt.slice(0, 70) + "…" : txt);
        }
    }

    function updateSeenText() {
        var conv = State.active && State.active.conv;
        if (!conv) return;
        var activeType = State.active && State.active.type;
        var el = $("#seenText");
        if (activeType === "channel") { el.textContent = ""; return; }
        if (!State.settings.read_receipts || activeType === "group") {
            el.textContent = "";
            return;
        }
        var last = State.messages[State.messages.length - 1];
        if (!last || last.sender_id !== State.me.id) { el.textContent = ""; return; }
        var others = State.members.filter(function (m) { return m.id !== State.me.id; });
        if (!others.length) { el.textContent = ""; return; }
        var allRead = others.every(function (m) { return (m.last_read_message_id || 0) >= last.id; });
        if (allRead) {
            var t = "Seen";
            if (t !== State.lastSeenText) { el.textContent = t; State.lastSeenText = t; }
        } else {
            var some = others.filter(function (m) { return (m.last_read_message_id || 0) >= last.id; });
            var t2 = some.length ? "Seen by " + some.map(function (m) { return m.username; }).join(", ") : "";
            if (t2 !== State.lastSeenText) { el.textContent = t2; State.lastSeenText = t2; }
        }
    }
        // ----------------------------------------------------------
    // Sending + editing + deleting + pinning
    // ----------------------------------------------------------
    function sendMessage() {
        if (!State.active) return;
        var input = $("#msgInput");
        var content = input.value.trim();
        var attach = State.attach;
        var pendingAnime = State.pendingAnime;
        if (!content && !attach && !pendingAnime) return;

        // If an anime is pending, send it as an anime card
        if (pendingAnime) {
            sendAnimeCard(pendingAnime);
            clearAnimePreview();
            input.value = "";
            autoGrow(input);
            return;
        }

        var kind = attach ? attach.kind : "text";
        // Same wire format as the enter chat (/community): a picked GIF is
        // sent as kind:"gif" with the GIF url as the message content.
        var wireContent = (kind === "gif" && attach && attach.url) ? attach.url : content;
        var payload = {
            ctx: State.active.type + ":" + State.active.id,
            kind: kind,
            content: wireContent,
            attachment_url: attach ? attach.url : null,
            attachment_preview: attach ? attach.preview || null : null,
            parent_message_id: State.replyTo ? State.replyTo.id : null,
        };
        // Optimistic send: render the message instantly, then swap in the
        // server's copy when the POST returns, so Enter feels immediate
        // even when the backend round-trip is slow.
        if (State.me) {
            var tempId = -Date.now();
            var tempMsg = {
                id: tempId,
                sender_id: State.me.id,
                sender: {
                    id: State.me.id,
                    username: State.me.username || "",
                    avatar_color: State.me.avatar_color || "",
                    avatar: State.me.avatar || null,
                },
                kind: kind,
                content: wireContent,
                attachment_url: payload.attachment_url,
                attachment_preview: payload.attachment_preview,
                parent_message_id: payload.parent_message_id,
                parent: null,
                created_at: new Date().toISOString().replace("T", " ").slice(0, 19),
                temp: true,
            };
            appendMessages([tempMsg]);
            input.value = "";
            autoGrow(input);
            clearAttach();
            State.replyTo = null;
            $("#replyBar").classList.add("hidden");
            api("/threads/api/messages", { json: payload }).then(function (res) {
                if (!res.success) {
                    handleApiError(res);
                    removeTempMessage(tempId);
                    input.value = content;
                    if (attach) State.attach = attach;
                    return;
                }
                replaceTempMessage(tempId, res.message);
                markActiveRead();
            });
            return;
        }
        api("/threads/api/messages", { json: payload }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            appendMessages([res.message]);
            input.value = "";
            autoGrow(input);
            clearAttach();
            State.replyTo = null;
            $("#replyBar").classList.add("hidden");
            markActiveRead();
        });
    }

    function removeTempMessage(tempId) {
        State.messages = State.messages.filter(function (m) { return m.id !== tempId; });
        renderMessages(false);
    }

    function replaceTempMessage(tempId, real) {
        // If the poller already fetched the real message before this response
        // arrived, just drop the temporary copy (no duplicate).
        if (State.seenIds[real.id]) { removeTempMessage(tempId); return; }
        var idx = -1;
        State.messages.forEach(function (m, i) { if (m.id === tempId) idx = i; });
        if (idx >= 0) {
            State.messages[idx] = real;
            State.seenIds[real.id] = true;
            State.afterId = Math.max(State.afterId, real.id);
            // In-place DOM swap: find the temp element and replace its
            // content instead of re-rendering the entire message list.
            var tempEl = document.querySelector('[data-mid="' + tempId + '"]');
            if (tempEl) {
                var tmp = document.createElement("div");
                tmp.innerHTML = renderMessage(real);
                var newEl = tmp.firstElementChild;
                if (newEl) {
                    tempEl.parentNode.replaceChild(newEl, tempEl);
                    // Ensure scroll stays at bottom after swap
                    var _ml = $("#msgList");
                    _ml.scrollTop = _ml.scrollHeight;
                } else {
                    renderMessages(true);
                }
            } else {
                appendMessages([real]);
            }
        } else {
            appendMessages([real]);
        }
    }

    function appendMessages(msgs) {
        var fresh = msgs.filter(function (m) { return !State.seenIds[m.id]; });
        if (!fresh.length) return;
        fresh.forEach(function (m) { State.seenIds[m.id] = true; });
        var list = $("#msgList");

        var html = "";
        var lastDay = State.messages.length ? dayKey(State.messages[State.messages.length - 1].created_at) : null;
        fresh.forEach(function (m) {
            var day = dayKey(m.created_at);
            if (day !== lastDay) {
                html += '<div class="thr-day-divider"><span>' + escapeHtml(fmtDay(m.created_at)) + "</span></div>";
                lastDay = day;
            }
            html += renderMessage(m);
        });
        list.insertAdjacentHTML("beforeend", html);
        State.messages = State.messages.concat(fresh);
        if (fresh.length) {
            State.afterId = Math.max(State.afterId, fresh[fresh.length - 1].id);
        }
        // Keep the conversation cache in sync on EVERY append path (send,
        // poll, optimistic swap) so reopening stays instant and consistent.
        if (State.active) {
            var ck = State.active.type + ":" + State.active.id;
            if (msgCache[ck]) {
                msgCache[ck].messages = State.messages;
                msgCache[ck].afterId = State.afterId;
                msgCache[ck].at = Date.now();
            }
        }
        // Always scroll to bottom on new messages — like community chat.
        list.scrollTop = list.scrollHeight;
        updateSeenText();
    }

    function editMessage(id, newContent) {
        api("/threads/api/messages/" + id, { method: "PATCH", json: { content: newContent } }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            State.messages.forEach(function (m, i) {
                if (m.id === id) { State.messages[i] = res.message; State.seenIds[id] = true; }
            });
            State.editingId = null;
            renderMessages(false);
        });
    }

    function deleteMessage(id) {
        if (!window.confirm("Delete this message?")) return;
        api("/threads/api/messages/" + id, { method: "DELETE" }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            State.messages.forEach(function (m, i) {
                if (m.id === id) {
                    State.messages[i].deleted_at = "yes";
                    State.messages[i].content = "";
                }
            });
            renderMessages(false);
        });
    }

    function pinMessage(id) {
        api("/threads/api/messages/" + id + "/pin", { method: "POST" }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            State.messages.forEach(function (m) {
                if (m.id === id) { m.is_pinned = res.is_pinned ? 1 : 0; }
            });
            renderMessages(false);
            reloadPins();
        });
    }

    function reloadPins() {
        if (!State.active) return;
        api("/threads/api/messages?ctx=" + State.active.type + ":" + State.active.id + "&limit=1").then(function (res) {
            if (res.success) renderPins(res.pins || []);
        });
    }

    function markActiveRead() {
        if (!State.active) return;
        var last = null;
        for (var i = State.messages.length - 1; i >= 0; i--) {
            if (State.messages[i].id > 0) { last = State.messages[i]; break; }
        }
        var id = last ? last.id : 0;
        if (State.active.type === "channel") {
            api("/threads/api/channels/" + State.active.id + "/read", { json: { message_id: id } });
            State.active.conv.unread = 0;
            if (State.activeCommunity) {
                (State.activeCommunity.channels || []).forEach(function (ch) {
                    if (ch.id === State.active.id) ch.unread = 0;
                });
                renderChannelList();
                renderRail();
            }
            return;
        }
        api("/threads/api/conversations/" + State.active.id + "/read", { json: { message_id: id } });
        State.active.conv.unread = 0;
        renderConversations();
    }

    // ----------------------------------------------------------
    // Polling
    // ----------------------------------------------------------
    function pollMessages() {
        if (!State.active) return;
        if (document.hidden) return;
        var ctype = State.active.type, cid = State.active.id;
        var seq = State.reqSeq;
        var key = ctype + ":" + cid;
        api("/threads/api/messages?ctx=" + ctype + ":" + cid + "&after=" + State.afterId).then(function (res) {
            if (seq !== State.reqSeq) return;   // switched conversations — never mix contexts
            if (!res.success) { handleApiError(res); return; }
            if (res.messages && res.messages.length) {
                appendMessages(res.messages);
                // read-receipt member state refresh
                if (res.members && res.members.length) {
                    State.members = res.members;
                    State.memberMap = {};
                    State.members.forEach(function (m) { State.memberMap[m.id] = m; });
                }
                if (msgCache[key]) {
                    msgCache[key].messages = State.messages;
                    msgCache[key].afterId = State.afterId;
                    msgCache[key].at = Date.now();
                }
                markActiveRead();
            }
            if (isChannelOpen()) {
                updateChannelExtras(res.polls || State.polls, res.parties || State.parties);
            }
            updateTypingRow(res.typing || []);
            updateSeenText();
        });
    }

    function updateTypingRow(list) {
        var row = $("#typingRow");
        if (!State.settings.typing_indicators || !list.length) {
            row.classList.add("hidden");
            return;
        }
        var names = list.map(function (t) { return t.username; });
        var text = names.length === 1 ? names[0] + " is typing…"
            : names.length === 2 ? names[0] + " and " + names[1] + " are typing…"
            : names[0] + " and " + (names.length - 1) + " others are typing…";
        $("#typingText").textContent = text;
        row.classList.remove("hidden");
    }

    function refreshPresence() {
        var ids = [];
        var conv = State.active && State.active.conv;
        if (conv && conv.type === "dm" && conv.other) ids.push(conv.other.id);
        State.conversations.forEach(function (c) {
            if (c.type === "dm" && c.other && ids.indexOf(c.other.id) === -1) ids.push(c.other.id);
        });
        State.members.forEach(function (m) {
            if (ids.indexOf(m.id) === -1) ids.push(m.id);
        });
        api("/threads/api/presence?ids=" + ids.join(",")).then(function (res) {
            if (res.success) {
                State.presence = res.presence || {};
                renderConversations();
                if (State.active && State.active.conv.type === "dm") renderChatHead();
            }
        });
    }

    function refreshNotifications() {
        api("/threads/api/notifications").then(function (res) {
            if (!res.success) return;
            State.notifUnread = res.unread || 0;
            var badge = $("#bellBadge");
            badge.textContent = State.notifUnread || "";
            badge.classList.toggle("hidden", !State.notifUnread);
            if (!$("#bellDropdown").classList.contains("hidden")) {
                renderNotifications(res.notifications || []);
            }
        });
    }

    function renderNotifications(items) {
        var list = $("#notifList");
        if (!items.length) {
            list.innerHTML = '<div class="thr-dropdown-empty">No notifications yet</div>';
            return;
        }
        list.innerHTML = items.map(function (n) {
            var icon = n.type === "mention" ? "at"
                : n.type === "reply" ? "reply"
                : n.type === "dm" ? "comment-dots"
                : n.type === "friend_request" ? "user-plus"
                : n.type === "friend_accept" ? "user-check"
                : "bell";
            var text = n.type === "mention" ? " mentioned you"
                : n.type === "reply" ? " replied to your message"
                : n.type === "dm" ? " sent you a message"
                : n.type === "friend_request" ? " sent you a friend request"
                : n.type === "friend_accept" ? " accepted your friend request" : "";
            var ago = fmtConvTime(n.created_at);
            return '<div class="thr-notif' + (n.read ? " read" : "") + '" data-ntype="' + n.type + '" data-nctx="' +
                (n.context_type ? n.context_type + ":" + n.context_id : "") + '">' +
                '<span class="thr-notif-avatar" style="background:' + escapeHtml(n.from_color || "#8b5cf6") + '">' +
                avatarInner({ username: n.from_username, avatar: n.from_avatar }) + "</span>" +
                '<span class="thr-notif-body"><span class="thr-notif-text"><b>' + escapeHtml(n.from_username || "someone") + "</b>" +
                escapeHtml(text) + "</span><span class='thr-notif-time'>" + escapeHtml(ago) + "</span></span>" +
                (n.read ? "" : '<span class="thr-notif-dot"></span>') + "</div>";
        }).join("");
    }

    // ----------------------------------------------------------
    // Composer: typing, mention autocomplete, attach, gif
    // ----------------------------------------------------------
    function autoGrow(el) {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 160) + "px";
    }

    function onInputTyping() {
        autoGrow($("#msgInput"));
    }

    function updateMentionBox() {
        var input = $("#msgInput");
        var text = input.value.slice(0, input.selectionStart || input.value.length);
        var m = text.match(/(?:^|\s)@([A-Za-z0-9_]*)$/);
        var box = $("#mentionBox");
        if (!m || !State.members.length) {
            box.classList.add("hidden");
            return;
        }
        var q = m[1].toLowerCase();
        var cands = State.members.filter(function (mem) {
            return mem.id !== State.me.id && mem.username.toLowerCase().indexOf(q) !== -1;
        }).slice(0, 8);
        if (!cands.length) {
            box.classList.add("hidden");
            return;
        }
        box.innerHTML = cands.map(function (mem) {
            return '<div class="thr-mention-opt" data-user="' + mem.username + '">' +
                '<span class="thr-avatar thr-avatar-sm" style="background:' + escapeHtml(mem.avatar_color) + '">' +
                avatarInner(mem) + "</span>@" + escapeHtml(mem.username) + "</div>";
        }).join("");
        box.classList.remove("hidden");
    }

    function insertMention(username) {
        var input = $("#msgInput");
        var pos = input.selectionStart || input.value.length;
        var text = input.value;
        var match = text.slice(0, pos).match(/(?:^|\s)@([A-Za-z0-9_]*)$/);
        if (!match) return;
        var start = pos - match[0].length;
        var before = text.slice(0, start);
        var after = text.slice(pos);
        input.value = before + "@" + username + " " + after;
        input.focus();
        input.selectionStart = input.selectionEnd = before.length + username.length + 2;
        $("#mentionBox").classList.add("hidden");
        autoGrow(input);
    }

    function clearAttach() {
        State.attach = null;
        State.pendingAnime = null;
        $("#attachPreview").classList.add("hidden");
        $("#animePreview").classList.add("hidden");
        $("#fileInput").value = "";
    }

    function showAnimePreview(data) {
        var el = $("#animePreview");
        if (!el) return;
        var img = data.image ? '<img src="' + escapeHtml(data.image) + '" alt="" onerror="this.style.display=\'none\'">' : '';
        var meta = [data.year, data.rating].filter(Boolean).join(" \u2022 ");
        el.innerHTML = '<div class="thr-anime-preview-card">' + img +
            '<div class="thr-anime-preview-info">' +
            '<div class="thr-anime-preview-title">' + escapeHtml(data.title) + '</div>' +
            (meta ? '<div class="thr-anime-preview-meta">' + escapeHtml(meta) + '</div>' : '') +
            '</div>' +
            '<button class="thr-anime-preview-remove" onclick="clearAnimePreview()"><i class="fas fa-times"></i></button>' +
            '</div>';
        el.classList.remove("hidden");
    }

    window.clearAnimePreview = function() {
        State.pendingAnime = null;
        $("#animePreview").classList.add("hidden");
    };

    function showAttachPreview() {
        var a = State.attach;
        if (!a) return;
        $("#attachName").textContent = a.name || (a.kind === "gif" ? "GIF" : a.kind);
        // Real thumbnail like enter chat's pending-gif preview
        var thumb = $("#attachThumb");
        if (a.kind === "gif" || a.kind === "image") {
            thumb.src = a.preview || a.url;
            thumb.classList.remove("hidden");
        } else {
            thumb.classList.add("hidden");
            thumb.removeAttribute("src");
        }
        $("#attachPreview").classList.remove("hidden");
    }

    // ----------------------------------------------------------
    // Modals
    // ----------------------------------------------------------
    function openModal(id) { $("#" + id).classList.remove("hidden"); }
    function closeModal(id) { $("#" + id).classList.add("hidden"); }

    function fmtDate(iso) {
        if (!iso) return "";
        try {
            var d = new Date(iso.indexOf("T") === -1 ? iso.replace(" ", "T") + "Z" : iso);
            return d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
        } catch (e) { return iso; }
    }

    // ---- Other-user mini profile modal ----
    function openUserProfile(uid) {
        if (!uid) return;
        api("/threads/api/users/" + uid + "/profile").then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            var u = res.user || {};
            var av = document.getElementById("upAvatar");
            if (u.avatar) {
                av.innerHTML = '<img class="thr-avatar-img" src="/static/images/avatars/' + escapeHtml(u.avatar) + '" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:50%">';
                av.style.background = "transparent";
            } else {
                av.textContent = initials(u.username);
                av.style.background = u.avatar_color || "#8b5cf6";
            }
            av.style.display = "inline-flex";
            document.getElementById("upName").textContent = "@" + (u.username || "user");
            var rank = res.rank || "D";
            document.getElementById("upRole").textContent = "Rank " + rank;
            var badge = document.getElementById("upBadge");
            badge.textContent = rank;
            badge.classList.remove("rank-F", "rank-D", "rank-C", "rank-B", "rank-A", "rank-S", "rank-S+");
            badge.classList.add("rank-" + rank);
            document.getElementById("upXp").textContent = (res.xp || 0).toLocaleString() + " XP";
            document.getElementById("upPct").textContent = (res.xp_pct || 0) + "%";
            var bar = document.getElementById("upBar");
            bar.classList.remove("rank-F", "rank-D", "rank-C", "rank-B", "rank-A", "rank-S", "rank-S+");
            bar.classList.add("rank-" + rank);
            document.getElementById("upBarFill").style.width = (res.xp_pct || 0) + "%";
            var joined = document.getElementById("upJoined");
            joined.innerHTML = '<i class="fas fa-calendar-alt"></i> &nbsp;Joined ' + (fmtDate(res.joined_at) || "unknown");
            var tagsWrap = document.getElementById("upGuilds");
            var guilds = res.guilds || [];
            tagsWrap.innerHTML = guilds.length ? guilds.map(function (g) {
                return '<span class="thr-profile-tag"><i class="fas fa-hashtag"></i>' + escapeHtml(g.name) +
                    (g.role !== "member" ? '<span class="thr-tag-role">' + escapeHtml(g.role) + "</span>" : "") +
                    "</span>";
            }).join("") : '<span class="thr-profile-empty">No public guilds yet</span>';
            var link = document.getElementById("upFullProfile");
            link.href = "/user/" + encodeURIComponent(u.username || "");
            openModal("modalUserProfile");
        });
    }

    function wireModalClose() {
        $$(".thr-modal-close").forEach(function (b) {
            b.addEventListener("click", function () {
                closeModal(b.getAttribute("data-close"));
            });
        });
        $$(".thr-modal").forEach(function (m) {
            m.addEventListener("click", function (e) {
                if (e.target === m) closeModal(m.id);
            });
        });
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape") {
                $$(".thr-modal:not(.hidden)").forEach(function (m) { closeModal(m.id); });
            }
        });
    }
    // =======================================================================
    // FRIEND REQUEST FLOW (single source of truth)
    //
    // 1. New message modal: search a username -> Add friend / Requested /
    //    Message. Sending the request is ALL this modal does.
    // 2. Friend requests modal: incoming requests with Accept / Reject.
    //    Accepting closes the modal and opens the new DM in the chat area.
    // =======================================================================

    function wireDmModal() {
        var input = $("#dmSearch");
        var results = $("#dmResults");
        var t;

        function rowHtml(u) {
            var action;
            if (u.friend_status === "friends") {
                action = '<button class="thr-btn thr-btn-sm thr-btn-primary" data-msg-id="' + u.id + '">Message</button>';
            } else if (u.friend_status === "outgoing") {
                action = '<span class="thr-count-chip">Requested</span>';
            } else if (u.friend_status === "incoming") {
                // They already sent us a request - say so; accept it in the
                // Friend requests section.
                action = '<span class="thr-count-chip">Sent you a request</span>';
            } else {
                action = '<button class="thr-btn thr-btn-sm" data-add-id="' + u.id + '">Add friend</button>';
            }
            return '<div class="thr-user-row" data-uid="' + u.id + '" data-fstatus="' + u.friend_status + '">' +
                '<span class="thr-avatar thr-avatar-md" style="background:' + escapeHtml(u.avatar_color || "#8b5cf6") + '">' +
                avatarInner(u) + "</span>" +
                "<span>" + escapeHtml(u.username) + "</span>" + action + "</div>";
        }

        function search() {
            var q = input.value.trim();
            if (!q) { results.innerHTML = ""; return; }
            api("/threads/api/users/search?q=" + encodeURIComponent(q)).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                results.innerHTML = res.users.map(rowHtml).join("")
                    || '<div class="thr-dropdown-empty">No users found</div>';
            }).catch(function (e) {
                if (e && e.message !== "auth") results.innerHTML = '<div class="thr-dropdown-empty">Search failed - try again</div>';
            });
        }

        input.addEventListener("input", function () {
            clearTimeout(t);
            t = setTimeout(search, 250);
        });

        results.addEventListener("click", function (e) {
            // "Add friend" - send a friend request. That is all.
            var addBtn = e.target.closest("[data-add-id]");
            if (addBtn) {
                addBtn.disabled = true;
                addBtn.textContent = "Sending…";
                api("/threads/api/friends/request", { json: { user_id: parseInt(addBtn.getAttribute("data-add-id"), 10) } })
                    .then(function (res) {
                        if (!res.success) { handleApiError(res); }
                        else { toast("Friend request sent \u2713"); }
                        search();
                    }).catch(function (e) {
                        if (e && e.message !== "auth") toast("Couldn't send request", "error");
                        search();
                    });
                return;
            }
            // "Message" - open the DM with an existing friend.
            var msgBtn = e.target.closest("[data-msg-id]");
            if (!msgBtn) return;
            var row = msgBtn.closest(".thr-user-row");
            var uid = parseInt(row.getAttribute("data-uid"), 10);
            msgBtn.disabled = true;
            api("/threads/api/conversations/dm", { json: { user_id: uid } }).then(function (res) {
                if (!res.success) {
                    if (res.error === "not_friends") {
                        toast("Send a friend request first", "error");
                    } else {
                        handleApiError(res);
                    }
                    msgBtn.disabled = false;
                    return;
                }
                closeModal("modalNewDm");
                input.value = "";
                results.innerHTML = "";
                upsertConversation(res.conversation);
                openConversation(res.conversation.type, res.conversation.id);
            }).catch(function (e) {
                msgBtn.disabled = false;
                if (e && e.message !== "auth") toast("Couldn't open chat", "error");
            });
        });
    }

    function refreshRequestBadge() {
        api("/threads/api/friends/requests").then(function (res) {
            if (!res.success) return;
            var n = res.count || 0;
            var badge = $("#reqBadge");
            badge.textContent = n > 99 ? "99+" : n;
            badge.classList.toggle("hidden", !n);
            State.reqCount = n;
            State.reqIncoming = res.incoming || [];
            State.reqOutgoing = res.outgoing || [];
        }).catch(function () { /* silent - background poll */ });
    }

    function renderRequestsModal() {
        var inc = $("#reqIncoming");
        var out = $("#reqOutgoing");
        function rowHtml(u) {
            return '<div class="thr-user-row" data-req-id="' + u.id + '">' +
                '<span class="thr-avatar thr-avatar-md" style="background:' + escapeHtml(u.avatar_color || "#8b5cf6") + '">' +
                avatarInner(u) + "</span><span>" + escapeHtml(u.username) + "</span>" +
                '<button class="thr-btn thr-btn-sm thr-btn-primary" data-accept="' + u.id + '">Accept</button>' +
                '<button class="thr-btn thr-btn-sm thr-btn-danger" data-reject="' + u.id + '">Reject</button></div>';
        }
        inc.innerHTML = (State.reqIncoming || []).map(rowHtml).join("")
            || '<div class="thr-dropdown-empty">No message requests yet</div>';
        out.innerHTML = (State.reqOutgoing || []).map(function (u) {
            return '<div class="thr-user-row"><span class="thr-avatar thr-avatar-md" style="background:' +
                escapeHtml(u.avatar_color || "#8b5cf6") + '">' + avatarInner(u) +
                "</span><span>" + escapeHtml(u.username) + "</span>" +
                '<span class="thr-count-chip">Pending</span></div>';
        }).join("") || '<div class="thr-dropdown-empty">No sent requests yet</div>';
    }

    function openRequestsModal() {
        // Show the modal immediately with cached data, then refresh.
        renderRequestsModal();
        openModal("modalRequests");
        api("/threads/api/friends/requests").then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            State.reqIncoming = res.incoming || [];
            State.reqOutgoing = res.outgoing || [];
            renderRequestsModal();
        }).catch(function (e) {
            if (e && e.message !== "auth") {
                toast("Couldn't load requests" + (e && e.message ? " (" + e.message + ")" : ""), "error");
            }
        });
    }

    function wireRequestsModal() {
        $("#btnRequests").addEventListener("click", openRequestsModal);
        $("#modalRequests").addEventListener("click", function (e) {
            var acc = e.target.closest("[data-accept]");
            var rej = e.target.closest("[data-reject]");
            if (!acc && !rej) return;
            var btn = acc || rej;
            var reqId = parseInt(btn.getAttribute(acc ? "data-accept" : "data-reject"), 10);
            var accept = !!acc;
            btn.disabled = true;
            api("/threads/api/friends/requests/" + reqId + "/respond",
                { json: { accept: accept } }).then(function (res) {
                if (!res.success) { handleApiError(res); btn.disabled = false; return; }
                if (accept) {
                    toast("Friend request accepted \u{1F389}");
                    // Move the user from the requests section to the chat:
                    // close the modal and open the fresh DM.
                    closeModal("modalRequests");
                    if (res.conversation) {
                        upsertConversation(res.conversation);
                        openConversation(res.conversation.type, res.conversation.id);
                    } else {
                        refreshConversations();
                    }
                } else {
                    toast("Friend request rejected");
                    State.reqIncoming = (State.reqIncoming || []).filter(function (u) { return u.id !== reqId; });
                    renderRequestsModal();
                }
                refreshRequestBadge();
                refreshConversations();
            }).catch(function (e) {
                btn.disabled = false;
                if (e && e.message !== "auth") toast("Couldn't update request" + (e && e.message ? " (" + e.message + ")" : ""), "error");
            });
        });
    }

    function upsertConversation(conv) {
        var found = false;
        State.conversations.forEach(function (c, i) {
            if (c.id === conv.id && c.type === conv.type) { State.conversations[i] = conv; found = true; }
        });
        if (!found) State.conversations.unshift(conv);
        renderConversations();
    }

    // ---- Friend requests modal ----

    // (group creation was replaced by the friend-request flow)

    // ---- GIF modal ----
    function wireGifModal() {
        var grid = $("#gifGrid");
        var note = $("#gifNote");
        var input = $("#gifSearch");
        var t;

        function load(q) {
            grid.innerHTML = '<div class="thr-gif-loading"><i class="fas fa-spinner fa-spin"></i></div>';
            note.classList.add("hidden");
            api("/threads/api/gifs" + (q ? "?q=" + encodeURIComponent(q) : "")).then(function (res) {
                if (!res.success) {
                    grid.innerHTML = "";
                    note.textContent = res.hint || res.error || "Couldn't load GIFs.";
                    note.classList.remove("hidden");
                    return;
                }
                if (!res.results.length) {
                    grid.innerHTML = "";
                    note.textContent = "No GIFs found.";
                    note.classList.remove("hidden");
                    return;
                }
                grid.innerHTML = res.results.map(function (g) {
                    return '<img class="thr-gif" src="' + escapeHtml(g.preview) + '" data-url="' +
                        escapeHtml(g.url) + '" data-preview="' + escapeHtml(g.preview) +
                        '" alt="' + escapeHtml(g.title || "gif") + '" loading="lazy">';
                }).join("");
            });
        }

        input.addEventListener("input", function () {
            clearTimeout(t);
            t = setTimeout(function () { load(input.value.trim()); }, 350);
        });

        grid.addEventListener("click", function (e) {
            var img = e.target.closest(".thr-gif");
            if (!img) return;
            State.attach = {
                kind: "gif",
                url: img.getAttribute("data-url"),
                preview: img.getAttribute("data-preview"),
                name: "GIF",
            };
            var gp = $("#modalGif"); if (gp) gp.classList.add("hidden");
            showAttachPreview();
            $("#msgInput").focus();
        });

        // GIF handled by + menu above
    }

    // ---- Anime picker modal ----
    function wireAnimeModal() {
        var grid = $("#animeResults");
        var input = $("#animeSearch");
        var t;

        function load(q) {
            if (!q) { grid.innerHTML = '<div class="thr-anime-hint">Type to search 13k+ anime…</div>'; return; }
            grid.innerHTML = '<div class="thr-gif-loading"><i class="fas fa-spinner fa-spin"></i></div>';
            api("/api/search?q=" + encodeURIComponent(q)).then(function (res) {
                if (!res.success || !res.results.length) {
                    grid.innerHTML = '<div class="thr-anime-hint">No results</div>';
                    return;
                }
                grid.innerHTML = res.results.map(function (a) {
                    return '<div class="thr-anime-card" data-slug="' + escapeHtml(a.slug) +
                        '" data-title="' + escapeHtml(a.title) +
                        '" data-image="' + escapeHtml(a.image || '') +
                        '" data-year="' + escapeHtml(a.year || '') +
                        '" data-rating="' + escapeHtml(a.rating || '') + '"' +
                        '<img src="' + escapeHtml(a.image || '') + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">' +
                        '<div class="thr-anime-card-info">' +
                            '<div class="thr-anime-card-title">' + escapeHtml(a.title) + '</div>' +
                            '<div class="thr-anime-card-meta">' + escapeHtml(a.year || '') + (a.rating ? ' \u2022 ' + escapeHtml(a.rating) : '') + '</div>' +
                        '</div>' +
                        '<div class="thr-anime-card-send"><i class="fas fa-paper-plane"></i></div>' +
                    '</div>';
                }).join("");
            }).catch(function () {
                grid.innerHTML = '<div class="thr-anime-hint">Search failed</div>';
            });
        }

        input.addEventListener("input", function () {
            clearTimeout(t);
            t = setTimeout(function () { load(input.value.trim()); }, 300);
        });

        grid.addEventListener("click", function (e) {
            var card = e.target.closest(".thr-anime-card");
            if (!card) return;
            var data = {
                slug: card.dataset.slug,
                title: card.dataset.title,
                image: card.dataset.image,
                year: card.dataset.year,
                rating: card.dataset.rating,
            };
            // Show anime in message bar preview (like community chat)
            State.pendingAnime = data;
            showAnimePreview(data);
            var ap = $("#modalAnime"); if (ap) ap.classList.add("hidden");
            grid.innerHTML = '';
            $("#msgInput").focus();
        });

        // Anime handled by + menu above
    }

    function sendAnimeCard(data) {
        if (!State.active) return;
        var payload = {
            ctx: State.active.type + ":" + State.active.id,
            kind: "anime",
            content: JSON.stringify(data),
            parent_message_id: null,
        };
        // Optimistic send
        var tempId = -Date.now();
        var tempMsg = {
            id: tempId,
            sender_id: State.me.id,
            sender: {
                id: State.me.id,
                username: State.me.username || "",
                avatar_color: State.me.avatar_color || "",
                avatar: State.me.avatar || null,
            },
            kind: "anime",
            content: payload.content,
            attachment_url: null,
            attachment_preview: null,
            parent_message_id: null,
            parent: null,
            created_at: new Date().toISOString().replace("T", " ").slice(0, 19),
            temp: true,
        };
        appendMessages([tempMsg]);
        api("/threads/api/messages", { json: payload }).then(function (res) {
            if (!res.success) {
                handleApiError(res);
                removeTempMsg(tempId);
                return;
            }
            replaceTempMsg(tempId, res.message);
            markActiveRead();
        });
    }

    function removeTempMsg(tempId) {
        State.messages = State.messages.filter(function (m) { return m.id !== tempId; });
        renderMessages(false);
    }

    function replaceTempMsg(tempId, real) {
        if (State.seenIds[real.id]) { removeTempMsg(tempId); return; }
        var idx = -1;
        State.messages.forEach(function (m, i) { if (m.id === tempId) idx = i; });
        if (idx >= 0) {
            State.messages[idx] = real;
            State.seenIds[real.id] = true;
            State.afterId = Math.max(State.afterId, real.id);
            renderMessages(true);
        } else {
            appendMessages([real]);
        }
    }

    // ---- Emoji picker modal ----
    var EMOJI_DATA = {
        "Smileys": ["\u{1F600}","\u{1F603}","\u{1F604}","\u{1F601}","\u{1F606}","\u{1F605}","\u{1F923}","\u{1F602}","\u{1F642}","\u{1F643}","\u{1F609}","\u{1F608}","\u{1F60E}","\u{1F60D}","\u{1F970}","\u{1F618}","\u{1F617}","\u{1F619}","\u{1F61A}","\u{1F60B}","\u{1F61B}","\u{1F61C}","\u{1F61D}","\u{1F92A}","\u{1F610}","\u{1F611}","\u{1F636}","\u{1F60F}","\u{1F612}","\u{1F644}","\u{1F62C}","\u{1F914}","\u{1F92D}","\u{1F911}","\u{1F917}","\u{1F920}","\u{1F973}","\u{1F97F}","\u{1F60C}","\u{1F614}","\u{1F62A}","\u{1F62B}","\u{1F634}","\u{1F924}","\u{1F637}"],
        "Gestures": ["\u{1F44B}","\u{1F91A}","\u{1F44C}","\u{1F90F}","\u{1F448}","\u{1F449}","\u{1F446}","\u{1F447}","\u{261D}\u{FE0F}","\u{1F44D}","\u{1F44E}","\u{1F44A}","\u{1F44F}","\u{1F64C}","\u{1F450}","\u{1F4AA}","\u{1F440}","\u{1F4A4}","\u{1F44B}","\u{1F64F}","\u{1F91F}","\u{1F918}","\u{270A}","\u{270B}","\u{1F44B}","\u{1F932}","\u{1F91E}","\u{1F91F}","\u{270B}","\u{1F91D}","\u{1F64F}","\u{1F44F}","\u{1F4AF}","\u{1F442}","\u{1F443}","\u{1F9E0}","\u{1FAC0}","\u{1F9B4}"],
        "Hearts": ["\u{2764}\u{FE0F}","\u{1F491}","\u{1F48E}","\u{1F494}","\u{1F495}","\u{1F496}","\u{1F497}","\u{1F498}","\u{1F499}","\u{1F49A}","\u{1F49B}","\u{1F49C}","\u{1F90D}","\u{1F90E}","\u{1F5A4}","\u{1F90F}","\u{2764}\u{FE0F}","\u{1F493}","\u{1F49D}","\u{1F49E}","\u{1F49F}","\u{2763}\u{FE0F}","\u{1F48C}","\u{1F48D}","\u{1F48F}","\u{1F91A}","\u{1F48C}"],
        "Nature": ["\u{1F331}","\u{1F33B}","\u{1F33A}","\u{1F337}","\u{1F338}","\u{1F339}","\u{1F33C}","\u{1F335}","\u{1F334}","\u{1F332}","\u{1F333}","\u{1F340}","\u{1F341}","\u{1F342}","\u{1F343}","\u{1F33E}","\u{1F344}","\u{1F345}","\u{1F346}","\u{1F347}","\u{1F348}","\u{1F349}","\u{1F34A}","\u{1F34B}","\u{1F34C}","\u{1F34D}","\u{1F34E}","\u{1F34F}","\u{1F350}","\u{1F351}","\u{1F352}","\u{1F353}","\u{2600}\u{FE0F}","\u{1F319}","\u{2B50}","\u{26C5}","\u{2601}\u{FE0F}","\u{1F308}","\u{1F30A}","\u{1F300}","\u{1F30B}","\u{1F30D}","\u{1F30E}","\u{1F30F}","\u{1F30C}","\u{1F310}","\u{1F311}","\u{1F312}","\u{1F313}","\u{1F314}","\u{1F315}","\u{1F316}","\u{1F317}","\u{1F318}"],
        "Food": ["\u{1F370}","\u{1F382}","\u{1F371}","\u{1F372}","\u{1F373}","\u{1F375}","\u{1F376}","\u{1F37A}","\u{1F37B}","\u{1F378}","\u{1F37C}","\u{2615}","\u{1F964}","\u{1F9C3}","\u{1F9C0}","\u{1F36D}","\u{1F36C}","\u{1F366}","\u{1F36B}","\u{1F36A}","\u{1F369}","\u{1F36E}","\u{1F36F}","\u{1F367}","\u{1F354}","\u{1F355}","\u{1F356}","\u{1F357}","\u{1F358}","\u{1F359}","\u{1F35A}","\u{1F35B}","\u{1F35C}","\u{1F35D}","\u{1F35E}","\u{1F35F}","\u{1F360}","\u{1F361}","\u{1F362}","\u{1F363}","\u{1F364}","\u{1F365}","\u{1F961}","\u{1F962}","\u{1F963}","\u{1F950}","\u{1F951}","\u{1F952}","\u{1F953}","\u{1F954}","\u{1F955}","\u{1F956}","\u{1F957}","\u{1F958}","\u{1F959}","\u{1F95A}","\u{1F95B}","\u{1F95C}","\u{1F95D}","\u{1F95E}","\u{1F95F}","\u{1F960}","\u{1F968}","\u{1F969}","\u{1F96A}","\u{1F96B}","\u{1F96C}","\u{1F96D}","\u{1F96E}","\u{1F96F}"],
        "Activities": ["\u{1F3B0}","\u{1F3AE}","\u{1F3B2}","\u{1F3B3}","\u{1F3B1}","\u{1F3B4}","\u{2660}\u{FE0F}","\u{2665}\u{FE0F}","\u{2663}\u{FE0F}","\u{2666}\u{FE0F}","\u{1F0CF}","\u{1F004}","\u{1F3C6}","\u{1F3C5}","\u{1F3C3}","\u{26BD}","\u{1F3C0}","\u{1F3C8}","\u{1F3A0}","\u{1F3A1}","\u{1F3A2}","\u{1F3A3}","\u{1F3A4}","\u{1F3A5}","\u{1F3A6}","\u{1F3A7}","\u{1F3A8}","\u{1F3A9}","\u{1F3AA}","\u{1F3AB}","\u{1F3AC}","\u{1F3AD}","\u{1F3AF}","\u{1F3B6}","\u{1F3B8}","\u{1F3B9}","\u{1F3BA}","\u{1F3BB}","\u{1F3BC}","\u{1F3BD}","\u{1F3BE}","\u{1F3BF}","\u{1F3D1}","\u{1F9E3}","\u{1F3AD}","\u{1F97A}","\u{1F97B}"],
        "Objects": ["\u{1F4A1}","\u{1F4A4}","\u{1F4A3}","\u{1F4A5}","\u{1F4A6}","\u{1F4A7}","\u{1F4A8}","\u{1F4A9}","\u{1F4AA}","\u{1F4AB}","\u{1F4AC}","\u{1F4AD}","\u{1F4AE}","\u{1F4AF}","\u{1F4B0}","\u{1F4B1}","\u{1F4B2}","\u{1F4B3}","\u{1F4B4}","\u{1F4B5}","\u{1F4B6}","\u{1F4B7}","\u{1F4B8}","\u{1F4B9}","\u{1F4BA}","\u{1F4BB}","\u{1F4BC}","\u{1F4BD}","\u{1F4BE}","\u{1F4BF}","\u{1F4C0}","\u{1F4C1}","\u{1F4C2}","\u{1F4C3}","\u{1F4C4}","\u{1F4C5}","\u{1F4C6}","\u{1F4C7}","\u{1F4C8}","\u{1F4C9}","\u{1F4CA}","\u{1F4CB}","\u{1F4CC}","\u{1F4CD}","\u{1F4CE}","\u{1F4CF}","\u{1F4D0}","\u{1F4D1}","\u{1F4D2}","\u{1F4D3}","\u{1F4D4}","\u{1F4D5}","\u{1F4D6}","\u{1F4D7}","\u{1F4D8}","\u{1F4D9}","\u{1F4DA}","\u{1F4DB}","\u{1F4DC}","\u{1F4DD}","\u{1F4DE}","\u{1F4DF}","\u{1F4E0}","\u{1F4E1}","\u{1F4E2}","\u{1F4E3}","\u{1F4E4}","\u{1F4E5}","\u{1F4E6}","\u{1F4E7}","\u{1F4E8}","\u{1F4E9}","\u{1F4EA}","\u{1F4EB}","\u{1F4EC}","\u{1F4ED}","\u{1F4EE}","\u{1F4EF}","\u{1F4F0}","\u{1F4F1}","\u{1F4F2}","\u{1F4F3}","\u{1F4F4}","\u{1F4F5}","\u{1F4F6}","\u{1F4F7}","\u{1F4F9}","\u{1F4FA}","\u{1F4FB}","\u{1F4FC}","\u{1F500}","\u{1F501}","\u{1F502}","\u{1F503}","\u{1F504}","\u{1F505}","\u{1F506}","\u{1F507}","\u{1F508}","\u{1F509}","\u{1F50A}","\u{1F50B}","\u{1F50C}","\u{1F50D}","\u{1F50E}","\u{1F50F}","\u{1F510}","\u{1F511}","\u{1F512}","\u{1F513}","\u{1F514}","\u{1F515}","\u{1F516}","\u{1F517}","\u{1F518}","\u{1F519}","\u{1F51A}","\u{1F51B}","\u{1F51C}","\u{1F51D}","\u{1F51E}","\u{1F51F}","\u{1F520}","\u{1F521}","\u{1F522}","\u{1F523}","\u{1F524}","\u{1F525}","\u{1F526}","\u{1F527}","\u{1F528}","\u{1F529}","\u{1F52A}","\u{1F52B}","\u{1F52C}","\u{1F52D}","\u{1F52E}","\u{1F52F}","\u{1F530}","\u{1F531}","\u{1F532}","\u{1F533}","\u{1F534}","\u{1F535}","\u{1F536}","\u{1F537}","\u{1F538}","\u{1F539}","\u{1F53A}","\u{1F53B}","\u{1F53C}","\u{1F53D}","\u{1F53E}","\u{1F53F}","\u{1F540}","\u{1F541}","\u{1F542}","\u{1F543}","\u{1F544}","\u{1F545}","\u{1F546}","\u{1F547}","\u{1F548}","\u{1F549}","\u{1F54A}","\u{1F54B}","\u{1F54C}","\u{1F54D}","\u{1F54E}","\u{1F550}","\u{1F551}","\u{1F552}","\u{1F553}","\u{1F554}","\u{1F555}","\u{1F556}","\u{1F557}","\u{1F558}","\u{1F559}","\u{1F55A}","\u{1F55B}","\u{1F55C}","\u{1F55D}","\u{1F55E}","\u{1F55F}","\u{1F560}","\u{1F561}","\u{1F562}","\u{1F563}","\u{1F564}","\u{1F565}","\u{1F566}","\u{1F567}","\u{1F56F}","\u{1F570}","\u{1F573}","\u{1F574}","\u{1F575}","\u{1F576}","\u{1F577}","\u{1F578}","\u{1F579}","\u{1F57A}","\u{1F580}","\u{1F583}","\u{1F584}","\u{1F585}","\u{1F586}","\u{1F587}","\u{1F58A}","\u{1F58B}","\u{1F58C}","\u{1F58D}","\u{1F58E}","\u{1F58F}","\u{1F590}","\u{1F591}","\u{1F592}","\u{1F593}","\u{1F595}","\u{1F596}","\u{1F597}","\u{1F598}","\u{1F599}","\u{1F59A}","\u{1F59B}","\u{1F59C}","\u{1F59D}","\u{1F59E}","\u{1F59F}","\u{1F5A0}","\u{1F5A1}","\u{1F5A2}","\u{1F5A5}","\u{1F5A8}","\u{1F5A9}","\u{1F5AA}","\u{1F5AB}","\u{1F5AC}","\u{1F5AD}","\u{1F5AE}","\u{1F5AF}","\u{1F5B0}","\u{1F5B1}","\u{1F5B2}","\u{1F5B3}","\u{1F5B4}","\u{1F5B5}","\u{1F5B6}","\u{1F5B7}","\u{1F5B8}","\u{1F5B9}","\u{1F5BA}","\u{1F5BB}","\u{1F5BC}","\u{1F5BD}","\u{1F5BE}","\u{1F5BF}","\u{1F5C0}","\u{1F5C1}","\u{1F5C2}","\u{1F5C3}","\u{1F5C4}","\u{1F5C5}","\u{1F5C6}","\u{1F5C7}","\u{1F5C8}","\u{1F5C9}","\u{1F5CA}","\u{1F5CB}","\u{1F5CC}","\u{1F5CD}","\u{1F5CE}","\u{1F5CF}","\u{1F5D0}","\u{1F5D1}","\u{1F5D2}","\u{1F5D3}","\u{1F5D4}","\u{1F5D5}","\u{1F5D6}","\u{1F5D7}","\u{1F5D8}","\u{1F5D9}","\u{1F5DA}","\u{1F5DB}","\u{1F5DC}","\u{1F5DD}","\u{1F5DE}","\u{1F5DF}","\u{1F5E0}","\u{1F5E1}","\u{1F5E2}","\u{1F5E3}","\u{1F5E4}","\u{1F5E5}","\u{1F5E6}","\u{1F5E7}","\u{1F5E8}","\u{1F5E9}","\u{1F5EA}","\u{1F5EB}","\u{1F5EC}","\u{1F5ED}","\u{1F5EE}","\u{1F5EF}","\u{1F5F0}","\u{1F5F1}","\u{1F5F2}","\u{1F5F3}","\u{1F5F4}","\u{1F5F5}","\u{1F5F6}","\u{1F5F7}","\u{1F5F8}","\u{1F5F9}","\u{1F5FA}","\u{1F5FB}","\u{1F5FC}","\u{1F5FD}","\u{1F5FE}","\u{1F5FF}"],
        "Symbols": ["\u{1F303}","\u{1F304}","\u{1F305}","\u{1F306}","\u{1F307}","\u{1F309}","\u{26AA}","\u{26AB}","\u{2B55}","\u{2705}","\u{2714}\u{FE0F}","\u{274C}","\u{274E}","\u{2753}","\u{2754}","\u{2755}","\u{2795}","\u{2796}","\u{2797}","\u{2764}\u{FE0F}","\u{1F4AF}","\u{1F525}","\u{1F31F}","\u{1F4AB}","\u{2728}","\u{2B50}","\u{1F4A5}","\u{1F4A3}","\u{1F386}","\u{1F387}","\u{2734}\u{FE0F}","\u{2733}\u{FE0F}","\u{1F49E}","\u{1F49D}","\u{1F49C}","\u{1F49B}","\u{1F49A}","\u{1F499}","\u{2764}\u{FE0F}","\u{2665}\u{FE0F}","\u{25CF}","\u{25CB}","\u{25A0}","\u{25B1}","\u{25B2}","\u{25BC}","\u{25C0}","\u{25B6}","\u{1F534}","\u{1F535}","\u{1F7E0}","\u{1F7E1}","\u{1F7E2}","\u{1F7E3}","\u{1F7E4}","\u{1F536}","\u{1F537}","\u{1F538}","\u{1F539}","\u{1F53A}","\u{1F53B}","\u{1F4B5}","\u{1F4B0}","\u{2696}\u{FE0F}","\u{1F48E}","\u{1F381}","\u{26D4}","\u{26A0}\u{FE0F}","\u{1F6AB}","\u{1F6AF}","\u{1F6B2}","\u{267F}","\u{1F6BC}","\u{1F6B6}","\u{1F6B5}","\u{1F6B4}","\u{1F680}","\u{1F6A2}","\u{26F5}","\u{1F6A4}","\u{2693}\u{FE0F}","\u{26FD}","\u{1F6A8}","\u{1F6A5}","\u{1F6A6}","\u{1F6D1}","\u{1F6A7}","\u{1F6B0}","\u{1F6C1}","\u{1F6F8}","\u{1F683}","\u{1F684}","\u{1F685}","\u{1F686}","\u{1F687}","\u{1F688}","\u{1F689}","\u{1F68A}","\u{1F69D}","\u{1F69E}","\u{1F691}","\u{1F692}","\u{1F693}","\u{1F694}","\u{1F695}","\u{1F696}","\u{1F697}","\u{1F698}","\u{1F699}","\u{1F69A}","\u{1F6B1}","\u{1F6B3}","\u{1F6B7}","\u{1F6B8}","\u{1F6B9}","\u{1F6BA}","\u{1F6BB}","\u{1F6BC}","\u{1F6BD}","\u{1F6BE}","\u{1F6BF}","\u{1F6C0}","\u{1F6D2}","\u{1F6E1}\u{FE0F}","\u{1F6E2}\u{FE0F}","\u{1F6E5}\u{FE0F}","\u{1F6E9}\u{FE0F}","\u{1F6EB}","\u{1F6EC}","\u{1F6F0}\u{FE0F}","\u{1F6F3}\u{FE0F}","\u{24C2}\u{FE0F}","\u{1F17F}\u{FE0F}","\u{1F202}\u{FE0F}","\u{1F237}\u{FE0F}","\u{1F21A}\u{FE0F}","\u{1F22F}\u{FE0F}","\u{203C}\u{FE0F}","\u{2049}\u{FE0F}","\u{2122}\u{FE0F}","\u{2139}\u{FE0F}","\u{2194}\u{FE0F}","\u{2195}\u{FE0F}","\u{2196}\u{FE0F}","\u{2197}\u{FE0F}","\u{2198}\u{FE0F}","\u{2199}\u{FE0F}","\u{219A}\u{FE0F}","\u{219B}\u{FE0F}","\u{21AA}\u{FE0F}","\u{21AB}\u{FE0F}","\u{25AA}\u{FE0F}","\u{25AB}\u{FE0F}","\u{25B6}\u{FE0F}","\u{25C0}\u{FE0F}","\u{25FB}\u{FE0F}","\u{25FC}\u{FE0F}","\u{25FD}\u{FE0F}","\u{25FE}\u{FE0F}","\u{2934}\u{FE0F}","\u{2935}\u{FE0F}","\u{2B05}\u{FE0F}","\u{2B06}\u{FE0F}","\u{2B07}\u{FE0F}","\u{3030}\u{FE0F}","\u{303D}\u{FE0F}","\u{3297}\u{FE0F}","\u{3299}\u{FE0F}"]
    };

    function wireEmojiModal() {
        var grid = $("#emojiGrid");
        var catsWrap = $("#emojiCats");
        var searchInput = $("#emojiSearch");
        var currentCat = "Smileys";

        var catLabels = { "Smileys": "\u{1F600}", "Gestures": "\u{1F44B}", "Hearts": "\u{2764}\u{FE0F}", "Nature": "\u{1F33F}", "Food": "\u{1F34E}", "Activities": "\u{1F3AE}", "Objects": "\u{1F4A1}", "Symbols": "\u{2B50}" };

        function renderCats() {
            var html = "";
            Object.keys(EMOJI_DATA).forEach(function (cat) {
                html += '<button class="thr-emoji-cat' + (cat === currentCat ? " active" : "") + '" data-cat="' + cat + '">' + catLabels[cat] + '</button>';
            });
            catsWrap.innerHTML = html;
        }

        function renderGrid(filter) {
            var emojis = [];
            if (filter) {
                var q = filter.toLowerCase();
                Object.keys(EMOJI_DATA).forEach(function (cat) {
                    EMOJI_DATA[cat].forEach(function (e) { emojis.push(e); });
                });
            } else {
                emojis = EMOJI_DATA[currentCat] || [];
            }
            var html = emojis.map(function (e) {
                return '<button class="thr-emoji-item" data-emoji="' + e + '">' + e + '</button>';
            }).join("");
            grid.innerHTML = html || '<div class="thr-emoji-empty">No emojis found</div>';
        }

        renderCats();
        renderGrid();

        catsWrap.addEventListener("click", function (e) {
            var btn = e.target.closest(".thr-emoji-cat");
            if (!btn) return;
            currentCat = btn.getAttribute("data-cat");
            searchInput.value = "";
            renderCats();
            renderGrid();
        });

        grid.addEventListener("click", function (e) {
            var btn = e.target.closest(".thr-emoji-item");
            if (!btn) return;
            var emoji = btn.getAttribute("data-emoji");
            var input = $("#msgInput");
            var pos = input.selectionStart || input.value.length;
            input.value = input.value.slice(0, pos) + emoji + input.value.slice(pos);
            input.focus();
            input.selectionStart = input.selectionEnd = pos + emoji.length;
            autoGrow(input);
        });

        var searchT;
        searchInput.addEventListener("input", function () {
            clearTimeout(searchT);
            var val = this.value;
            searchT = setTimeout(function () { renderGrid(val.trim()); }, 150);
        });

        // Plus button menu (like community chat)
        $("#btnPlus").addEventListener("click", function (e) {
            e.stopPropagation();
            var menu = $("#plusMenu");
            var isOpen = !menu.classList.contains("hidden");
            menu.classList.toggle("hidden", isOpen);
        });
        document.addEventListener("click", function (e) {
            var menu = $("#plusMenu");
            var btn = $("#btnPlus");
            if (menu && !menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
                menu.classList.add("hidden");
            }
        });
        $("#plusMenu").addEventListener("click", function (e) {
            var item = e.target.closest(".thr-plus-item");
            if (!item) return;
            var action = item.dataset.action;
            $("#plusMenu").classList.add("hidden");
            if (action === "emoji") {
                openModal("modalEmoji");
                setTimeout(function () { document.querySelector("#modalEmoji .thr-emoji-search").focus(); }, 100);
            } else if (action === "gif") {
                openModal("modalGif");
                setTimeout(function () { var gi = $("#gifSearch"); if (gi) gi.focus(); }, 100);
            } else if (action === "anime") {
                openModal("modalAnime");
                var ai = $("#animeSearch");
                if (ai) { ai.value = ""; var ag = $("#animeResults"); if (ag) ag.innerHTML = ""; setTimeout(function () { ai.focus(); }, 100); }
            } else if (action === "attach") {
                $("#fileInput").click();
            }
        });

    }

    // ---- Members modal ----
    function wireMembersModal() {
        var list = $("#memberList");
        var addRow = $("#memberAddRow");
        var sInput = $("#memberSearch");
        var sResults = $("#memberResults");

        function render() {
            var conv = State.active && State.active.conv;
            if (!conv || conv.type !== "group") return;
            var canManage = meIsOwner();
            $("#membersCount").textContent = State.members.length;
            addRow.classList.toggle("hidden", !canManage);
            list.innerHTML = State.members.map(function (m) {
                var role = m.role === "owner" ? '<span class="thr-role-chip owner">Owner</span>'
                    : m.role === "admin" ? '<span class="thr-role-chip">Admin</span>' : "";
                var p = State.presence[m.id];
                var dot = p ? '<span class="thr-dot ' + (p.online ? "online" : "offline") + '"></span>' : "";
                var remove = "";
                if (canManage && m.id !== State.me.id) {
                    remove = '<button class="thr-link-btn danger" data-kick="' + m.id + '">Remove</button>';
                }
                if (m.id === State.me.id) {
                    remove = '<button class="thr-link-btn danger" data-leave="1">Leave group</button>';
                }
                return '<div class="thr-member-row">' +
                    '<span class="thr-avatar thr-avatar-md" style="background:' + escapeHtml(m.avatar_color) + '">' +
                    avatarInner(m) + "</span>" + dot +
                    '<span class="thr-member-name" data-uid="' + m.id + '">' + escapeHtml(m.username) + (m.id === State.me.id ? " (you)" : "") + "</span>" +
                    role + "<span class='thr-member-actions'>" + remove + "</span></div>";
            }).join("");
        }

        function search(q) {
            if (!q) { sResults.innerHTML = ""; return; }
            api("/threads/api/users/search?q=" + encodeURIComponent(q)).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                var ids = {};
                State.members.forEach(function (m) { ids[m.id] = true; });
                sResults.innerHTML = res.users.filter(function (u) { return !ids[u.id]; }).map(function (u) {
                    return '<div class="thr-user-row" data-uid="' + u.id + '">' +
                        '<span class="thr-avatar thr-avatar-md" style="background:' + escapeHtml(u.avatar_color) + '">' +
                        avatarInner(u) + "</span><span>" + escapeHtml(u.username) + "</span></div>";
                }).join("") || '<div class="thr-dropdown-empty">All members already added</div>';
            });
        }

        var t;
        sInput.addEventListener("input", function () {
            clearTimeout(t);
            t = setTimeout(function () { search(sInput.value.trim()); }, 250);
        });
        sResults.addEventListener("click", function (e) {
            var row = e.target.closest(".thr-user-row");
            if (!row) return;
            var uid = parseInt(row.getAttribute("data-uid"), 10);
            api("/threads/api/conversations/" + State.active.id + "/members", { json: { user_id: uid } }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                sInput.value = "";
                sResults.innerHTML = "";
                loadMembers();
                refreshConversations();
            });
        });
        list.addEventListener("click", function (e) {
            var kick = e.target.closest("[data-kick]");
            if (kick) {
                var uid = parseInt(kick.getAttribute("data-kick"), 10);
                api("/threads/api/conversations/" + State.active.id + "/members/" + uid, { method: "DELETE" }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    loadMembers();
                    refreshConversations();
                });
                return;
            }
            if (e.target.closest("[data-leave]")) {
                api("/threads/api/conversations/" + State.active.id + "/members/" + State.me.id, { method: "DELETE" }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    closeModal("modalMembers");
                    toast("You left the group");
                    handleLeftConversation(res.conversation_gone);
                });
            }
        });

        $("#btnMembers").addEventListener("click", function () {
            if (isChannelOpen()) { openCommunityMenu("members"); return; }
            openModal("modalMembers");
            render();
            loadMembers();
        });

        function loadMembers() {
            if (!State.active) return;
            api("/threads/api/messages?ctx=" + State.active.type + ":" + State.active.id + "&limit=1").then(function (res) {
                if (res.success && res.members) {
                    State.members = res.members;
                    State.memberMap = {};
                    State.members.forEach(function (m) { State.memberMap[m.id] = m; });
                    render();
                    refreshConversations();
                }
            });
        }
    }

    function handleLeftConversation(gone) {
        if (gone) {
            State.active = null;
            $("#convView").classList.add("hidden");
            $("#emptyState").classList.remove("hidden");
        }
        refreshConversations();
    }
        // ============================================================
    // Communities tab (Phase 2)
    // ============================================================

    function setTab(tab) {
        State.activeTab = tab;
        $$(".thr-tab").forEach(function (x) {
            x.classList.toggle("active", x.getAttribute("data-tab") === tab);
        });
        var isComm = tab === "communities";
        $("#commRail").classList.toggle("hidden", !isComm);
        $(".thr-left").classList.toggle("hidden", isComm);
        if (!isComm) {
            $("#channelPanel").classList.add("hidden");
            $("#discoverPanel").classList.add("hidden");
            if (isChannelOpen()) {
                State.active = null;
                $("#convView").classList.add("hidden");
                $("#emptyState").classList.remove("hidden");
            }
            renderConversations();
            return;
        }
        $("#channelPanel").classList.toggle("hidden", State.discoverMode);
        $("#discoverPanel").classList.toggle("hidden", !State.discoverMode);
        // Never auto-open Discover. The rail always shows the user's guilds;
        // Discover only opens when the user clicks the compass button or
        // types in the discover search. This fixes guilds "missing" from the
        // rail and unwanted public guilds appearing on open.
        if (!State.discoverMode) {
            if (!State.activeCommunity) {
                if (State.communities.length) {
                    var c = State.communities[0];
                    State.activeCommunity = c;
                    State.myCommunityRole = c.role || "member";
                    renderRail();
                    renderChannelPanel();
                    if (c.channels && c.channels.length) openChannel(c.channels[0]);
                } else {
                    // No guilds yet: show the empty rail with a hint to create/discover.
                    renderRail();
                    $("#channelPanel").classList.add("hidden");
                }
            } else if (!isChannelOpen() && State.activeCommunity.channels && State.activeCommunity.channels.length) {
                openChannel(State.activeCommunity.channels[0]);
            } else {
                renderChannelPanel();
            }
        }
        refreshCommunities();
    }

    function showDiscover() {
        State.discoverMode = true;
        $("#channelPanel").classList.add("hidden");
        $("#discoverPanel").classList.remove("hidden");
        loadDiscover();
    }

    function renderRail() {
        var html = "";
        // Flag guilds whose names collide with another of your guilds so we can
        // label them (they're separate guilds, not a duplicate row).
        var counts = {};
        State.communities.forEach(function (c) { counts[escapeHtml(c.name || "")] = (counts[escapeHtml(c.name || "")] || 0) + 1; });
        State.communities.forEach(function (c) {
            var active = State.activeCommunity && State.activeCommunity.id === c.id;
            var dup = counts[escapeHtml(c.name || "")] > 1;
            var roleTag = c.role === "owner" ? " (owner)" : c.role === "moderator" ? " (mod)" : "";
            var title = escapeHtml(c.name || "") + (dup ? " · " + (c.member_count || "?") + " members" + roleTag : (roleTag ? roleTag : ""));
            html += '<div class="thr-rail-item' + (active ? " active" : "") + '" data-comm="' + c.id + '" title="' +
                title + '">' +
                (c.icon_url
                    ? '<span class="thr-rail-icon thr-rail-icon-img" style="background:' + escapeHtml(c.icon_color || "#8b5cf6") + '"><img src="' + escapeHtml(c.icon_url) + '" alt="" loading="lazy" onerror="this.style.display=\'none\';this.parentNode.textContent=\'' + escapeHtml(initials(c.name)) + '\'"></span>'
                    : '<span class="thr-rail-icon" style="background:' + escapeHtml(c.icon_color || "#8b5cf6") + '">' + escapeHtml(initials(c.name)) + "</span>") +

                (c.unread ? '<span class="thr-unread-badge thr-rail-badge">' + (c.unread > 99 ? "99+" : c.unread) + "</span>" : "") +
                "</div>";
        });
        $("#commRailList").innerHTML = html || '<div class="thr-rail-empty" title="Join or create a guild">+</div>';
    }

    function renderChannelPanel() {
        var c = State.activeCommunity;
        if (!c) return;
        $("#commName").textContent = c.name || "";
        $("#commMeta").textContent = (c.is_public ? "Public · " : "Private · ") + (c.member_count || 0) + " members" + (c.genre ? " · " + c.genre : "");
        var headAv = $("#commHeadAvatar");
        if (c.icon_url) {
            headAv.innerHTML = '<img src="' + escapeHtml(c.icon_url) + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">';
            headAv.classList.remove("hidden");
        } else if (headAv) {
            headAv.classList.add("hidden");
        }
        var rules = $("#commRules");
        if (c.rules) {
            rules.innerHTML = '<i class="fas fa-scroll"></i> ' + escapeHtml(c.rules);
            rules.classList.remove("hidden");
        } else {
            rules.classList.add("hidden");
        }
        renderChannelList();
        renderPartyList();
    }

    function renderChannelList() {
        var c = State.activeCommunity;
        if (!c) return;
        var filter = State.commFilter.toLowerCase();
        var html = "";
        (c.channels || []).forEach(function (ch) {
            if (filter && ch.name.indexOf(filter) === -1) return;
            var active = State.active && State.active.type === "channel" && State.active.id === ch.id;
            html += '<div class="thr-channel' + (active ? " active" : "") + '" data-ch="' + ch.id + '">' +
                '<span class="thr-channel-name"># ' + escapeHtml(ch.name) + "</span>" +
                (ch.has_live_party ? '<span class="thr-live-dot" title="Watch party live">\uD83D\uDD34</span>' : "") +
                (ch.unread ? '<span class="thr-unread-badge">' + (ch.unread > 99 ? "99+" : ch.unread) + "</span>" : "") +
                "</div>";
        });
        if (isCommMod()) {
            html += '<div class="thr-channel thr-channel-add" id="btnAddChannel"><span class="thr-channel-name">+ New channel</span></div>';
        }
        $("#channelList").innerHTML = html || '<div class="thr-conv-empty">No channels match.</div>';
    }

    function renderPartyList() {
        var c = State.activeCommunity;
        if (!c) return;
        var parties = c.parties || [];
        var html = parties.map(function (p) {
            var live = p.is_live ? '<span class="thr-live-dot">\uD83D\uDD34</span> ' : "";
            return '<div class="thr-party-row" data-party="' + p.id + '" data-ch="' + p.channel_id + '" title="Open #' +
                escapeHtml(p.channel_name || "") + '">' +
                live + "<b>" + escapeHtml(p.title) + "</b>" +
                '<span class="thr-party-meta">' + escapeHtml(p.anime_title || p.anime_id || "") +
                " · " + fmtConvTime(p.scheduled_time) + " · " + (p.rsvp_count || 0) + " going</span></div>";
        }).join("");
        $("#partyList").innerHTML = html || '<div class="thr-conv-empty">No watch parties yet — host one from a channel!</div>';
    }

    function openChannel(ch) {
        if (!ch) return;
        State.active = { type: "channel", id: ch.id, conv: ch };
        State.replyTo = null;
        State.attach = null;
        State.editingId = null;
        // Invalidate every in-flight request from the previous conversation --
        // without this, a DM's pending poll/history response could render its
        // messages inside this guild channel (the "merge" bug).
        State.reqSeq++;
        var seq = State.reqSeq;
        var key = "channel:" + ch.id;
        State.newSinceId = ch.last_read_message_id || 0;
        State.messages = [];
        State.seenIds = {};
        State.afterId = 0;
        State.firstId = 0;
        State.hasMore = true;
        State.loadingOlder = false;
        State.polls = [];
        State.parties = [];

        // CRITICAL: clear the DOM immediately so old messages from the
        // previous context don't flash before loadHistory resolves.
        $("#msgList").innerHTML = '';
        $("#emptyState").classList.add("hidden");
        $("#convView").classList.remove("hidden");
        renderChatHead();

        var cached = msgCache[key];
        if (cached && cached.messages.length) {
            State.messages = cached.messages;
            State.messages.forEach(function (m) { State.seenIds[m.id] = true; });
            State.afterId = cached.afterId;
            State.firstId = cached.firstId;
            State.hasMore = cached.hasMore;
            State.members = cached.members || [];
            State.memberMap = {};
            State.members.forEach(function (m) { State.memberMap[m.id] = m; });
            State.pins = cached.pins || [];
            State.polls = cached.polls || [];
            State.parties = cached.parties || [];
            if (cached.html) {
                var _ml = $("#msgList");
                _ml.innerHTML = cached.html;
                _ml.scrollTop = _ml.scrollHeight;
                updateSeenText();
                // Re-fetch ranks when the cached HTML was saved without badges,
                // so rank badges / XP bars stay visible after visiting a guild.
                fetchThrRanks(State.messages).then(function (n) {
                    if (n > 0 && seq === State.reqSeq && $("#msgList") && $("#msgList").innerHTML) {
                        renderMessages(false);
                        updateSeenText();
                    }
                });
            } else {
                fetchThrRanks(State.messages).then(function () {
                    if (seq !== State.reqSeq) return;
                    renderMessages(true);
                    renderPins(State.pins);
                    renderPartyStrip();
                    updateSeenText();
                });
            }
            pollMessages();
            markActiveRead();
            $("#msgInput").focus();
            renderChannelList();
            return;
        }

        State.loadingHistory = true;
        loadHistory();
        markActiveRead();
        $("#msgInput").focus();
        renderChannelList();
    }

    function refreshCommunities(cb) {
        api("/threads/api/communities").then(function (res) {
            if (!res.success) { if (cb) cb(); return; }
            var prevActiveId = State.activeCommunity ? State.activeCommunity.id : null;
            State.communities = res.communities || [];
            renderRail();
            if (State.activeCommunity) {
                var fresh = null;
                State.communities.forEach(function (c) { if (c.id === prevActiveId) fresh = c; });
                if (fresh) {
                    State.activeCommunity = fresh;
                    renderChannelPanel();
                    if (State.active && State.active.type === "channel") {
                        var ch = null;
                        (fresh.channels || []).forEach(function (x) { if (x.id === State.active.id) ch = x; });
                        if (ch) State.active.conv = ch;
                    }
                } else {
                    State.activeCommunity = null;
                    State.active = null;
                    $("#convView").classList.add("hidden");
                    $("#emptyState").classList.remove("hidden");
                }
            }
            if (cb) cb();
        });
    }

    function loadDiscover(q) {
        q = q || $("#discoverSearch").value.trim();
        api("/threads/api/communities/discover" + (q ? "?q=" + encodeURIComponent(q) : "")).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            State.discoverList = res.communities || [];
            renderDiscover();
        });
    }

    function renderDiscover() {
        var list = State.discoverList || [];
        $("#discoverList").innerHTML = list.map(function (c) {
            return '<div class="thr-discover-card">' +
                '<span class="thr-rail-icon thr-disc-icon" style="background:' + escapeHtml(c.icon_color || "#8b5cf6") + '">' +
                escapeHtml(initials(c.name)) + "</span>" +
                '<div class="thr-disc-body">' +
                '<div class="thr-disc-name">' + escapeHtml(c.name) + "</div>" +
                '<div class="thr-disc-meta">' + (c.member_count || 0) + " members" + (c.genre ? " · " + escapeHtml(c.genre) : "") + "</div>" +
                (c.description ? '<div class="thr-disc-desc">' + escapeHtml(c.description) + "</div>" : "") +
                "</div>" +
                '<button class="thr-btn thr-btn-sm thr-btn-primary" data-join="' + c.id + '">Join</button></div>';
        }).join("") || '<div class="thr-conv-empty">No guilds found — create the first one!</div>';
    }

    // ---- Polls + parties (rendered in channel chat) ----

    function renderPollCard(p) {
        var total = p.total_votes || 0;
        var voted = p.my_option_id != null;
        var opts = (p.options || []).map(function (o) {
            var pct = total ? Math.round((o.votes / total) * 100) : 0;
            return '<div class="thr-poll-opt' + (o.id === p.my_option_id ? " chosen" : "") + '" data-poll="' + p.id +
                '" data-opt="' + o.id + '" title="' + (voted ? "Change your vote" : "Click to vote") + '">' +
                '<span class="thr-poll-bar" style="width:' + pct + '%"></span>' +
                '<span class="thr-poll-text">' + escapeHtml(o.text) + "</span>" +
                '<span class="thr-poll-count">' + o.votes + " · " + pct + "%</span></div>";
        }).join("");
        return '<div class="thr-poll-card" data-pollid="' + p.id + '">' +
            '<div class="thr-poll-head"><i class="fas fa-square-poll-vertical"></i> <b>' + escapeHtml(p.question) + "</b></div>" +
            '<div class="thr-poll-sub">by ' + escapeHtml(p.author) + " · " + total + (total === 1 ? " vote" : " votes") +
            (voted ? ' · <span class="thr-voted-chip">voted</span>' : "") + "</div>" +
            '<div class="thr-poll-opts">' + opts + "</div></div>";
    }

    function renderPartyStrip() {
        var strip = $("#partyStrip");
        var live = State.parties.filter(function (p) { return p.is_live; });
        var upcoming = State.parties.filter(function (p) { return !p.is_live; });
        if (!live.length && !upcoming.length) {
            strip.classList.add("hidden");
            strip.innerHTML = "";
            return;
        }
        var html = "";
        live.forEach(function (p) {
            html += '<div class="thr-party-banner live" data-party="' + p.id + '">' +
                '<span class="thr-live-pulse"></span><i class="fas fa-tv"></i> <b>' + escapeHtml(p.title) + "</b>" +
                (p.anime_title ? " — " + escapeHtml(p.anime_title) : "") + " is live now! " +
                '<button class="thr-link-btn" data-join-party="' + p.id + '">Join party</button>' +
                (p.is_rsvped ? ' <span class="thr-voted-chip">you\u2019re going</span>' : "") + "</div>";
        });
        upcoming.forEach(function (p) {
            html += '<div class="thr-party-banner" data-party="' + p.id + '">' +
                '<i class="fas fa-tv"></i> <b>' + escapeHtml(p.title) + "</b>" +
                (p.anime_title ? " (" + escapeHtml(p.anime_title) + ")" : "") + " · starts " + fmtConvTime(p.scheduled_time) +
                " · " + (p.rsvp_count || 0) + " going " +
                '<button class="thr-link-btn" data-rsvp-party="' + p.id + '">' + (p.is_rsvped ? "Going \u2713" : "RSVP") + "</button>" +
                (p.is_host || isCommMod() ? ' <button class="thr-link-btn danger" data-cancel-party="' + p.id + '">cancel</button>' : "") +
                "</div>";
        });
        strip.innerHTML = html;
        strip.classList.remove("hidden");
    }

    function updateChannelExtras(polls, parties) {
        var pollsChanged = JSON.stringify(polls) !== JSON.stringify(State.polls);
        var partiesChanged = JSON.stringify(parties) !== JSON.stringify(State.parties);
        if (pollsChanged) {
            State.polls = polls || [];
            renderMessages(false);
        }
        if (partiesChanged) {
            State.parties = parties || [];
            renderPartyStrip();
        }
    }

    function loadChannelParties() {
        if (!isChannelOpen()) return;
        api("/threads/api/messages?ctx=channel:" + State.active.id + "&limit=1").then(function (res) {
            if (res.success && res.parties) {
                State.parties = res.parties;
                renderPartyStrip();
            }
        });
    }

    function modDeleteMessage(id) {
        if (!window.confirm("Delete this message as a moderator?")) return;
        api("/threads/api/messages/" + id, { method: "DELETE" }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            State.messages.forEach(function (m, i) {
                if (m.id === id) { State.messages[i].deleted_at = "yes"; State.messages[i].content = ""; }
            });
            renderMessages(false);
            toast("Message deleted");
        });
    }

    // ---- Community menu modal ----

    var _inviteLinkCache = {};
    function loadInviteLink() {
        var c = State.activeCommunity;
        if (!c) return;
        var input = $("#commInviteLink");
        if (_inviteLinkCache[c.id]) {
            if (input) input.value = _inviteLinkCache[c.id];
            return;
        }
        api("/threads/api/communities/" + c.id + "/invite").then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            var link = location.origin + "/threads?invite=" + res.invite_code;
            _inviteLinkCache[c.id] = link;
            if (input) input.value = link;
        });
    }

    function loadInviteFriends() {
        var box = $("#commInviteFriends");
        if (!box) return;
        api("/threads/api/friends").then(function (res) {
            if (!res.success) { box.innerHTML = '<div class="thr-dropdown-empty">Could not load friends</div>'; return; }
            var friends = res.friends || [];
            if (!friends.length) {
                box.innerHTML = '<div class="thr-dropdown-empty">No friends yet — add friends to invite them</div>';
                return;
            }
            box.innerHTML = friends.map(function (u) {
                return '<div class="thr-user-row" data-send-invite="' + u.id + '">' +
                    '<span class="thr-avatar thr-avatar-md" style="background:' + escapeHtml(u.avatar_color || "#8b5cf6") + '">' +
                    avatarInner(u) + "</span><span>" + escapeHtml(u.username) + "</span>" +
                    '<button class="thr-btn thr-btn-sm thr-btn-primary">Send</button></div>';
            }).join("");
        });
    }

    function sendInviteToFriend(uid) {
        var c = State.activeCommunity;
        if (!c) return;
        var btn = null;
        $$("#commInviteFriends [data-send-invite]").forEach(function (row) {
            if (parseInt(row.getAttribute("data-send-invite"), 10) === uid) {
                btn = row.querySelector("button");
            }
        });
        if (btn) btn.disabled = true;
        api("/threads/api/communities/" + c.id + "/invite/send", { json: { user_id: uid } }).then(function (res) {
            if (btn) btn.disabled = false;
            if (!res.success) { handleApiError(res); return; }
            toast("Invite sent to your friend!");
        }).catch(function () { if (btn) btn.disabled = false; });
    }

    var _commDetailCache = {};
    function openCommunityMenu(tab) {
        var c = State.activeCommunity;
        if (!c) return;
        $("#commModalTitle").textContent = c.name || "Community";
        $("#commEditName").value = c.name || "";
        $("#commEditGenre").value = c.genre || "";
        $("#commEditDesc").value = c.description || "";
        $("#commEditRules").value = c.rules || "";
        seedVisToggle("#modalCommunity", c.is_public !== 0);
        $("#btnMuteCommunity").textContent = c.muted ? "Unmute guild" : "Mute guild";
        _editCommAvatar = c.icon_url || null;
        if (State._editPicker) State._editPicker.set(_editCommAvatar);
        var canMod = isCommMod();
        var isOwnerGuild = State.communities.some(function (x) { return x.id === c.id; }) && !!(c.role === "owner");
        var delBtn = document.getElementById("btnDeleteCommunity");
        if (delBtn) delBtn.classList.toggle("hidden", !isOwnerGuild);
        $$(".thr-comm-tab").forEach(function (t) {
            var name = t.getAttribute("data-ctab");
            t.classList.toggle("hidden", !canMod && name !== "info" && name !== "members");
        });
        // Open the modal INSTANTLY using cached detail if we have it, then
        // refresh in the background so members/roles are always current.
        var target = tab || "info";
        var cachedDetail = _commDetailCache[c.id];
        if (cachedDetail) {
            State.communityDetail = cachedDetail;
            openModal("modalCommunity");
            $$(".thr-comm-tab").forEach(function (t) {
                t.classList.toggle("active", t.getAttribute("data-ctab") === target);
            });
            showCommTab(target);
            loadInviteLink();
            loadInviteFriends();
        } else {
            // Show the modal right away with whatever we have, then fill in.
            openModal("modalCommunity");
            $$(".thr-comm-tab").forEach(function (t) {
                t.classList.toggle("active", t.getAttribute("data-ctab") === target);
            });
            showCommTab(target);
        }
        api("/threads/api/communities/" + c.id).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            State.communityDetail = res;
            _commDetailCache[c.id] = res;
            loadInviteLink();
            loadInviteFriends();
            var activeTab = null;
            $$(".thr-comm-tab").forEach(function (t) {
                if (t.classList.contains("active")) activeTab = t.getAttribute("data-ctab");
            });
            showCommTab(activeTab || target);
        });
    }

    function showCommTab(tab) {
        ["info", "members", "modlog", "reports"].forEach(function (x) {
            $("#ctab" + x.charAt(0).toUpperCase() + x.slice(1)).classList.toggle("hidden", x !== tab);
        });
        if (tab === "members") renderCommMembers();
        if (tab === "modlog") loadModlog();
        if (tab === "reports") renderCommReports();
    }

    function renderCommMembers() {
        var d = State.communityDetail;
        if (!d) return;
        var me = State.me;
        var canMod = d.my_role === "owner" || d.my_role === "moderator";
        var isOwner = d.my_role === "owner";
        var html = (d.members || []).map(function (m) {
            var role = m.role === "owner" ? '<span class="thr-role-chip owner">Owner</span>'
                : m.role === "moderator" ? '<span class="thr-role-chip">Mod</span>' : "";
            var actions = "";
            if (canMod && m.id !== me.id && m.role !== "owner") {
                if (isOwner) {
                    actions += m.role === "moderator"
                        ? '<button class="thr-link-btn" data-uid="' + m.id + '" data-role="member">Demote</button>'
                        : '<button class="thr-link-btn" data-uid="' + m.id + '" data-role="moderator">Make mod</button>';
                }
                actions += '<button class="thr-link-btn danger" data-kick="' + m.id + '">Kick</button>';
                actions += '<button class="thr-link-btn danger" data-mute="' + m.id + '" data-muted="' + (m.muted ? "1" : "0") + '">' + (m.muted ? "Unmute" : "Mute") + "</button>";
                actions += '<button class="thr-link-btn danger" data-ban="' + m.id + '">Ban</button>';
            }
            if (m.id !== me.id) {
                actions += '<button class="thr-link-btn danger" data-block="' + m.id + '">Block</button>';
            }
            return '<div class="thr-member-row">' +
                '<span class="thr-avatar thr-avatar-md" style="background:' + escapeHtml(m.avatar_color) + '">' +
                avatarInner(m) + "</span>" +
                '<span class="thr-member-name" data-uid="' + m.id + '">' + escapeHtml(m.username) + (m.id === me.id ? " (you)" : "") + "</span>" +
                (m.muted ? '<span class="thr-muted-chip">muted</span>' : "") + role +
                '<span class="thr-member-actions">' + actions + "</span></div>";
        }).join("");
        var bannedHtml = "";
        if (d.banned && d.banned.length) {
            bannedHtml = '<div class="thr-banned-head">Banned users</div>' + d.banned.map(function (b) {
                return '<div class="thr-member-row"><span class="thr-avatar thr-avatar-md thr-avatar-dim" style="background:#4b5267">' +
                    avatarInner(b) + "</span><span class='thr-member-name'>" + escapeHtml(b.username) +
                    "</span><span class='thr-member-actions'><button class='thr-link-btn' data-unban='" + b.id + "'>Unban</button></span></div>";
            }).join("");
        }
        $("#commMemberList").innerHTML = html || '<div class="thr-dropdown-empty">No members</div>';
        $("#commBannedList").innerHTML = bannedHtml;
    }

    function loadModlog() {
        if (!State.activeCommunity) return;
        api("/threads/api/communities/" + State.activeCommunity.id + "/modlog").then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            var log = res.log || [];
            var ACTION_LABELS = {
                update_community: "Edited guild",
                create_channel: "Created channel",
                update_channel: "Updated channel",
                delete_channel: "Deleted channel",
                kick: "Kicked member",
                ban: "Banned member",
                unban: "Unbanned member",
                mute: "Muted member",
                unmute: "Unmuted member",
                set_role: "Changed role",
            };
            $("#commModlog").innerHTML = log.length ? log.map(function (l) {
                return '<div class="thr-modlog-row"><b>' + escapeHtml(ACTION_LABELS[l.action] || l.action) + "</b> by " +
                    escapeHtml(l.actor || "?") + (l.target ? " → " + escapeHtml(l.target) : "") +
                    (l.reason ? " — " + escapeHtml(l.reason) : "") +
                    '<span class="thr-modlog-time">' + fmtConvTime(l.created_at) + "</span></div>";
            }).join("") : '<div class="thr-dropdown-empty">No moderation actions yet</div>';
        });
    }

    function renderCommReports() {
        var d = State.communityDetail;
        var reports = (d && d.reports) || [];
        $("#commReports").innerHTML = reports.length ? reports.map(function (r) {
            return '<div class="thr-report-row"><i class="fas fa-flag"></i><div>' +
                "<div><b>Report #" + r.id + "</b> by " + escapeHtml(r.reporter) + "</div>" +
                '<div class="thr-report-msg">' + escapeHtml(String(r.content || "").slice(0, 200)) + "</div>" +
                (r.reason ? '<div class="thr-report-reason">' + escapeHtml(r.reason) + "</div>" : "") +
                '</div><button class="thr-btn thr-btn-sm" data-resolve-report="' + r.id + '">Dismiss</button></div>';
        }).join("") : '<div class="thr-dropdown-empty">No open reports</div>';
    }

    var _editCommAvatar = null;
    function saveCommunityEdit() {
        var c = State.activeCommunity;
        if (!c) return;
        api("/threads/api/communities/" + c.id, {
            method: "PATCH",
            json: {
                name: $("#commEditName").value,
                genre: $("#commEditGenre").value,
                description: $("#commEditDesc").value,
                icon_url: _editCommAvatar,
                rules: $("#commEditRules").value,
                is_public: thrVisValue("#modalCommunity"),
            },
        }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            toast("Guild updated");
            closeModal("modalCommunity");
            refreshCommunities();
        });
    }

    function leaveCommunityAction() {
        var c = State.activeCommunity;
        if (!c) return;
        if (!window.confirm("Leave " + c.name + "?")) return;
        api("/threads/api/communities/" + c.id + "/leave", { json: {} }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            closeModal("modalCommunity");
            State.activeCommunity = null;
            State.active = null;
            $("#convView").classList.add("hidden");
            $("#emptyState").classList.remove("hidden");
            refreshCommunities();
            showDiscover();
        });
    }

    function toggleCommunityMute() {
        var c = State.activeCommunity;
        if (!c) return;
        var next = !c.muted;
        api("/threads/api/communities/" + c.id + "/mute", { json: { muted: next } }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            c.muted = next;
            toast(next ? "Guild muted — no unread badges" : "Unmuted");
            $("#btnMuteCommunity").textContent = next ? "Unmute guild" : "Mute guild";
            refreshCommunities();
        });
    }

    function deleteCommunityAction() {
        var c = State.activeCommunity;
        if (!c) return;
        if (!window.confirm("Permanently delete \"" + c.name + "\" and all its channels, messages, parties and members? This CANNOT be undone.")) return;
        if (!window.confirm("Are you really sure? Delete \"" + c.name + "\" forever?")) return;
        api("/threads/api/communities/" + c.id + "/delete", { json: {} }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            closeModal("modalCommunity");
            State.activeCommunity = null;
            State.active = null;
            $("#convView").classList.add("hidden");
            $("\#emptyState").classList.remove("hidden");
            refreshCommunities();
            toast("Guild deleted");
        });
    }

    function submitReport() {
        if (!State.reportMessageId) return;
        api("/threads/api/messages/" + State.reportMessageId + "/report", {
            json: { reason: $("#reportReason").value },
        }).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            closeModal("modalReport");
            toast("Thanks — report sent to the moderators");
        });
    }
        // ---- Modals: poll, party, new community ----

    function renderPollModalOptions() {
        var vals = $$("#pollOptions .thr-text-input").map(function (i) { return i.value; });
        if (vals.length < 2) vals.push("");
        var html = vals.map(function (v, i) {
            return '<div class="thr-poll-option-row">' +
                '<input class="thr-text-input" type="text" maxlength="120" placeholder="Option ' + (i + 1) + '" value="' + escapeHtml(v) + '">' +
                (i > 1 ? '<button class="thr-link-btn danger" data-del-opt="' + i + '">\u2715</button>' : "") + "</div>";
        }).join("");
        $("#pollOptions").innerHTML = html;
    }

    var partyPick = null;

    function wirePollModal() {
        renderPollModalOptions();
        $("#btnAddPollOption").addEventListener("click", function () {
            var vals = $$("#pollOptions .thr-text-input").map(function (i) { return i.value; });
            if (vals.length >= 8) { toast("Max 8 options", "error"); return; }
            vals.push("");
            var html = vals.map(function (v, i) {
                return '<div class="thr-poll-option-row">' +
                    '<input class="thr-text-input" type="text" maxlength="120" placeholder="Option ' + (i + 1) + '" value="' + escapeHtml(v) + '">' +
                    (i > 1 ? '<button class="thr-link-btn danger" data-del-opt="' + i + '">\u2715</button>' : "") + "</div>";
            }).join("");
            $("#pollOptions").innerHTML = html;
        });
        $("#pollOptions").addEventListener("click", function (e) {
            var b = e.target.closest("[data-del-opt]");
            if (!b) return;
            var inputs = $$("#pollOptions .thr-text-input");
            var idx = parseInt(b.getAttribute("data-del-opt"), 10);
            if (inputs[idx]) inputs[idx].remove();
        });
        $("#btnCreatePoll").addEventListener("click", function () {
            if (!isChannelOpen()) return;
            var question = $("#pollQuestion").value.trim();
            var options = $$("#pollOptions .thr-text-input").map(function (i) { return i.value.trim(); }).filter(Boolean);
            if (!question) { toast("Ask a question", "error"); return; }
            if (options.length < 2) { toast("Add at least 2 options", "error"); return; }
            api("/threads/api/channels/" + State.active.id + "/polls", {
                json: { question: question, options: options },
            }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                State.polls = res.polls || [];
                renderMessages(false);
                closeModal("modalPoll");
                toast("Poll posted");
            });
        });
    }

    function wirePartyModal() {
        var sInput = $("#partyAnimeSearch");
        var results = $("#partyAnimeResults");
        var t;
        function search() {
            var q = sInput.value.trim();
            if (!q) { results.innerHTML = ""; return; }
            fetch("/api/search?q=" + encodeURIComponent(q)).then(function (r) { return r.json(); }).then(function (res) {
                if (!res.success) { results.innerHTML = ""; return; }
                results.innerHTML = res.results.map(function (a) {
                    return '<div class="thr-user-row thr-party-anime-row" data-slug="' + escapeHtml(a.slug) + '" data-title="' + escapeHtml(a.title) + '">' +
                        (a.image ? '<img class="thr-party-anime-thumb" src="' + escapeHtml(a.image) + '" alt="">' : "") +
                        "<span>" + escapeHtml(a.title) + "</span></div>";
                }).join("") || '<div class="thr-dropdown-empty">No anime found</div>';
            });
        }
        sInput.addEventListener("input", function () {
            clearTimeout(t);
            t = setTimeout(search, 300);
        });
        results.addEventListener("click", function (e) {
            var row = e.target.closest(".thr-party-anime-row");
            if (!row) return;
            partyPick = { slug: row.getAttribute("data-slug"), title: row.getAttribute("data-title") };
            $("#partyAnimePick").innerHTML = '<i class="fas fa-tv"></i> Watching: <b>' + escapeHtml(partyPick.title) +
                "</b> <button class='thr-link-btn' data-clear-party-pick='1'>\u2715</button>";
            $("#partyAnimePick").classList.remove("hidden");
            results.innerHTML = "";
            sInput.value = "";
        });
        $("#partyAnimePick").addEventListener("click", function (e) {
            if (e.target.closest("[data-clear-party-pick]")) {
                partyPick = null;
                $("#partyAnimePick").classList.add("hidden");
                $("#partyAnimePick").innerHTML = "";
            }
        });
        $("#btnCreateParty").addEventListener("click", function () {
            if (!isChannelOpen()) return;
            var title = $("#partyTitleInput").value.trim();
            var when = $("#partyWhen").value;
            if (!title) { toast("Name the party", "error"); return; }
            if (!when) { toast("Pick a start time", "error"); return; }
            var iso = new Date(when).toISOString();
            api("/threads/api/channels/" + State.active.id + "/parties", {
                json: { title: title, anime_id: partyPick ? partyPick.slug : "", scheduled_time: iso },
            }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                State.parties = res.parties || [];
                renderPartyStrip();
                closeModal("modalParty");
                toast("Watch party created — watch for the \uD83D\uDD34 flag!");
                refreshCommunities();
            });
        });
    }

    var COMM_COLORS = ["#8b5cf6", "#ef4444", "#f59e0b", "#22c55e", "#3b82f6", "#ec4899", "#06b6d4", "#f97316", "#14b8a6"];
    function wireGuildAvatarPickers() {
        // Reusable anime-avatar search for the create-guild modal and the
        // guild info tab. Picks an anime cover image as the guild's pfp.
        function setup(cfg) {
            var preview = $(cfg.preview);
            var searchBox = $(cfg.searchBox);
            var query = $(cfg.query);
            var results = $(cfg.results);
            var btnPick = $(cfg.btnPick);
            var btnClear = $(cfg.btnClear);
            var current = cfg.initial || null;

            function renderPreview() {
                if (current) {
                    preview.style.backgroundImage = "url('" + escapeHtml(current) + "')";
                    preview.style.backgroundSize = "cover";
                    preview.style.backgroundPosition = "center";
                    preview.textContent = "";
                    if (btnClear) btnClear.hidden = false;
                } else {
                    preview.style.backgroundImage = "";
                    preview.textContent = cfg.initialText || "G";
                    if (btnClear) btnClear.hidden = true;
                }
            }
            renderPreview();

            var preloadedDefault = false;
            function runSearch(q) {
                results.innerHTML = '<div class="thr-gif-loading"><i class="fas fa-spinner fa-spin"></i></div>';
                api("/api/search?q=" + encodeURIComponent(q)).then(function (res) {
                    if (!res.success || !res.results.length) {
                        results.innerHTML = '<div class="thr-anime-hint">No results</div>';
                        return;
                    }
                    results.innerHTML = res.results.slice(0, 20).map(function (a) {
                        return '<div class="thr-avatar-pick" data-img="' + escapeHtml(a.image || "") + '" data-title="' + escapeHtml(a.title) + '">' +
                            '<img src="' + escapeHtml(a.image || "") + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">' +
                            '<span>' + escapeHtml(a.title) + "</span></div>";
                    }).join("");
                }).catch(function () { results.innerHTML = '<div class="thr-anime-hint">Search failed</div>'; });
            }
            btnPick.addEventListener("click", function () {
                searchBox.classList.toggle("hidden");
                if (!searchBox.classList.contains("hidden")) {
                    query.focus();
                    // Preload a popular default list so the picker feels instant.
                    if (!preloadedDefault && !query.value.trim()) {
                        preloadedDefault = true;
                        runSearch("attack on titan");
                    }
                }
            });
            if (btnClear) {
                btnClear.addEventListener("click", function () {
                    current = null;
                    cfg.onChange(null);
                    results.innerHTML = "";
                    searchBox.classList.add("hidden");
                    renderPreview();
                });
            }
            var t;
            query.addEventListener("input", function () {
                clearTimeout(t);
                var q = query.value.trim();
                if (!q) { results.innerHTML = ""; return; }
                t = setTimeout(function () { runSearch(q); }, 250);
            });
            results.addEventListener("click", function (e) {
                var pick = e.target.closest(".thr-avatar-pick");
                if (!pick) return;
                current = pick.getAttribute("data-img");
                cfg.onChange(current);
                results.innerHTML = "";
                searchBox.classList.add("hidden");
                query.value = "";
                renderPreview();
            });
            return { get current() { return current; }, set(v) { current = v; renderPreview(); } };
        }

        // Create-guild picker
        var createPicker = setup({
            preview: "#commAvatarPreview",
            searchBox: "#commAvatarSearch",
            query: "#commAvatarQuery",
            results: "#commAvatarResults",
            btnPick: "#btnPickCommAvatar",
            btnClear: "#btnClearCommAvatar",
            initialText: "G",
            onChange: function (url) { chosenCommAvatar = url; },
        });

        // Edit-guild picker (in the guild info tab)
        var editPicker = setup({
            preview: "#commEditAvatarPreview",
            searchBox: "#commEditAvatarSearch",
            query: "#commEditAvatarQuery",
            results: "#commEditAvatarResults",
            btnPick: "#btnPickEditCommAvatar",
            btnClear: "#btnClearEditCommAvatar",
            initialText: "G",
            onChange: function (url) { _editCommAvatar = url; },
        });

        // Expose so openCommunityMenu can seed the edit preview.
        State._editPicker = editPicker;
    }

    var chosenCommAvatar = null;
    var chosenCommColor = COMM_COLORS[0];
    function thrVisValue(scope) {
        var a = document.querySelector(scope + ' .thr-vis-opt.active');
        return a ? (a.getAttribute('data-vis') === '1') : true;
    }
    function wireVisToggle(scope) {
        var w = document.querySelector(scope + ' .thr-vis-toggle');
        if (!w) return;
        w.addEventListener('click', function (e) {
            var o = e.target.closest('.thr-vis-opt');
            if (!o) return;
            w.querySelectorAll('.thr-vis-opt').forEach(function (b) { b.classList.remove('active'); });
            o.classList.add('active');
        });
    }
    function seedVisToggle(scope, isPublic) {
        var w = document.querySelector(scope + ' .thr-vis-toggle');
        if (!w) return;
        w.querySelectorAll('.thr-vis-opt').forEach(function (b) {
            b.classList.toggle('active', b.getAttribute('data-vis') === (isPublic ? '1' : '0'));
        });
    }


    function wireNewCommunityModal() {
        var swatches = $("#commColors");
        swatches.innerHTML = COMM_COLORS.map(function (c, i) {
            return '<span class="thr-swatch' + (i === 0 ? " chosen" : "") + '" data-color="' + c + '" style="background:' + c + '"></span>';
        }).join("");
        swatches.addEventListener("click", function (e) {
            var sw = e.target.closest(".thr-swatch");
            if (!sw) return;
            chosenCommColor = sw.getAttribute("data-color");
            $$(".thr-swatch", swatches).forEach(function (s) { s.classList.remove("chosen"); });
            sw.classList.add("chosen");
        });
        $("#btnCreateComm").addEventListener("click", function () { seedVisToggle("#modalNewCommunity", true); openModal("modalNewCommunity"); });
        $("#btnCreateCommSubmit").addEventListener("click", function () {
            var name = $("#commNameInput").value.trim();
            if (!name) { toast("Give the guild a name", "error"); return; }
            api("/threads/api/communities", {
                json: {
                    name: name,
                    genre: $("#commGenreInput").value.trim(),
                    description: $("#commDescInput").value.trim(),
                    icon_color: chosenCommColor,
                    icon_url: chosenCommAvatar,
                    is_public: thrVisValue("#modalNewCommunity"),
                },
            }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                closeModal("modalNewCommunity");
                $("#commNameInput").value = "";
                $("#commGenreInput").value = "";
                $("#commDescInput").value = "";
                var c = res.community;
                State.discoverMode = false;
                State.activeCommunity = c;
                State.myCommunityRole = "owner";
                renderRail();
                renderChannelPanel();
                if (c.channels && c.channels.length) openChannel(c.channels[0]);
                toast("Guild created!");
            });
        });
    }

    function wireCommunities() {
        wireNewCommunityModal();
        wireVisToggle("#modalCommunity");
        wirePollModal();
        wirePartyModal();

        $("#btnSubmitReport").addEventListener("click", submitReport);

        // rail
        $("#commRailList").addEventListener("click", function (e) {
            var item = e.target.closest(".thr-rail-item");
            if (!item) return;
            var cid = parseInt(item.getAttribute("data-comm"), 10);
            var c = null;
            State.communities.forEach(function (x) { if (x.id === cid) c = x; });
            if (!c) return;
            State.discoverMode = false;
            State.activeCommunity = c;
            State.myCommunityRole = c.role || "member";
            renderRail();
            renderChannelPanel();
            if (State.active && State.active.type === "channel") {
                var ok = false;
                (c.channels || []).forEach(function (ch) { if (ch.id === State.active.id) ok = true; });
                if (!ok) {
                    State.active = null;
                    $("#convView").classList.add("hidden");
                    $("#emptyState").classList.remove("hidden");
                    if (c.channels && c.channels.length) openChannel(c.channels[0]);
                }
            } else if (c.channels && c.channels.length) {
                openChannel(c.channels[0]);
            }
        });

        // channel list
        $("#channelList").addEventListener("click", function (e) {
            if (e.target.closest("#btnAddChannel")) {
                openModal("modalNewChannel");
                return;
            }
            var item = e.target.closest(".thr-channel");
            if (!item) return;
            var chid = parseInt(item.getAttribute("data-ch"), 10);
            var ch = null;
            (State.activeCommunity.channels || []).forEach(function (x) { if (x.id === chid) ch = x; });
            if (ch) openChannel(ch);
        });

        // party rows in the channel panel
        $("#partyList").addEventListener("click", function (e) {
            var row = e.target.closest(".thr-party-row");
            if (!row) return;
            var chid = parseInt(row.getAttribute("data-ch"), 10);
            var ch = null;
            (State.activeCommunity.channels || []).forEach(function (x) { if (x.id === chid) ch = x; });
            if (ch) openChannel(ch);
        });

        // discover
        $("#btnDiscover").addEventListener("click", showDiscover);
        $("#discoverList").addEventListener("click", function (e) {
            var join = e.target.closest("[data-join]");
            if (!join) return;
            var cid = parseInt(join.getAttribute("data-join"), 10);
            api("/threads/api/communities/" + cid + "/join", { json: {} }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                var c = res.community;
                State.discoverMode = false;
                State.activeCommunity = c;
                State.myCommunityRole = "member";
                loadDiscover($("#discoverSearch").value);
                renderRail();
                renderChannelPanel();
                if (c.channels && c.channels.length) openChannel(c.channels[0]);
                toast("Joined " + c.name);
            });
        });

        // community menu header + tabs + actions
        $("#btnCommMenuHead").addEventListener("click", function () { openCommunityMenu("info"); });
        $$(".thr-comm-tab").forEach(function (t) {
            t.addEventListener("click", function () {
                $$(".thr-comm-tab").forEach(function (x) { x.classList.remove("active"); });
                t.classList.add("active");
                showCommTab(t.getAttribute("data-ctab"));
            });
        });
        $("#btnSaveCommunity").addEventListener("click", saveCommunityEdit);
        $("#btnLeaveCommunity").addEventListener("click", leaveCommunityAction);
        $("#btnMuteCommunity").addEventListener("click", toggleCommunityMute);
        var _delCommBtn = document.getElementById("btnDeleteCommunity");
        if (_delCommBtn) _delCommBtn.addEventListener("click", deleteCommunityAction);

        // community modal: member actions
        $("#commMemberList").addEventListener("click", function (e) {
            var row = e.target.closest(".thr-member-row");
            var b = e.target.closest("[data-role],[data-kick],[data-mute],[data-ban],[data-block],[data-unban]");
            if (!b && row) {
                var uName = row.querySelector(".thr-member-name");
                if (uName && uName.getAttribute("data-uid")) {
                    openUserProfile(parseInt(uName.getAttribute("data-uid"), 10));
                }
                return;
            }
            if (!b) return;
            var uid = parseInt(b.getAttribute("data-uid") || b.getAttribute("data-kick") ||
                b.getAttribute("data-mute") || b.getAttribute("data-ban") || b.getAttribute("data-block") ||
                b.getAttribute("data-unban"), 10);
            var base = "/threads/api/communities/" + State.activeCommunity.id + "/members/" + uid;
            if (b.hasAttribute("data-role")) {
                api(base + "/role", { json: { role: b.getAttribute("data-role") } }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    toast("Role updated");
                    openCommunityMenu("members");
                });
            } else if (b.hasAttribute("data-kick")) {
                if (!window.confirm("Kick this member?")) return;
                api(base + "/kick", { json: {} }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    toast("Member kicked");
                    openCommunityMenu("members");
                });
            } else if (b.hasAttribute("data-mute")) {
                var muted = b.getAttribute("data-muted") === "1";
                api(base + "/mute", { json: { muted: !muted } }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    toast(muted ? "Unmuted" : "Muted — they can't post");
                    openCommunityMenu("members");
                });
            } else if (b.hasAttribute("data-ban")) {
                if (!window.confirm("Ban this member from the community?")) return;
                api(base + "/ban", { json: {} }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    toast("Member banned");
                    openCommunityMenu("members");
                });
            } else if (b.hasAttribute("data-block")) {
                api("/threads/api/users/block", { json: { user_id: uid } }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    toast("User blocked — their messages are hidden");
                });
            }
        });

        $("#commBannedList").addEventListener("click", function (e) {
            var b = e.target.closest("[data-unban]");
            if (!b) return;
            var uid = parseInt(b.getAttribute("data-unban"), 10);
            api("/threads/api/communities/" + State.activeCommunity.id + "/members/" + uid + "/unban", { json: {} }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                toast("Unbanned");
                openCommunityMenu("members");
            });
        });

        $("#commReports").addEventListener("click", function (e) {
            var b = e.target.closest("[data-resolve-report]");
            if (!b) return;
            var rid = parseInt(b.getAttribute("data-resolve-report"), 10);
            api("/threads/api/reports/" + rid + "/resolve", { json: {} }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                toast("Report dismissed");
                openCommunityMenu("reports");
            });
        });

        // guild invite link: load it whenever the modal opens, copy on click
        function copyTextToClipboard(text) {
            // Reliable everywhere: build a detached textarea, select, execCommand.
            var ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.left = "-9999px";
            ta.style.top = "0";
            ta.setAttribute("readonly", "");
            document.body.appendChild(ta);
            ta.select();
            ta.setSelectionRange(0, text.length);
            var ok = false;
            try {
                ok = document.execCommand("copy");
            } catch (e) { ok = false; }
            document.body.removeChild(ta);
            return ok;
        }
        $("#btnCopyInvite").addEventListener("click", function () {
            var input = $("#commInviteLink");
            if (!input || !input.value) {
                loadInviteLink();
                toast("Generating invite link…", "info");
                return;
            }
            var val = input.value;
            // Try modern async first, fall back to the sync textarea copy.
            var copied = false;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(val).then(function () {
                    toast("Invite link copied!");
                }).catch(function () {
                    toast(copyTextToClipboard(val) ? "Invite link copied!" : "Press Ctrl+C to copy", copyTextToClipboard(val) ? undefined : "info");
                });
            } else {
                toast(copyTextToClipboard(val) ? "Invite link copied!" : "Press Ctrl+C to copy");
            }
        });
        $("#commInviteLink").addEventListener("click", function () {
            if (this.value) window.open(this.value, "_blank");
        });
        $("#commInviteFriends").addEventListener("click", function (e) {
            var row = e.target.closest("[data-send-invite]");
            if (!row) return;
            sendInviteToFriend(parseInt(row.getAttribute("data-send-invite"), 10));
        });

        // party strip actions
        $("#partyStrip").addEventListener("click", function (e) {
            var b = e.target.closest("[data-join-party],[data-rsvp-party],[data-cancel-party]");
            if (!b) return;
            var pid = parseInt(b.getAttribute("data-join-party") || b.getAttribute("data-rsvp-party") || b.getAttribute("data-cancel-party"), 10);
            if (b.hasAttribute("data-join-party")) {
                var p = null;
                State.parties.forEach(function (x) { if (x.id === pid) p = x; });
                if (p && p.anime_id) window.open("/anime/" + p.anime_id, "_blank");
                else toast("Party is live — enjoy the chat!");
                return;
            }
            if (b.hasAttribute("data-cancel-party")) {
                if (!window.confirm("Cancel this watch party?")) return;
                api("/threads/api/parties/" + pid, { method: "DELETE" }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    toast("Party cancelled");
                    loadChannelParties();
                    refreshCommunities();
                });
                return;
            }
            var isGoing = false;
            State.parties.forEach(function (x) { if (x.id === pid) isGoing = x.is_rsvped; });
            if (isGoing) {
                api("/threads/api/parties/" + pid + "/rsvp", { method: "DELETE" }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    State.parties = res.parties || [];
                    renderPartyStrip();
                    refreshCommunities();
                });
            } else {
                api("/threads/api/parties/" + pid + "/rsvp", { json: {} }).then(function (res) {
                    if (!res.success) { handleApiError(res); return; }
                    State.parties = res.parties || [];
                    renderPartyStrip();
                    toast("You're going! \uD83C\uDF7F");
                    refreshCommunities();
                });
            }
        });

        // poll vote (delegated from inline poll cards)
        $("#msgList").addEventListener("click", function (e) {
            var opt = e.target.closest(".thr-poll-opt");
            if (!opt) return;
            var pid = parseInt(opt.getAttribute("data-poll"), 10);
            var oid = parseInt(opt.getAttribute("data-opt"), 10);
            api("/threads/api/polls/" + pid + "/vote", { json: { option_id: oid } }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                State.polls = res.polls || [];
                renderMessages(false);
            });
        });

        // channel chat-head buttons
        $("#btnParty").addEventListener("click", function () {
            if (!isChannelOpen()) return;
            $("#partyTitleInput").value = "";
            $("#partyWhen").value = "";
            partyPick = null;
            $("#partyAnimePick").classList.add("hidden");
            $("#partyAnimePick").innerHTML = "";
            openModal("modalParty");
        });
        $("#btnNewPoll").addEventListener("click", function () {
            if (!isChannelOpen()) return;
            $("#pollQuestion").value = "";
            renderPollModalOptions();
            openModal("modalPoll");
        });

        // new channel modal
        $("#btnCreateChannel").addEventListener("click", function () {
            var name = $("#channelNameInput").value.trim();
            if (!name) { toast("Channel needs a name", "error"); return; }
            api("/threads/api/communities/" + State.activeCommunity.id + "/channels", {
                json: { name: name, topic: $("#channelTopicInput").value.trim() },
            }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                closeModal("modalNewChannel");
                $("#channelNameInput").value = "";
                $("#channelTopicInput").value = "";
                var ch = res.channel;
                (State.activeCommunity.channels || []).push(ch);
                renderChannelList();
                openChannel(ch);
                refreshCommunities();
                toast("Channel created");
            });
        });
    }

    function openChannelFromNotification(chid) {
        if (State.activeTab !== "communities") setTab("communities");
        function tryOpen() {
            var found = false;
            State.communities.forEach(function (c) {
                (c.channels || []).forEach(function (ch) {
                    if (ch.id === chid) {
                        found = true;
                        State.discoverMode = false;
                        State.activeCommunity = c;
                        State.myCommunityRole = c.role || "member";
                        renderRail();
                        renderChannelPanel();
                        openChannel(ch);
                    }
                });
            });
            return found;
        }
        if (!tryOpen()) {
            refreshCommunities(function () { tryOpen(); });
        }
    }
        // ---- Pins modal ----
    function wirePinsModal() {
        var list = $("#pinsList");
        function render() {
            api("/threads/api/messages?ctx=" + State.active.type + ":" + State.active.id + "&limit=1").then(function (res) {
                if (!res.success) return;
                var pins = res.pins || [];
                list.innerHTML = pins.length ? pins.map(function (p) {
                    var txt = p.content || (p.kind === "gif" ? "GIF" : p.kind === "image" ? "Image" : p.kind === "video" ? "Video" : p.kind === "anime" ? (function(){try{var d=JSON.parse(p.content);return "\uD83D\uDCFA "+d.title}catch(e){return "Anime"}})() : "");
                    return '<div class="thr-pin-row">' +
                        '<i class="fas fa-thumbtack"></i>' +
                        '<div><div class="thr-pin-user">@' + escapeHtml(p.sender ? p.sender.username : "") + "</div>" +
                        '<div class="thr-pin-content">' + escapeHtml(txt) + "</div></div></div>";
                }).join("") : '<div class="thr-dropdown-empty">Nothing pinned yet</div>';
            });
        }
        $("#btnPins").addEventListener("click", function () {
            openModal("modalPins");
            render();
        });
        $("#btnPinnedStripOpen").addEventListener("click", function () {
            openModal("modalPins");
            render();
        });
    }

    // ---- Settings modal ----
    function syncSettingsUI() {
        $("#setReadReceipts").checked = !!State.settings.read_receipts;
        $("#setTyping").checked = !!State.settings.typing_indicators;
    }
    function wireSettingsModal() {
        function save() {
            api("/threads/api/settings", {
                json: {
                    read_receipts: $("#setReadReceipts").checked ? 1 : 0,
                    typing_indicators: $("#setTyping").checked ? 1 : 0,
                },
            }).then(function (res) {
                if (res.success) {
                    State.settings = res.settings;
                    toast("Settings saved");
                }
            });
        }
        $("#setReadReceipts").addEventListener("change", save);
        $("#setTyping").addEventListener("change", save);
        $("#btnSettings").addEventListener("click", function () {
            syncSettingsUI();
            openModal("modalSettings");
        });
    }

    // ---- Notifications bell ----
    function wireBell() {
        var dd = $("#bellDropdown");
        $("#btnBell").addEventListener("click", function (e) {
            e.stopPropagation();
            var opening = dd.classList.contains("hidden");
            dd.classList.toggle("hidden", !opening);
            if (opening) {
                api("/threads/api/notifications").then(function (res) {
                    if (res.success) renderNotifications(res.notifications || []);
                });
            }
        });
        document.addEventListener("click", function (e) {
            if (!dd.classList.contains("hidden") && !e.target.closest(".thr-bell-wrap")) {
                dd.classList.add("hidden");
            }
        });
        dd.addEventListener("click", function (e) {
            var item = e.target.closest(".thr-notif");
            if (!item) return;
            var ctx = item.getAttribute("data-nctx");
            if (ctx) {
                var parts = ctx.split(":");
                if (parts.length === 2) {
                    var type = parts[0], id = parseInt(parts[1], 10);
                    if (type === "dm" || type === "group") {
                        // ensure the conversation is in our list
                        var found = State.conversations.some(function (c) { return c.id === id && c.type === type; });
                        if (!found) refreshConversations();
                        openConversation(type, id);
                        dd.classList.add("hidden");
                    } else if (type === "channel") {
                        openChannelFromNotification(id);
                        dd.classList.add("hidden");
                    }
                }
            }
            if (item.getAttribute('data-ntype') === 'friend_request') {
                dd.classList.add('hidden');
                openRequestsModal();
                return;
            }
            if (e.target.closest("#btnNotifRead")) return;
        });
        $("#btnNotifRead").addEventListener("click", function () {
            api("/threads/api/notifications/read", { json: {} }).then(function (res) {
                if (res.success) {
                    State.notifUnread = 0;
                    var badge = $("#bellBadge");
                    badge.textContent = "";
                    badge.classList.add("hidden");
                    refreshNotifications();
                }
            });
        });
    }

    // ----------------------------------------------------------
    // Events
    // ----------------------------------------------------------
    function wireEvents() {
        // conversation list clicks
        $("#convList").addEventListener("click", function (e) {
            var item = e.target.closest(".thr-conv");
            if (!item) return;
            var id = parseInt(item.getAttribute("data-id"), 10);
            var type = item.getAttribute("data-type");
            if (State.active && State.active.id === id && State.active.type === type) return;
            openConversation(type, id);
        });

        // search filter
        $("#convSearch").addEventListener("input", function () {
            State.convFilter = this.value;
            renderConversations();
        });

        // composer
        var input = $("#msgInput");
        input.addEventListener("input", onInputTyping);
        input.addEventListener("keydown", function (e) {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
        $("#btnSend").addEventListener("click", sendMessage);
        $("#btnCancelReply").addEventListener("click", function () {
            State.replyTo = null;
            $("#replyBar").classList.add("hidden");
        });
        $("#btnClearAttach").addEventListener("click", clearAttach);

        // attach
        // Attach handled by + menu above
        $("#fileInput").addEventListener("change", function () {
            var file = this.files && this.files[0];
            if (!file) return;
            var ext = (file.name.split(".").pop() || "").toLowerCase();
            if (["png", "jpg", "jpeg", "gif", "webp"].indexOf(ext) === -1 &&
                ["mp4", "webm", "mov"].indexOf(ext) === -1) {
                toast("That file type isn't supported", "error");
                this.value = "";
                return;
            }
            if (file.size > 25 * 1024 * 1024) {
                toast("File must be under 25 MB", "error");
                this.value = "";
                return;
            }
            var fd = new FormData();
            fd.append("file", file);
            fetch("/threads/api/upload", { method: "POST", body: fd }).then(function (r) { return r.json(); }).then(function (res) {
                if (!res.success) { handleApiError(res); toast(res.error || "Upload failed", "error"); return; }
                State.attach = { kind: res.kind, url: res.url, preview: res.url, name: res.name };
                showAttachPreview();
            });
        });

        // message actions (delegated)
        $("#msgList").addEventListener("click", function (e) {
            var act = e.target.closest("[data-act]");
            if (!act) return;
            var id = parseInt(act.getAttribute("data-id"), 10);
            var kind = act.getAttribute("data-act");
            if (kind === "reply") {
                var msg = State.messages.filter(function (m) { return m.id === id; })[0];
                if (!msg) return;
                State.replyTo = msg;
                var sender = msg.sender ? msg.sender.username : "someone";
                var snippet = msg.content || (msg.kind === "gif" ? "GIF" : msg.kind === "image" ? "Image" : msg.kind === "video" ? "Video" : "");
                $("#replyName").textContent = "@" + sender;
                $("#replySnippet").textContent = snippet.length > 60 ? snippet.slice(0, 60) + "…" : snippet;
                $("#replyBar").classList.remove("hidden");
                $("#msgInput").focus();
            } else if (kind === "pin") {
                pinMessage(id);
            } else if (kind === "edit") {
                State.editingId = id;
                renderMessages(false);
                var ta = document.querySelector('.thr-edit-input[data-edit-id="' + id + '"]');
                if (ta) { ta.focus(); ta.select(); }
            } else if (kind === "delete") {
                deleteMessage(id);
            } else if (kind === "save-edit") {
                var ta = document.querySelector('.thr-edit-input[data-edit-id="' + id + '"]');
                var val = ta ? ta.value.trim() : "";
                if (!val) { toast("Message can't be empty", "error"); return; }
                editMessage(id, val);
            } else if (kind === "cancel-edit") {
                State.editingId = null;
                renderMessages(false);
            }
        });

        // mention box
        $("#mentionBox").addEventListener("click", function (e) {
            var opt = e.target.closest(".thr-mention-opt");
            if (opt) insertMention(opt.getAttribute("data-user"));
        });

      // mute toggle
        $("#btnMute").addEventListener("click", function () {
            var conv = State.active.conv;
            var next = !conv.muted;
            api("/threads/api/conversations/" + conv.id + "/mute", { json: { muted: next } }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                conv.muted = next;
                renderChatHead();
                toast(next ? "Muted — no unread badges for this chat" : "Unmuted");
                renderConversations();
            });
        });

        // new DM + friend-requests buttons (header + empty state)
        $("#btnNewDm").addEventListener("click", function () { openModal("modalNewDm"); });
        $("#btnEmptyDm").addEventListener("click", function () { openModal("modalNewDm"); });

        // tabs
        $$(".thr-tab").forEach(function (t) {
            t.addEventListener("click", function () {
                setTab(t.getAttribute("data-tab"));
            });
        });

        // message actions — report / moderator delete
        $("#msgList").addEventListener("click", function (e) {
            var act = e.target.closest("[data-act]");
            if (!act) return;
            var kind = act.getAttribute("data-act");
            if (kind === "report") {
                State.reportMessageId = parseInt(act.getAttribute("data-id"), 10);
                $("#reportReason").value = "";
                openModal("modalReport");
            } else if (kind === "mod-delete") {
                modDeleteMessage(parseInt(act.getAttribute("data-id"), 10));
            }
        });

        // open mini-profile when clicking a message author / avatar
        $("#msgList").addEventListener("click", function (e) {
            var who = e.target.closest(".thr-profile-open");
            if (!who) return;
            var uid = parseInt(who.getAttribute("data-uid"), 10);
            if (uid && uid !== State.me.id) openUserProfile(uid);
        });

        // open mini-profile when clicking a member in the DM members modal
        $("#memberList").addEventListener("click", function (e) {
            var row = e.target.closest(".thr-member-row");
            if (!row || e.target.closest("button,[data-kick],[data-leave]")) return;
            var name = row.querySelector(".thr-member-name");
            var uid = name ? parseInt(name.getAttribute("data-uid"), 10) : null;
            if (uid && uid !== State.me.id) openUserProfile(uid);
        });

        // channel search filter
        $("#channelSearch").addEventListener("input", function () {
            State.commFilter = this.value;
            renderChannelList();
        });

        // discover search
        $("#discoverSearch").addEventListener("input", function () {
            clearTimeout(this._t);
            var input = this;
            this._t = setTimeout(function () { loadDiscover(input.value); }, 300);
        });
    }

    // ----------------------------------------------------------
    // Boot
    // ----------------------------------------------------------
    function parseUser() {
        try {
            State.me = JSON.parse(document.body.getAttribute("data-user"));
        } catch (e) { State.me = null; }
        if (!State.me) {
            window.location.href = "/login?next=" + encodeURIComponent("/threads");
            return;
        }
    }

    function boot() {
        parseUser();
        wireModalClose();
        wireDmModal();
        wireRequestsModal();
        refreshRequestBadge();
        wireGifModal();
        wireEmojiModal();
        wireMembersModal();
        wireAnimeModal();
        wirePinsModal();
        wireSettingsModal();
        wireCommunities();
        wireGuildAvatarPickers();
        wireBell();
        wireEvents();

        try {
            State.conversations = JSON.parse(document.body.getAttribute("data-conversations")) || [];
        } catch (e) { State.conversations = []; }
        State.notifUnread = parseInt(document.body.getAttribute("data-notifications") || "0", 10) || 0;
        renderConversations();
        refreshNotifications();

        // Pre-loaded messages: if the server embedded the first conversation's
        // messages, render them instantly without an API call (like community chat).
        try {
            var preloaded = JSON.parse(document.body.getAttribute("data-preloaded") || "{}");
            if (preloaded.ctx && preloaded.messages && preloaded.messages.length) {
                var parts = preloaded.ctx.split(":");
                var pType = parts[0], pId = parseInt(parts[1], 10);
                var conv = null;
                State.conversations.forEach(function (c) {
                    if (c.type === pType && c.id === pId) conv = c;
                });
                if (conv) {
                    State.active = { type: pType, id: pId, conv: conv };
                    State.reqSeq++;
                    State.newSinceId = conv.last_read_message_id || 0;
                    State.messages = preloaded.messages;
                    State.messages.forEach(function (m) { State.seenIds[m.id] = true; });
                    State.afterId = preloaded.afterId;
                    State.firstId = preloaded.firstId;
                    State.hasMore = preloaded.hasMore;
                    State.members = preloaded.members || [];
                    State.memberMap = {};
                    State.members.forEach(function (m) { State.memberMap[m.id] = m; });
                    State.pins = preloaded.pins || [];
                    State.polls = preloaded.polls || [];
                    State.parties = preloaded.parties || [];
                    State.settings = preloaded.settings || State.settings;
                    msgCache[pType + ":" + pId] = {
                        messages: State.messages,
                        afterId: State.afterId,
                        firstId: State.firstId,
                        hasMore: State.hasMore,
                        members: State.members,
                        pins: State.pins,
                        polls: State.polls,
                        parties: State.parties,
                        html: "",
                        at: Date.now(),
                    };
                    $("#emptyState").classList.add("hidden");
                    $("#convView").classList.remove("hidden");
                    renderChatHead();
                    renderMessages(true);
                    renderPins(State.pins);
                    if (pType === "channel") renderPartyStrip();
                    markActiveRead();
                    msgCache[pType + ":" + pId].html = $("#msgList").innerHTML;
                    // Fetch rank badges in the background and re-render so they
                    // show on the first conversation (and stay in the cache).
                    fetchThrRanks(State.messages).then(function () {
                        if (State.active && State.active.type === pType && State.active.id === pId &&
                            $("#msgList") && $("#msgList").innerHTML) {
                            renderMessages(false);
                        }
                    });
                }
            }
        } catch (e) { /* preloaded parse fail — no big deal */ }

        // Pre-loaded guild channel: seed the message cache so opening that
        // guild's default channel is instant (no blank/spinner).
        try {
            var gpre = JSON.parse(document.body.getAttribute("data-preloaded-guild") || "{}");
            if (gpre.ctx && gpre.messages && gpre.messages.length && State.activeTab !== "communities") {
                var gkey = gpre.ctx;
                if (!msgCache[gkey]) {
                    msgCache[gkey] = {
                        messages: gpre.messages,
                        afterId: gpre.afterId,
                        firstId: gpre.firstId,
                        hasMore: gpre.hasMore,
                        members: gpre.members || [],
                        pins: gpre.pins || [],
                        polls: gpre.polls || [],
                        parties: gpre.parties || [],
                        html: "",
                        at: Date.now(),
                    };
                }
            }
        } catch (e2) { /* ignore */ }

        // heartbeat + polling (paused when the tab is hidden to save CPU/network)
        refreshPresence();
        refreshCommunities();
        var _pollTimer = setInterval(function () {
            if (!document.hidden) pollMessages();
        }, 1500);
        var _convTimer = setInterval(function () {
            if (!document.hidden) refreshConversations();
        }, 5000);
        var _commTimer = setInterval(function () {
            if (!document.hidden) refreshCommunities();
        }, 10000);
        var _presTimer = setInterval(function () {
            if (!document.hidden) refreshPresence();
        }, 10000);
        var _notifTimer = setInterval(function () {
            if (!document.hidden) refreshNotifications();
        }, 15000);
        var _reqTimer = setInterval(function () {
            if (!document.hidden) refreshRequestBadge();
        }, 15000);

        // presence away/back
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) {
                api("/threads/api/presence?away=1");
            } else {
                refreshPresence();
                pollMessages();
                refreshConversations();
                refreshCommunities();
            }
        });
        // leave a heart-beat while the tab is open
        setInterval(function () {
            if (!document.hidden) api("/threads/api/presence");
        }, 30000);

        // parse URL params once for invite, open, etc.
        var params = new URLSearchParams(window.location.search);

        // join a guild via ?invite=CODE, then open it
        var inviteCode = params.get("invite");
        if (inviteCode) {
            api("/threads/api/communities/join-invite", { json: { code: inviteCode } }).then(function (res) {
                if (!res.success) {
                    toast(res.error === "invalid_invite" ? "That invite link is invalid or expired." : (res.error === "banned" ? "You can't join that guild." : "Could not join the guild."), "error");
                    return;
                }
                var joined = res.community;
                toast("Joined " + (joined ? joined.name : "the guild") + "!");
                refreshCommunities();
                if (joined) {
                    State.discoverMode = false;
                    State.activeCommunity = joined;
                    State.myCommunityRole = joined.role || "member";
                    renderRail();
                    renderChannelPanel();
                    if (joined.channels && joined.channels.length) openChannel(joined.channels[0]);
                }
            });
        }

        // open ?with=dm:3 from a notification click elsewhere
        var open = params.get("open");
        if (open && open.indexOf(":") !== -1) {
            var parts = open.split(":");
            if ((parts[0] === "dm" || parts[0] === "group") && parts[1]) {
                setTimeout(function () { openConversation(parts[0], parseInt(parts[1], 10)); }, 100);
            } else if (parts[0] === "channel" && parts[1]) {
                setTimeout(function () { openChannelFromNotification(parseInt(parts[1], 10)); }, 100);
            }
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();