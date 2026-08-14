// ============================================================
// THREADS — Messages tab (Phase 1)
// Full-screen chat client for AnimeChat. Pure vanilla JS, no
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
        seenIds: {},
        afterId: 0,
        firstId: 0,
        hasMore: true,
        loadingOlder: false,
        members: [],
        memberMap: {},         // id -> member row
        presence: {},          // id -> {status, online}
        settings: { read_receipts: true, typing_indicators: true },
        replyTo: null,
        attach: null,          // {kind, url, preview, name}
        editingId: null,
        typingSentAt: 0,
        notifUnread: 0,
        convFilter: "",
        lastSeenText: "",
    };

    // ----------------------------------------------------------
    // Tiny helpers
    // ----------------------------------------------------------
    function $(sel) { return document.querySelector(sel); }
    function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

    function escapeHtml(s) {
        var div = document.createElement("div");
        div.textContent = s == null ? "" : String(s);
        return div.innerHTML;
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
        return fetch(path, opts).then(function (r) { return r.json(); });
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
        if (lm.kind === "gif") return "🎁 GIF";
        if (lm.kind === "image") return "🖼 Image";
        if (lm.kind === "video") return "🎥 Video";
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
                escapeHtml(initials(other.username)) + "</span>";
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
        document.title = unread > 0 ? "(" + unread + ") Threads | AnimeChat" : "Threads | AnimeChat";
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
        State.messages = [];
        State.seenIds = {};
        State.afterId = 0;
        State.firstId = 0;
        State.hasMore = true;
        State.loadingOlder = false;

        $("#emptyState").classList.add("hidden");
        $("#convView").classList.remove("hidden");
        renderChatHead();
        loadHistory();
        markActiveRead();
        $("#msgInput").focus();
    }

    function renderChatHead() {
        var conv = State.active.conv;
        var isDm = conv.type === "dm";
        var name = convDisplayName(conv);
        $("#chatAvatar").innerHTML = isDm
            ? escapeHtml(initials(conv.other ? conv.other.username : "?"))
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
        $("#btnMembers").style.display = conv.type === "group" ? "" : "none";
    }

    function loadHistory() {
        var ctype = State.active.type, cid = State.active.id;
        api("/threads/api/messages?ctx=" + ctype + ":" + cid + "&limit=60").then(function (res) {
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
            renderMessages(true);
            renderPins(res.pins || []);
            refreshPresence();
        });
    }

    function renderMessages(scrollToBottom) {
        var list = $("#msgList");
        var html = "";
        var lastDay = null;
        var limit = State.messages.length;
        for (var i = 0; i < limit; i++) {
            var m = State.messages[i];
            var day = dayKey(m.created_at);
            if (day !== lastDay) {
                html += '<div class="thr-day-divider"><span>' + escapeHtml(fmtDay(m.created_at)) + "</span></div>";
                lastDay = day;
            }
            html += renderMessage(m);
        }
        list.innerHTML = html;

        if (scrollToBottom) {
            list.scrollTop = list.scrollHeight;
        } else {
            var prev = list.scrollTop;
            list.scrollTop = prev; // keep position when prepending older
        }
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
        if (m.attachment_url) {
            if (m.kind === "image" || m.kind === "gif") {
                attach = '<div class="thr-attach"><img src="' + escapeHtml(m.attachment_url) +
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
        }

        var body;
        if (State.editingId === m.id) {
            body = '<div class="thr-edit-box">' +
                '<textarea class="thr-edit-input" data-edit-id="' + m.id + '" rows="2">' + escapeHtml(content) + "</textarea>" +
                '<div class="thr-edit-actions"><button class="thr-btn thr-btn-sm thr-btn-primary" data-act="save-edit" data-id="' + m.id + '">Save</button>' +
                '<button class="thr-btn thr-btn-sm" data-act="cancel-edit">Cancel</button></div></div>';
        } else {
            body = parentRef + (content ? '<div class="thr-msg-content">' + mentions + "</div>" : "") +
                attach +
                '<div class="thr-msg-meta"><span class="thr-msg-time">' + fmtClock(m.created_at) + "</span>" + flags + "</div>";
        }

        return '<div class="' + cls + '" data-mid="' + m.id + '">' +
            '<div class="thr-msg-avatar" style="background:' + escapeHtml(sender.avatar_color || "#8b5cf6") + '">' +
            escapeHtml(initials(sender.username)) + "</div>" +
            '<div class="thr-msg-main">' +
            '<div class="thr-msg-head"><span class="thr-msg-user">' + escapeHtml(sender.username || "unknown") + "</span>" +
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
            var txt = p.content || (p.kind === "gif" ? "GIF" : p.kind === "image" ? "Image" : p.kind === "video" ? "Video" : "");
            $("#pinnedStripText").textContent = "@" + (p.sender ? p.sender.username : "") + ": " +
                (txt.length > 70 ? txt.slice(0, 70) + "…" : txt);
        }
    }

    function updateSeenText() {
        var conv = State.active && State.active.conv;
        if (!conv) return;
        var el = $("#seenText");
        if (!State.settings.read_receipts || conv.type === "group") {
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
        if (!content && !attach) return;
        var kind = attach ? attach.kind : "text";
        var payload = {
            ctx: State.active.type + ":" + State.active.id,
            kind: kind,
            content: content,
            attachment_url: attach ? attach.url : null,
            attachment_preview: attach ? attach.preview || null : null,
            parent_message_id: State.replyTo ? State.replyTo.id : null,
        };
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

    function appendMessages(msgs) {
        var fresh = msgs.filter(function (m) { return !State.seenIds[m.id]; });
        if (!fresh.length) return;
        fresh.forEach(function (m) { State.seenIds[m.id] = true; });
        var list = $("#msgList");
        var stick = list.scrollTop + list.clientHeight >= list.scrollHeight - 80;
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
        if (stick) list.scrollTop = list.scrollHeight;
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
        var last = State.messages[State.messages.length - 1];
        var id = last ? last.id : 0;
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
        api("/threads/api/messages?ctx=" + ctype + ":" + cid + "&after=" + State.afterId).then(function (res) {
            if (!res.success) { handleApiError(res); return; }
            if (res.messages && res.messages.length) {
                appendMessages(res.messages);
                // read-receipt member state refresh
                if (res.members && res.members.length) {
                    State.members = res.members;
                    State.memberMap = {};
                    State.members.forEach(function (m) { State.memberMap[m.id] = m; });
                }
                markActiveRead();
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
                : n.type === "dm" ? "comment-dots" : "bell";
            var text = n.type === "mention" ? " mentioned you"
                : n.type === "reply" ? " replied to your message"
                : n.type === "dm" ? " sent you a message" : "";
            var ago = fmtConvTime(n.created_at);
            return '<div class="thr-notif' + (n.read ? " read" : "") + '" data-ntype="' + n.type + '" data-nctx="' +
                (n.context_type ? n.context_type + ":" + n.context_id : "") + '">' +
                '<span class="thr-notif-avatar" style="background:' + escapeHtml(n.from_color || "#8b5cf6") + '">' +
                escapeHtml(initials(n.from_username)) + "</span>" +
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
        var now = Date.now();
        if (State.settings.typing_indicators && State.active && now - State.typingSentAt > 3000) {
            State.typingSentAt = now;
            api("/threads/api/typing", { json: { ctx: State.active.type + ":" + State.active.id } });
        }
        updateMentionBox();
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
                escapeHtml(initials(mem.username)) + "</span>@" + escapeHtml(mem.username) + "</div>";
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
        $("#attachPreview").classList.add("hidden");
        $("#fileInput").value = "";
    }

    function showAttachPreview() {
        var a = State.attach;
        if (!a) return;
        $("#attachName").textContent = a.name || (a.kind === "gif" ? "GIF" : a.kind);
        $("#attachPreview").classList.remove("hidden");
    }

    // ----------------------------------------------------------
    // Modals
    // ----------------------------------------------------------
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

    // ---- DM modal ----
    function wireDmModal() {
        var input = $("#dmSearch");
        var results = $("#dmResults");
        var t;
        function search() {
            var q = input.value.trim();
            if (!q) { results.innerHTML = ""; return; }
            api("/threads/api/users/search?q=" + encodeURIComponent(q)).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                results.innerHTML = res.users.map(function (u) {
                    return '<div class="thr-user-row" data-uid="' + u.id + '">' +
                        '<span class="thr-avatar thr-avatar-md" style="background:' + escapeHtml(u.avatar_color) + '">' +
                        escapeHtml(initials(u.username)) + "</span>" +
                        "<span>" + escapeHtml(u.username) + "</span></div>";
                }).join("") || '<div class="thr-dropdown-empty">No users found</div>';
            });
        }
        input.addEventListener("input", function () {
            clearTimeout(t);
            t = setTimeout(search, 250);
        });
        results.addEventListener("click", function (e) {
            var row = e.target.closest(".thr-user-row");
            if (!row) return;
            var uid = parseInt(row.getAttribute("data-uid"), 10);
            api("/threads/api/conversations/dm", { json: { user_id: uid } }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                closeModal("modalNewDm");
                input.value = "";
                results.innerHTML = "";
                upsertConversation(res.conversation);
                openConversation(res.conversation.type, res.conversation.id);
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

    // ---- Group modal ----
    var GROUP_COLORS = ["#8b5cf6", "#ef4444", "#f59e0b", "#22c55e", "#3b82f6", "#ec4899", "#06b6d4", "#f97316"];
    var groupPicks = {};

    function wireGroupModal() {
        var nameInput = $("#groupName");
        var sInput = $("#groupSearch");
        var sResults = $("#groupResults");
        var picks = $("#groupPicks");
        var swatches = $("#groupColors");
        var chosenColor = GROUP_COLORS[0];

        swatches.innerHTML = GROUP_COLORS.map(function (c, i) {
            return '<span class="thr-swatch' + (i === 0 ? " chosen" : "") + '" data-color="' + c + '" style="background:' + c + '"></span>';
        }).join("");
        swatches.addEventListener("click", function (e) {
            var sw = e.target.closest(".thr-swatch");
            if (!sw) return;
            chosenColor = sw.getAttribute("data-color");
            $$(".thr-swatch", swatches).forEach(function (s) { s.classList.remove("chosen"); });
            sw.classList.add("chosen");
        });

        function renderPicks() {
            var ids = Object.keys(groupPicks);
            picks.innerHTML = ids.map(function (uid) {
                var u = groupPicks[uid];
                return '<span class="thr-pick"><span class="thr-avatar thr-avatar-sm" style="background:' +
                    escapeHtml(u.avatar_color) + '">' + escapeHtml(initials(u.username)) + "</span>" +
                    escapeHtml(u.username) + ' <button class="thr-link-btn" data-remove="' + uid + '">✕</button></span>';
            }).join("");
            return ids.length;
        }

        function search(q) {
            if (!q) { sResults.innerHTML = ""; return; }
            api("/threads/api/users/search?q=" + encodeURIComponent(q)).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                var ids = Object.keys(groupPicks);
                sResults.innerHTML = res.users.filter(function (u) {
                    return ids.indexOf(String(u.id)) === -1;
                }).map(function (u) {
                    return '<div class="thr-user-row" data-uid="' + u.id + '" data-name="' +
                        escapeHtml(u.username) + '" data-color="' + escapeHtml(u.avatar_color) + '">' +
                        '<span class="thr-avatar thr-avatar-md" style="background:' + escapeHtml(u.avatar_color) + '">' +
                        escapeHtml(initials(u.username)) + "</span><span>" + escapeHtml(u.username) + "</span></div>";
                }).join("") || '<div class="thr-dropdown-empty">No more users</div>';
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
            var uid = row.getAttribute("data-uid");
            groupPicks[uid] = {
                id: parseInt(uid, 10),
                username: row.getAttribute("data-name"),
                avatar_color: row.getAttribute("data-color"),
            };
            renderPicks();
            sInput.value = "";
            sResults.innerHTML = "";
        });
        picks.addEventListener("click", function (e) {
            var b = e.target.closest("[data-remove]");
            if (b) { delete groupPicks[b.getAttribute("data-remove")]; renderPicks(); }
        });

        $("#btnCreateGroup").addEventListener("click", function () {
            var name = nameInput.value.trim();
            if (!name) { toast("Give the group a name", "error"); return; }
            var memberIds = Object.keys(groupPicks).map(Number);
            api("/threads/api/conversations/group", {
                json: { name: name, member_ids: memberIds, avatar_color: chosenColor },
            }).then(function (res) {
                if (!res.success) { handleApiError(res); return; }
                closeModal("modalNewGroup");
                nameInput.value = "";
                groupPicks = {};
                renderPicks();
                upsertConversation(res.conversation);
                openConversation(res.conversation.type, res.conversation.id);
            });
        });
    }

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
            closeModal("modalGif");
            showAttachPreview();
            $("#msgInput").focus();
        });

        $("#btnGif").addEventListener("click", function () {
            openModal("modalGif");
            load(input.value.trim());
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
                    escapeHtml(initials(m.username)) + "</span>" + dot +
                    '<span class="thr-member-name">' + escapeHtml(m.username) + (m.id === State.me.id ? " (you)" : "") + "</span>" +
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
                        escapeHtml(initials(u.username)) + "</span><span>" + escapeHtml(u.username) + "</span></div>";
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

    // ---- Pins modal ----
    function wirePinsModal() {
        var list = $("#pinsList");
        function render() {
            api("/threads/api/messages?ctx=" + State.active.type + ":" + State.active.id + "&limit=1").then(function (res) {
                if (!res.success) return;
                var pins = res.pins || [];
                list.innerHTML = pins.length ? pins.map(function (p) {
                    var txt = p.content || (p.kind === "gif" ? "GIF" : p.kind === "image" ? "Image" : p.kind === "video" ? "Video" : "");
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
                    }
                }
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
        $("#btnAttach").addEventListener("click", function () { $("#fileInput").click(); });
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

        // older messages on scroll-to-top
        $("#msgList").addEventListener("scroll", function () {
            var list = this;
            if (list.scrollTop < 80 && State.hasMore && !State.loadingOlder) {
                State.loadingOlder = true;
                api("/threads/api/messages?ctx=" + State.active.type + ":" + State.active.id +
                    "&before=" + State.firstId + "&limit=60").then(function (res) {
                    State.loadingOlder = false;
                    if (!res.success) { handleApiError(res); return; }
                    if (!res.messages.length) { State.hasMore = false; return; }
                    var before = list.scrollHeight;
                    var html = "";
                    res.messages.forEach(function (m) {
                        if (State.seenIds[m.id]) return;
                        State.seenIds[m.id] = true;
                        html += renderMessage(m);
                    });
                    list.insertAdjacentHTML("afterbegin", html);
                    State.messages = res.messages.concat(State.messages);
                    State.firstId = res.messages[0].id;
                    State.hasMore = res.messages.length >= 60;
                    list.scrollTop = list.scrollHeight - before;
                });
            }
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

        // new DM / group buttons (header + empty state)
        $("#btnNewDm").addEventListener("click", function () { openModal("modalNewDm"); });
        $("#btnEmptyDm").addEventListener("click", function () { openModal("modalNewDm"); });
        $("#btnNewGroup").addEventListener("click", function () { openModal("modalNewGroup"); });

        // tabs (Communities arrives in Phase 2)
        $$(".thr-tab").forEach(function (t) {
            t.addEventListener("click", function () {
                if (t.classList.contains("disabled")) return;
                $$(".thr-tab").forEach(function (x) { x.classList.remove("active"); });
                t.classList.add("active");
            });
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
        wireGroupModal();
        wireGifModal();
        wireMembersModal();
        wirePinsModal();
        wireSettingsModal();
        wireBell();
        wireEvents();

        try {
            State.conversations = JSON.parse(document.body.getAttribute("data-conversations")) || [];
        } catch (e) { State.conversations = []; }
        State.notifUnread = parseInt(document.body.getAttribute("data-notifications") || "0", 10) || 0;
        renderConversations();
        refreshNotifications();

        // heartbeat + polling
        refreshPresence();
        setInterval(pollMessages, 1500);
        setInterval(refreshConversations, 5000);
        setInterval(refreshPresence, 10000);
        setInterval(refreshNotifications, 15000);

        // presence away/back
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) {
                api("/threads/api/presence?away=1");
            } else {
                refreshPresence();
                pollMessages();
            }
        });
        // leave a heart-beat while the tab is open
        setInterval(function () {
            if (!document.hidden) api("/threads/api/presence");
        }, 30000);

        // open ?with=dm:3 from a notification click elsewhere
        var params = new URLSearchParams(window.location.search);
        var open = params.get("open");
        if (open && open.indexOf(":") !== -1) {
            var parts = open.split(":");
            if ((parts[0] === "dm" || parts[0] === "group") && parts[1]) {
                setTimeout(function () { openConversation(parts[0], parseInt(parts[1], 10)); }, 100);
            }
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();