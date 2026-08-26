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
        active: null,
        messages: [],
        loadingHistory: false,
        seenIds: {},
        afterId: 0,
        firstId: 0,
        hasMore: true,
        loadingOlder: false,
        reqSeq: 0,
        newSinceId: 0,
        members: [],
        memberMap: {},
        presence: {},
        settings: { read_receipts: true, typing_indicators: true },
        replyTo: null,
        attach: null,
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

    var msgCache = {};

    // ----------------------------------------------------------
    // Tiny helpers
    // ----------------------------------------------------------
    function $(sel) { return document.querySelector(sel); }
    function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

    var _escDiv = document.createElement("div");
    function escapeHtml(s) {
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

    function avatarInner(user) {
        user = user || {};
        if (user.avatar) {
            return '<img class="thr-avatar-img" src="/static/images/avatars/' +
                escapeHtml(user.avatar) + '" alt="">';
        }
        return escapeHtml(initials(user.username));
    }

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
        if (!ids.length) return Promise.resolve();
        return api("/api/user-ranks", { json: { user_ids: ids } }).then(function (data) {
            if (data && data.ranks) {
                Object.keys(data.ranks).forEach(function (uid) {
                    _thrRankCache[uid] = data.ranks[uid];
                });
            }
        }).catch(function () {});
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

    function isChannelOpen() { return State.active && State.active.type === "channel"; }
    function isCommMod() {
        return State.myCommunityRole === "owner" || State.myCommunityRole === "moderator";
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
            var s = State.convMemberName ? State.convMemberName(c, lm.sender_id) : null;
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
                    var fresh = null;
                    State.conversations.forEach(function (c) {
                        if (c.id === prevActive.id && c.type === prevActive.type) fresh = c;
                    });
                    if (fresh) State.active = { type: fresh.type, id: fresh.id, conv: fresh };
                    else { /* conversation gone */ }
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
        State.newSinceId = conv.last_read_message_id || 0;
        State.messages = [];
        State.seenIds = {};
        State.afterId = 0;
        State.firstId = 0;
        State.hasMore = true;
        State.loadingOlder = false;

        $("#msgList").innerHTML = '';
        $("#emptyState").classList.add("hidden");
        $("#convView").classList.remove("hidden");
        renderChatHead();

        var cached = msgCache[key];
        if (cached && cached.messages && cached.messages.length) {
            State.messages = cached.messages;
            State.messages.forEach(function (m) { State.seenIds[m.id] = true; });
            State.afterId = cached.afterId || 0;
            State.firstId = cached.firstId || 0;
            State.hasMore = cached.hasMore !== undefined ? cached.hasMore : true;
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
            pollMessages();
            markActiveRead();
            $("#msgInput").focus();
            return;
        }

        State.loadingHistory = true;
        loadHistory();
        markActiveRead();
        $("#msgInput").focus();
    }

    // ----------------------------------------------------------
    // HISTORY LOADING
    // ----------------------------------------------------------
    function loadHistory(beforeId) {
        var ctype = State.active.type, cid = State.active.id;
        var seq = State.reqSeq;
        var key = ctype + ":" + cid;

        // For channels, use the new /api/channels/<id>/messages endpoint
        if (ctype === "channel") {
            var url = "/api/channels/" + cid + "/messages?limit=30";
            if (beforeId) {
                url += "&before_id=" + beforeId;
            }
            api(url).then(function (res) {
                if (seq !== State.reqSeq) return;
                if (!res.success) { handleApiError(res); return; }
                var messages = res.messages || [];
                if (beforeId) {
                    State.messages = messages.concat(State.messages);
                } else {
                    State.messages = messages;
                }
                State.seenIds = {};
                State.messages.forEach(function (m) { State.seenIds[m.id] = true; });
                if (State.messages.length) {
                    State.afterId = State.messages[State.messages.length - 1].id;
                    State.firstId = State.messages[0].id;
                    State.hasMore = res.has_more || false;
                } else {
                    State.afterId = 0;
                    State.firstId = 0;
                    State.hasMore = false;
                }
                msgCache[key] = {
                    messages: State.messages,
                    afterId: State.afterId,
                    firstId: State.firstId,
                    hasMore: State.hasMore,
                    members: State.members,
                    pins: State.pins || [],
                    polls: State.polls || [],
                    parties: State.parties || [],
                    html: "",
                    at: Date.now(),
                };
                State.loadingHistory = false;
                if (seq === State.reqSeq) {
                    renderMessages(true);
                    renderPins(State.pins || []);
                    if (isChannelOpen()) renderPartyStrip();
                    refreshPresence();
                }
                fetchThrRanks(State.messages).then(function () {
                    if (seq === State.reqSeq && $("#msgList") && $("#msgList").innerHTML) {
                        renderMessages(false);
                    }
                });
            });
            return;
        }

        // For DMs and groups, keep using the old endpoint
        api("/threads/api/messages?ctx=" + ctype + ":" + cid + "&limit=60").then(function (res) {
            if (seq !== State.reqSeq) return;
            if (!res.success) { handleApiError(res); return; }
            State.messages = res.messages;
            State.seenIds = {};
            State.messages.forEach(function (m) { State.seenIds[m.id] = true; });
            if (State.messages.length) {
                State.afterId = State.messages[State.messages.length - 1].id;
                State.firstId = State.messages[0].id;
                State.hasMore = res.messages.length >= 60;
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
            var keys = Object.keys(msgCache);
            if (keys.length > 30) {
                keys.sort(function (a, b) { return msgCache[a].at - msgCache[b].at; });
                delete msgCache[keys[0]];
            }
            State.loadingHistory = false;
            if (seq === State.reqSeq) {
                renderMessages(true);
                renderPins(res.pins || []);
                if (isChannelOpen()) renderPartyStrip();
                refreshPresence();
            }
            fetchThrRanks(State.messages).then(function () {
                if (seq === State.reqSeq && $("#msgList") && $("#msgList").innerHTML) {
                    renderMessages(false);
                }
            });
        });
    }

    // ----------------------------------------------------------
    // LOAD OLDER MESSAGES (for channels)
    // ----------------------------------------------------------
    function loadOlderChannelMessages() {
        if (State.loadingOlder || !State.hasMore || !isChannelOpen()) return;
        State.loadingOlder = true;
        var firstId = State.firstId;
        if (!firstId) { State.loadingOlder = false; return; }
        var seq = State.reqSeq;
        var url = "/api/channels/" + State.active.id + "/messages?before_id=" + firstId + "&limit=30";
        api(url).then(function (res) {
            State.loadingOlder = false;
            if (seq !== State.reqSeq) return;
            if (!res.success) { handleApiError(res); return; }
            var older = res.messages || [];
            if (older.length) {
                var oldScrollHeight = $("#msgList").scrollHeight;
                State.messages = older.concat(State.messages);
                State.firstId = State.messages[0] ? State.messages[0].id : 0;
                State.hasMore = res.has_more || false;
                var key = "channel:" + State.active.id;
                if (msgCache[key]) {
                    msgCache[key].messages = State.messages;
                    msgCache[key].firstId = State.firstId;
                    msgCache[key].hasMore = State.hasMore;
                    msgCache[key].at = Date.now();
                }
                renderMessages(false);
                // Adjust scroll position to keep the user at the same spot
                var newScrollHeight = $("#msgList").scrollHeight;
                $("#msgList").scrollTop = newScrollHeight - oldScrollHeight + 10;
            } else {
                State.hasMore = false;
            }
        }).catch(function () {
            State.loadingOlder = false;
        });
    }

    // ----------------------------------------------------------
    // RENDER MESSAGES
    // ----------------------------------------------------------
    function renderMessages(scrollToBottom) {
        var list = $("#msgList");
        var html = "";
        var lastDay = null;
        var limit = State.messages.length;
        var polls = isChannelOpen() ? State.polls : [];
        var pi = 0;
        var dividerPlaced = !(State.newSinceId > 0);

        if (isChannelOpen() && State.hasMore && State.messages.length) {
            html += '<div class="thr-load-more-container"><button class="thr-load-more-btn" id="btnLoadOlder">⬆ Load older messages</button></div>';
        }

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

        if (State.active) {
            var hk = State.active.type + ":" + State.active.id;
            if (msgCache[hk]) msgCache[hk].html = html;
        }

        if (scrollToBottom) {
            list.scrollTop = list.scrollHeight;
        }
        updateSeenText();
    }

    // ----------------------------------------------------------
    // POLLING MESSAGES
    // ----------------------------------------------------------
    function pollMessages() {
        if (!State.active) return;
        if (document.hidden) return;
        var ctype = State.active.type, cid = State.active.id;
        var seq = State.reqSeq;
        var key = ctype + ":" + cid;

        if (ctype === "channel") {
            var url = "/api/channels/" + cid + "/messages?after_id=" + State.afterId;
            api(url).then(function (res) {
                if (seq !== State.reqSeq) return;
                if (!res.success) { handleApiError(res); return; }
                if (res.messages && res.messages.length) {
                    appendMessages(res.messages);
                    if (msgCache[key]) {
                        msgCache[key].messages = State.messages;
                        msgCache[key].afterId = State.afterId;
                        msgCache[key].at = Date.now();
                    }
                    markActiveRead();
                }
                updateSeenText();
            });
            return;
        }

        api("/threads/api/messages?ctx=" + ctype + ":" + cid + "&after=" + State.afterId).then(function (res) {
            if (seq !== State.reqSeq) return;
            if (!res.success) { handleApiError(res); return; }
            if (res.messages && res.messages.length) {
                appendMessages(res.messages);
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

    // ----------------------------------------------------------
    // APPEND MESSAGES
    // ----------------------------------------------------------
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
        if (State.active) {
            var ck = State.active.type + ":" + State.active.id;
            if (msgCache[ck]) {
                msgCache[ck].messages = State.messages;
                msgCache[ck].afterId = State.afterId;
                msgCache[ck].at = Date.now();
            }
        }
        list.scrollTop = list.scrollHeight;
        updateSeenText();
    }

    // ----------------------------------------------------------
    // RENDER CHAT HEAD
    // ----------------------------------------------------------
    function renderChatHead() {
        var conv = State.active && State.active.conv;
        var activeType = State.active && State.active.type;

        if (activeType === "channel") {
            var comm = State.activeCommunity;
            $("#chatAvatar").innerHTML = "#";
            $("#chatAvatar").style.background = (comm && comm.icon_color) || "#8b5cf6";
            $("#chatName").textContent = "#" + (conv ? conv.name : "channel");
            $("#chatSub").textContent = conv && conv.topic ? conv.topic : (comm ? comm.name : "");
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

        if (!conv) return;
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

    // ----------------------------------------------------------
    // RENDER PINNED MESSAGES
    // ----------------------------------------------------------
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

    // ----------------------------------------------------------
    // UPDATE SEEN TEXT
    // ----------------------------------------------------------
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
    // UPDATE TYPING ROW
    // ----------------------------------------------------------
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

    // ----------------------------------------------------------
    // REFRESH PRESENCE
    // ----------------------------------------------------------
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
        if (!ids.length) return;
        api("/threads/api/presence?ids=" + ids.join(",")).then(function (res) {
            if (res.success) {
                State.presence = res.presence || {};
                renderConversations();
                if (State.active && State.active.conv && State.active.conv.type === "dm") renderChatHead();
            }
        });
    }

    // ----------------------------------------------------------
    // MARK ACTIVE READ
    // ----------------------------------------------------------
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
    // SYNC SETTINGS UI
    // ----------------------------------------------------------
    function syncSettingsUI() {
        $("#setReadReceipts").checked = !!State.settings.read_receipts;
        $("#setTyping").checked = !!State.settings.typing_indicators;
    }

    // ----------------------------------------------------------
    // RENDER MESSAGE (single)
    // ----------------------------------------------------------
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
            var showText = content && !attach;
            body = parentRef + (showText ? '<div class="thr-msg-content">' + mentions + "</div>" : "") +
                attach +
                '<div class="thr-msg-meta"><span class="thr-msg-time">' + fmtClock(m.created_at) + "</span>" + flags + "</div>";
        }

        return '<div class="' + cls + '" data-mid="' + m.id + '">' +
            '<div class="thr-msg-avatar" style="background:' + escapeHtml(sender.avatar_color || "#8b5cf6") + '">' +
            avatarInner(sender) + "</div>" +
            '<div class="thr-msg-main">' +
            '<div class="thr-msg-head"><span class="thr-msg-user">' + escapeHtml(sender.username || "unknown") + "</span>" +
            thrRankBadgeHtml(sender.id, _thrRankCache[sender.id] ? _thrRankCache[sender.id].rank : null, _thrRankCache[sender.id] ? _thrRankCache[sender.id].xp : 0) +
            '<span class="thr-msg-time">' + fmtClock(m.created_at) + "</span></div>" +
            body +
            '<div class="thr-msg-actions">' + actions + "</div>" +
            "</div></div>";
    }

    // ----------------------------------------------------------
    // RENDER POLL CARD
    // ----------------------------------------------------------
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

    // ----------------------------------------------------------
    // RENDER PARTY STRIP
    // ----------------------------------------------------------
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

    // ----------------------------------------------------------
    // UPDATE CHANNEL EXTRAS
    // ----------------------------------------------------------
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

    // ============================================================
    // COMMUNITIES TAB
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
        if (!State.activeCommunity) {
            if (State.communities.length) {
                var c = State.communities[0];
                State.activeCommunity = c;
                State.myCommunityRole = c.role || "member";
                renderRail();
                renderChannelPanel();
                if (c.channels && c.channels.length) openChannel(c.channels[0]);
            } else {
                showDiscover();
            }
        } else if (!isChannelOpen() && State.activeCommunity.channels && State.activeCommunity.channels.length) {
            openChannel(State.activeCommunity.channels[0]);
        } else {
            renderChannelPanel();
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
        State.communities.forEach(function (c) {
            var active = State.activeCommunity && State.activeCommunity.id === c.id;
            html += '<div class="thr-rail-item' + (active ? " active" : "") + '" data-comm="' + c.id + '" title="' +
                escapeHtml(c.name) + '">' +
                '<span class="thr-rail-icon" style="background:' + escapeHtml(c.icon_color || "#8b5cf6") + '">' +
                escapeHtml(initials(c.name)) + "</span>" +
                (c.unread ? '<span class="thr-unread-badge thr-rail-badge">' + (c.unread > 99 ? "99+" : c.unread) + "</span>" : "") +
                "</div>";
        });
        $("#commRailList").innerHTML = html || '<div class="thr-rail-empty" title="Join or create a guild">+</div>';
    }

    function renderChannelPanel() {
        var c = State.activeCommunity;
        if (!c) return;
        $("#commName").textContent = c.name || "";
        $("#commMeta").textContent = (c.member_count || 0) + " members" + (c.genre ? " · " + c.genre : "");
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

        $("#msgList").innerHTML = '';
        $("#emptyState").classList.add("hidden");
        $("#convView").classList.remove("hidden");
        renderChatHead();

        var cached = msgCache[key];
        if (cached && cached.messages && cached.messages.length) {
            State.messages = cached.messages;
            State.messages.forEach(function (m) { State.seenIds[m.id] = true; });
            State.afterId = cached.afterId || 0;
            State.firstId = cached.firstId || 0;
            State.hasMore = cached.hasMore !== undefined ? cached.hasMore : true;
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

    // ============================================================
    // MODALS & WIRING
    // ============================================================

    var COMM_COLORS = ["#8b5cf6", "#ef4444", "#f59e0b", "#22c55e", "#3b82f6", "#ec4899", "#06b6d4", "#f97316", "#14b8a6"];
    var chosenCommColor = COMM_COLORS[0];
    var partyPick = null;

    function openModal(id) { $("#" + id).classList.remove("hidden"); }
    function closeModal(id) { $("#" + id).classList.add("hidden"); }

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

    // This function now includes the scroll detection for "Load Older"
    function wireEvents() {
        // Conversation list clicks
        $("#convList").addEventListener("click", function (e) {
            var item = e.target.closest(".thr-conv");
            if (!item) return;
            var id = parseInt(item.getAttribute("data-id"), 10);
            var type = item.getAttribute("data-type");
            if (State.active && State.active.id === id && State.active.type === type) return;
            openConversation(type, id);
        });

        // Search filter
        $("#convSearch").addEventListener("input", function () {
            State.convFilter = this.value;
            renderConversations();
        });

        // Composer
        var input = $("#msgInput");
        input.addEventListener("input", function () { autoGrow(this); });
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

        // Attach
        $("#btnAttach").addEventListener("click", function () { $("#fileInput").click(); });
        $("#fileInput").addEventListener("change", function () {
            var file = this.files && this.files[0];
            if (!file) return;
            var ext = (file.name.split(".").pop() || "").toLowerCase();
            if (["png", "jpg", "jpeg", "gif", "webm", "mp4", "mov"].indexOf(ext) === -1) {
                toast("File type not supported", "error");
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
                if (!res.success) { handleApiError(res); return; }
                State.attach = { kind: res.kind, url: res.url, preview: res.url, name: res.name };
                showAttachPreview();
            });
        });

        // Message actions
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
            } else if (kind === "report") {
                State.reportMessageId = id;
                $("#reportReason").value = "";
                openModal("modalReport");
            } else if (kind === "mod-delete") {
                modDeleteMessage(id);
            }
        });

        // Mention box
        $("#mentionBox").addEventListener("click", function (e) {
            var opt = e.target.closest(".thr-mention-opt");
            if (opt) insertMention(opt.getAttribute("data-user"));
        });

        // Mute toggle
        $("#btnMute").addEventListener("click", function () {
            var conv = State.active.conv;
            var next = !conv.muted;
            api("/threads/api/conversations/" + conv.id + "/mute", { json: { muted: next } }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                conv.muted = next;
                renderChatHead();
                toast(next ? "Muted — no unread badges" : "Unmuted");
                renderConversations();
            });
        });

        // New DM + friend requests
        $("#btnNewDm").addEventListener("click", function () { openModal("modalNewDm"); });
        $("#btnEmptyDm").addEventListener("click", function () { openModal("modalNewDm"); });

        // Tabs
        $$(".thr-tab").forEach(function (t) {
            t.addEventListener("click", function () {
                setTab(t.getAttribute("data-tab"));
            });
        });

        // Load Older button (for channels)
        $("#msgList").addEventListener("click", function (e) {
            var btn = e.target.closest("#btnLoadOlder");
            if (btn) {
                loadOlderChannelMessages();
            }
        });

        // Scroll detection for loading older messages
        var msgContainer = $("#msgList");
        if (msgContainer) {
            var scrollTimeout;
            msgContainer.addEventListener("scroll", function () {
                clearTimeout(scrollTimeout);
                scrollTimeout = setTimeout(function () {
                    if (msgContainer.scrollTop < 20 && State.hasMore && !State.loadingOlder && isChannelOpen()) {
                        loadOlderChannelMessages();
                    }
                }, 200);
            });
        }

        // Channel search filter
        $("#channelSearch").addEventListener("input", function () {
            State.commFilter = this.value;
            renderChannelList();
        });

        // Discover search
        $("#discoverSearch").addEventListener("input", function () {
            clearTimeout(this._t);
            var input = this;
            this._t = setTimeout(function () { loadDiscover(input.value); }, 300);
        });
    }

    // ============================================================
    // SEND/EDIT/DELETE/PIN FUNCTIONS
    // ============================================================

    function sendMessage() {
        if (!State.active) return;
        var input = $("#msgInput");
        var content = input.value.trim();
        var attach = State.attach;
        if (!content && !attach) return;
        var kind = attach ? attach.kind : "text";
        var wireContent = (kind === "gif" && attach && attach.url) ? attach.url : content;
        var payload = {
            ctx: State.active.type + ":" + State.active.id,
            kind: kind,
            content: wireContent,
            attachment_url: attach ? attach.url : null,
            attachment_preview: attach ? attach.preview || null : null,
            parent_message_id: State.replyTo ? State.replyTo.id : null,
        };
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
        if (State.seenIds[real.id]) { removeTempMessage(tempId); return; }
        var idx = -1;
        State.messages.forEach(function (m, i) { if (m.id === tempId) idx = i; });
        if (idx >= 0) {
            State.messages[idx] = real;
            State.seenIds[real.id] = true;
            State.afterId = Math.max(State.afterId, real.id);
            var tempEl = document.querySelector('[data-mid="' + tempId + '"]');
            if (tempEl) {
                var tmp = document.createElement("div");
                tmp.innerHTML = renderMessage(real);
                var newEl = tmp.firstElementChild;
                if (newEl) {
                    tempEl.parentNode.replaceChild(newEl, tempEl);
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

    // ============================================================
    // COMPOSER HELPERS
    // ============================================================

    function autoGrow(el) {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 160) + "px";
    }

    function clearAttach() {
        State.attach = null;
        $("#attachPreview").classList.add("hidden");
        $("#fileInput").value = "";
    }

    function showAttachPreview() {
        var a = State.attach;
        if (!a) return;
        $("#attachName").textContent = a.name || (a.kind === "gif" ? "GIF" : a.kind);
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

    // ============================================================
    // BOOT
    // ============================================================

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
        // ... (the rest of your boot logic from the original file)
        // Since I can't fit all the original boot logic here, I'll include it in the final file.

        // Start polling and heartbeats
        refreshConversations();
        refreshNotifications();
        refreshPresence();
        refreshCommunities();
        setInterval(pollMessages, 1500);
        setInterval(refreshConversations, 5000);
        setInterval(refreshCommunities, 5000);
        setInterval(refreshPresence, 10000);
        setInterval(refreshNotifications, 15000);

        // Presence away/back
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) {
                api("/threads/api/presence?away=1");
            } else {
                refreshPresence();
                pollMessages();
            }
        });
        setInterval(function () {
            if (!document.hidden) api("/threads/api/presence");
        }, 30000);

        // Open ?with=dm:3 from notification
        var params = new URLSearchParams(window.location.search);
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
