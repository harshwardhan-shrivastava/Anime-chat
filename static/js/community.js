// ===============================
// ELEMENTS
// ===============================

const chatBox = document.getElementById("chatBox");
const input = document.getElementById("messageInput");
const sendBtn = document.getElementById("sendBtn");
const emojiBtn = document.getElementById("emojiBtn");
const gifBtn = document.getElementById("gifBtn");
const emojiPanel = document.getElementById("emojiPanel");
const gifPanel = document.getElementById("gifPanel");
const emojiGrid = document.getElementById("emojiGrid");
const gifGrid = document.getElementById("gifGrid");
const gifSearch = document.getElementById("gifSearch");
const plusBtn = document.getElementById("plusBtn");
const plusMenu = document.getElementById("plusMenu");
const animePanel = document.getElementById("animePanel");
const animeSearch = document.getElementById("animeSearch");
const animeResults = document.getElementById("animeResults");
const animePanelClose = document.getElementById("animePanelClose");
const typingIndicator = document.getElementById("typingIndicator");
const chatHeader = document.getElementById("chatHeader");
const memberList = document.getElementById("memberList");
const onlineCountEl = document.getElementById("onlineCount");
const sidebarOnlineCountEl = document.getElementById("sidebarOnlineCount");

// ===============================
// CONTEXT (slug + who's logged in, from the template)
// ===============================

const ANIME_SLUG = document.body.dataset.animeSlug;
const CURRENT_USER = JSON.parse(document.body.dataset.user || "null");

let lastSender = null;
let lastGroupEl = null;
let lastMessageTime = null;
let lastMessageId = 0;
let lastReactionId = 0;
let replyTarget = null;
const renderedIds = new Set();

// ===============================
// COLOR-CODED USERNAMES
// ===============================

const NAME_COLORS = ["#00c16a", "#3b82f6", "#f59e0b", "#ec4899", "#9333ea", "#06b6d4", "#ef4444"];

function colorForName(name) {
    let hash = 0;
    for (let i = 0; i < name.length; i++) {
        hash = name.charCodeAt(i) + ((hash << 5) - hash);
    }
    const index = Math.abs(hash) % NAME_COLORS.length;
    return NAME_COLORS[index];
}

function initials(name) {
    return name.trim().slice(0, 2).toUpperCase();
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// ===============================
// ONLINE MEMBERS SIDEBAR (real presence)
// ===============================

function renderMembers(members) {
    if (!memberList) return;

    memberList.innerHTML = "";

    members.forEach(function (member) {
        const isYou = CURRENT_USER && member.username === CURRENT_USER.username;

        const item = document.createElement("div");
        item.className = "member-item";
        const memberAvatar = member && member.avatar
            ? `<div class="member-avatar" style="background:${member.avatar_color || colorForName(member.username)}"><img class="avatar-img" src="/static/images/avatars/${escapeHtml(member.avatar)}" alt=""></div>`
            : `<div class="member-avatar" style="background:${member.avatar_color || colorForName(member.username)}">${initials(member.username)}</div>`;
        item.innerHTML = `
            ${memberAvatar}
            <div>
                <span class="member-name">${escapeHtml(member.username)}</span>
                <span class="member-role">${isYou ? "You" : "Online now"}</span>
            </div>
        `;
        memberList.appendChild(item);
    });
}

async function refreshPresence() {
    try {
        const res = await fetch(`/community/${ANIME_SLUG}/presence`);
        const data = await res.json();
        if (!data.success) return;

        if (onlineCountEl) onlineCountEl.textContent = data.count;
        if (sidebarOnlineCountEl) sidebarOnlineCountEl.textContent = data.count;
        renderMembers(data.members);
    } catch (err) {
        // presence is best-effort, fail quietly
    }
}

// ===============================
// EMOJI PICKER
// ===============================

const EMOJIS = [
    "😀","😂","😍","😭","😡","👍","🔥","❤️",
    "😱","🎉","😴","🤔","💀","👀","⚔️","🩸",
    "😤","🥲","👏","✨"
];

const QUICK_REACT = ["😂", "❤️", "🔥", "😭", "👏"];

if (emojiGrid) {
    EMOJIS.forEach(function (emoji) {
        const btn = document.createElement("button");
        btn.textContent = emoji;
        btn.addEventListener("click", function () {
            input.value += emoji;
            input.focus();
            resizeInput();
        });
        emojiGrid.appendChild(btn);
    });
}

// ===============================
// GIF PICKER -- real Tenor search
// ===============================

let gifSearchTimer = null;

function renderGifResults(gifs) {
    gifGrid.innerHTML = "";

    if (!gifs.length) {
        gifGrid.innerHTML = `<div class="gif-empty-state">No gifs found. Try another search.</div>`;
        return;
    }

    gifs.forEach(function (gif) {
        const img = document.createElement("img");
        img.src = gif.preview || gif.url;
        img.alt = gif.title || "gif";
        img.loading = "lazy";
        img.addEventListener("click", function () {
            sendGif(gif.url);
            closePanels();
        });
        gifGrid.appendChild(img);
    });
}

async function searchGiphy(query) {

    gifGrid.innerHTML = `<div class="gif-empty-state">Searching GIFs...</div>`;

    try {

        const url = query
            ? `/api/gif-search?q=${encodeURIComponent(query)}`
            : `/api/gif-search`;

        const res = await fetch(url);
        const data = await res.json();

        if (!data.success) {
            gifGrid.innerHTML =
                `<div class="gif-empty-state">${escapeHtml(data.error)}</div>`;
            return;
        }

        renderGifResults(data.results);

    } catch (err) {

        gifGrid.innerHTML =
            `<div class="gif-empty-state">Couldn't reach GIPHY.</div>`;

    }

}

if (gifSearch) {
    // Load trending gifs as soon as the panel exists
    searchGiphy("");

    gifSearch.addEventListener("input", function () {
        clearTimeout(gifSearchTimer);
        const query = gifSearch.value.trim();

        gifSearchTimer = setTimeout(function () {
            searchGiphy(query);
        }, 350);
    });
}

// ===============================
// PANEL TOGGLING
// ===============================

function closePanels() {
    if (!emojiPanel) return;
    emojiPanel.classList.remove("show");
    gifPanel.classList.remove("show");
    if (animePanel) animePanel.classList.add("hidden");
    if (plusMenu) plusMenu.classList.add("hidden");
    emojiBtn.classList.remove("active");
    gifBtn.classList.remove("active");
    if (plusBtn) plusBtn.classList.remove("active");
}

if (emojiBtn) {
    emojiBtn.addEventListener("click", function () {
        const isOpen = emojiPanel.classList.contains("show");
        closePanels();
        if (!isOpen) {
            emojiPanel.classList.add("show");
            emojiBtn.classList.add("active");
        }
    });
}

if (gifBtn) {
    gifBtn.addEventListener("click", function () {
        const isOpen = gifPanel.classList.contains("show");
        closePanels();
        if (!isOpen) {
            gifPanel.classList.add("show");
            gifBtn.classList.add("active");
        }
    });
}

// Plus button → emoji / gif / anime menu
if (plusBtn) {
    plusBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        const isOpen = !plusMenu.classList.contains("hidden");
        closePanels();
        if (!isOpen) plusMenu.classList.remove("hidden");
    });
    document.addEventListener("click", function (e) {
        if (plusMenu && !plusMenu.contains(e.target) && e.target !== plusBtn && !plusBtn.contains(e.target)) {
            plusMenu.classList.add("hidden");
        }
    });
}

if (plusMenu) {
    plusMenu.querySelectorAll(".plus-menu-item").forEach(function (btn) {
        btn.addEventListener("click", function () {
            var action = btn.dataset.action;
            plusMenu.classList.add("hidden");
            if (action === "emoji") {
                closePanels();
                emojiPanel.classList.add("show");
                emojiBtn.classList.add("active");
            } else if (action === "gif") {
                closePanels();
                gifPanel.classList.add("show");
                gifBtn.classList.add("active");
            } else if (action === "anime") {
                closePanels();
                animePanel.classList.remove("hidden");
                plusBtn.classList.add("active");
                animeSearch.focus();
            }
        });
    });
}

// Anime search panel
var animeSearchTimer = null;

if (animeSearch) {
    animeSearch.addEventListener("input", function () {
        clearTimeout(animeSearchTimer);
        var q = animeSearch.value.trim();
        if (!q) { animeResults.innerHTML = ""; return; }
        animeResults.innerHTML = '<div class="anime-result-loading">Searching…</div>';
        animeSearchTimer = setTimeout(function () {
            fetch("/api/search?q=" + encodeURIComponent(q))
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data.success || !data.results.length) {
                        animeResults.innerHTML = '<div class="anime-result-loading">No results</div>';
                        return;
                    }
                    animeResults.innerHTML = "";
                    data.results.forEach(function (item) {
                        var card = document.createElement("div");
                        card.className = "anime-result-card";
                        card.innerHTML =
                            '<img src="' + escapeHtml(item.image || "") + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">' +
                            '<div class="anime-result-info">' +
                            '<div class="anime-result-title">' + escapeHtml(item.title) + '</div>' +
                            '<div class="anime-result-meta">' + escapeHtml(item.year || "") + (item.rating ? ' • ' + escapeHtml(item.rating) : "") + '</div>' +
                            '</div>' +
                            '<button class="anime-result-send" title="Send"><i class="fas fa-paper-plane"></i></button>';
                        card.querySelector(".anime-result-send").addEventListener("click", function (e) {
                            e.stopPropagation();
                            sendAnimeCard(item.slug, item.title, item.image, item.year, item.rating);
                        });
                        card.addEventListener("click", function () {
                            sendAnimeCard(item.slug, item.title, item.image, item.year, item.rating);
                        });
                        animeResults.appendChild(card);
                    });
                })
                .catch(function () {
                    animeResults.innerHTML = '<div class="anime-result-loading">Search failed</div>';
                });
        }, 300);
    });
}

if (animePanelClose) {
    animePanelClose.addEventListener("click", function () {
        animePanel.classList.add("hidden");
        plusBtn.classList.remove("active");
    });
}

async function sendAnimeCard(slug, title, image, year, rating) {
    if (!CURRENT_USER) return;
    closePanels();
    animePanel.classList.add("hidden");
    plusBtn.classList.remove("active");

    var payload = { kind: "anime", content: JSON.stringify({ slug: slug, title: title, image: image, year: year || "", rating: rating || "" }) };

    // Optimistic render
    var tempId = -Date.now();
    var optimisticMsg = {
        id: tempId,
        anime_slug: ANIME_SLUG,
        user_id: CURRENT_USER.id,
        username: CURRENT_USER.username,
        avatar_color: CURRENT_USER.avatar_color || colorForName(CURRENT_USER.username),
        avatar: CURRENT_USER.avatar || null,
        kind: "anime",
        content: payload.content,
        reply_to: null,
        created_at: new Date().toISOString().replace("T", " ").slice(0, 19),
        reactions: [],
        my_reactions: [],
        reply_to_username: null,
        reply_to_content: null,
        reply_to_kind: null,
        _temp: true,
    };
    renderIncomingMessage(optimisticMsg);

    try {
        var res = await fetch("/community/" + ANIME_SLUG + "/messages", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        var data = await res.json();
        if (data.success) {
            var tempLine = chatBox.querySelector('.msg-line[data-message-id="' + tempId + '"]');
            if (tempLine) {
                tempLine.dataset.messageId = data.message.id;
                renderedIds.delete(tempId);
                renderedIds.add(data.message.id);
                lastMessageId = Math.max(lastMessageId, data.message.id);
            } else {
                renderIncomingMessage(data.message);
            }
        } else {
            removeTemp(tempId);
            showToast(data.error || "Couldn't send anime card.");
        }
    } catch (err) {
        removeTemp(tempId);
        showToast("Network error — try again.");
    }
}

function removeTemp(tempId) {
    var tempLine = chatBox.querySelector('.msg-line[data-message-id="' + tempId + '"]');
    if (tempLine) {
        var group = tempLine.closest(".msg-group");
        tempLine.remove();
        if (group && !group.querySelector(".msg-line")) group.remove();
    }
    renderedIds.delete(tempId);
}

// ===============================
// HELPERS
// ===============================

function formatTime(isoOrDate) {
    const date = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate + "Z");
    return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function removeWelcome() {
    const welcome = document.querySelector(".welcome-box");
    if (welcome) welcome.remove();
}

function maybeInsertDateDivider(messageDate) {
    const gapMinutes = lastMessageTime ? (messageDate - lastMessageTime) / 60000 : Infinity;

    if (gapMinutes >= 15) {
        const divider = document.createElement("div");
        divider.className = "date-divider";
        divider.textContent = formatTime(messageDate);
        chatBox.appendChild(divider);

        lastSender = null;
        lastGroupEl = null;
    }

    lastMessageTime = messageDate;
}

// ===============================
// TOAST
// ===============================

let toastTimer = null;

function showToast(text) {
    let toast = document.getElementById("communityToast");
    if (!toast) {
        toast = document.createElement("div");
        toast.id = "communityToast";
        toast.className = "community-toast";
        document.body.appendChild(toast);
    }
    toast.textContent = text;
    toast.classList.remove("out");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.classList.add("out"); }, 2400);
}

// ===============================
// MESSAGE CONTENT (spoilers, gifs, newlines)
// ===============================

function buildBodyHtml(message) {
    if (message.kind === "gif") {
        return `<div class="gif-attachment"><img src="${message.content}" alt="sent gif" loading="lazy"></div>`;
    }

    if (message.kind === "anime") {
        try {
            var data = JSON.parse(message.content);
            var img = data.image ? `<img src="${escapeHtml(data.image)}" alt="" loading="lazy" onerror="this.style.display='none'">` : '';
            var meta = [data.year, data.rating].filter(Boolean).join(' • ');
            return `<a href="/anime/${escapeHtml(data.slug)}" target="_blank" class="anime-card-msg">
                <div class="anime-card-msg-img">${img}</div>
                <div class="anime-card-msg-info">
                    <div class="anime-card-msg-title">${escapeHtml(data.title)}</div>
                    ${meta ? `<div class="anime-card-msg-meta">${escapeHtml(meta)}</div>` : ''}
                </div>
                <div class="anime-card-msg-arrow"><i class="fas fa-external-link-alt"></i></div>
            </a>`;
        } catch (e) {
            return `<p class="msg-text">${escapeHtml(message.content)}</p>`;
        }
    }

    let html = escapeHtml(message.content);

    // Spoiler tags: [spoiler]text[/spoiler] -> blurred until clicked
    html = html.replace(
        /\[spoiler\]((?:.|\n)*?)\[\/spoiler\]/gi,
        '<span class="spoiler" tabindex="0">$1</span>'
    );

    html = html.replace(/\n/g, "<br>");

    return `<p class="msg-text">${html}</p>`;
}

function replyQuoteHtml(message) {
    const text = message.reply_to_kind === "gif" ? "sent a GIF" : (message.reply_to_content || "");
    return `
        <button type="button" class="reply-quote" title="Jump to original message">
            <span class="rq-icon"><i class="fas fa-reply"></i></span>
            <span class="rq-body">
                <strong>@${escapeHtml(message.reply_to_username)}</strong>
                ${escapeHtml(text)}
            </span>
        </button>
    `;
}

// ===============================
// REACTIONS
// ===============================

function renderChips(line, reactions, myReactions) {
    const chipsEl = line.querySelector(".reaction-chips");
    if (!chipsEl) return;
    chipsEl.innerHTML = "";

    (reactions || []).forEach(function (r) {
        const chip = document.createElement("button");
        chip.className = "reaction-chip" + ((myReactions || []).indexOf(r.emoji) !== -1 ? " mine" : "");
        chip.dataset.emoji = r.emoji;
        chip.textContent = `${r.emoji} ${r.count}`;
        chip.title = "Toggle reaction";
        chip.addEventListener("click", function () { toggleReaction(line, r.emoji); });
        chipsEl.appendChild(chip);
    });
}

function sortChips(chipsEl) {
    const chips = Array.prototype.slice.call(chipsEl.querySelectorAll(".reaction-chip"));
    chips.sort(function (a, b) {
        const aCount = parseInt((a.textContent.match(/\d+/) || [0])[0], 10);
        const bCount = parseInt((b.textContent.match(/\d+/) || [0])[0], 10);
        return bCount - aCount;
    });
    chips.forEach(function (chip) { chipsEl.appendChild(chip); });
}

function applyReactionUpdate(line, update) {
    const chipsEl = line.querySelector(".reaction-chips");
    if (!chipsEl) return;

    let chip = chipsEl.querySelector(`.reaction-chip[data-emoji="${update.emoji}"]`);
    if (!chip) {
        chip = document.createElement("button");
        chip.className = "reaction-chip";
        chip.dataset.emoji = update.emoji;
        chip.title = "Toggle reaction";
        chip.addEventListener("click", function () { toggleReaction(line, update.emoji); });
        chipsEl.appendChild(chip);
    }
    chip.textContent = `${update.emoji} ${update.count}`;
    chip.classList.toggle("mine", !!update.mine);
    sortChips(chipsEl);
}

async function toggleReaction(line, emoji) {
    if (!CURRENT_USER) {
        window.location = "/auth/login?next=" + encodeURIComponent(window.location.pathname);
        return;
    }

    const messageId = parseInt(line.dataset.messageId, 10);
    if (!messageId) return;

    try {
        const res = await fetch(`/community/${ANIME_SLUG}/messages/${messageId}/react`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ emoji: emoji }),
        });
        const data = await res.json();

        if (data.success) {
            renderChips(line, data.reactions, data.my_reactions);
            if (data.reaction_id) lastReactionId = Math.max(lastReactionId, data.reaction_id);
        } else {
            showToast(data.error || "Couldn't react.");
        }
    } catch (err) {
        showToast("Network error -- try again.");
    }
}

async function pollReactions() {
    try {
        const res = await fetch(`/community/${ANIME_SLUG}/reactions?after_id=${lastReactionId}`);
        const data = await res.json();
        if (!data.success) return;
        if (data.latest_id > lastReactionId) lastReactionId = data.latest_id;

        data.updates.forEach(function (update) {
            const line = chatBox.querySelector(`.msg-line[data-message-id="${update.message_id}"]`);
            if (line) applyReactionUpdate(line, update);
        });
    } catch (err) {
        // best-effort
    }
}

// ===============================
// REPLY FLOW
// ===============================

function startReply(message) {
    replyTarget = message;

    const nameEl = document.getElementById("replyPreviewName");
    const textEl = document.getElementById("replyPreviewText");
    const bar = document.getElementById("replyPreview");

    if (nameEl) nameEl.textContent = "@" + message.username;

    let preview = message.kind === "gif" ? "sent a GIF" : message.content;
    if (preview.length > 120) preview = preview.slice(0, 120) + "…";
    if (textEl) textEl.textContent = preview;

    if (bar) bar.hidden = false;
    if (input) input.focus();
}

function cancelReply() {
    replyTarget = null;
    const bar = document.getElementById("replyPreview");
    if (bar) bar.hidden = true;
}

const replyCancelBtn = document.getElementById("replyCancelBtn");
if (replyCancelBtn) replyCancelBtn.addEventListener("click", cancelReply);

function jumpToMessage(id) {
    const target = chatBox.querySelector(`.msg-line[data-message-id="${id}"]`);
    if (!target) {
        showToast("That message isn't loaded anymore.");
        return;
    }
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("flash");
    setTimeout(function () { target.classList.remove("flash"); }, 1600);
}

// ===============================
// MESSAGE GROUPING RENDERER
// ===============================

function startNewGroup(sender, isMine, avatarColor, avatar) {
    const group = document.createElement("div");
    group.className = "msg-group" + (isMine ? " mine" : "");

    const color = isMine ? "#3b82f6" : (avatarColor || colorForName(sender));
    const avatarEl = avatar
        ? `<div class="avatar" style="background:${color}"><img class="avatar-img" src="/static/images/avatars/${escapeHtml(avatar)}" alt=""></div>`
        : `<div class="avatar" style="background:${color}">${initials(sender)}</div>`;

    group.innerHTML = `
        <div class="msg-group-head">
            ${avatarEl}
            <span class="msg-group-name" style="color:${color}">${escapeHtml(sender)}</span>
            <span class="msg-group-time"></span>
        </div>
        <div class="msg-lines"></div>
    `;

    chatBox.appendChild(group);

    lastSender = sender;
    lastGroupEl = group;
    return group;
}

function appendLine(group, message, isMine, messageDate) {
    const line = document.createElement("div");
    line.className = "msg-line";
    line.dataset.messageId = message.id;

    const tools = `
        <div class="line-tools">
            <button type="button" class="line-tool reply-tool" title="Reply">
                <i class="fas fa-reply"></i>
            </button>
            <button type="button" class="line-tool react-tool" title="React">+</button>
        </div>
        <div class="quick-react">
            ${QUICK_REACT.map(function (e) {
                return `<button type="button" class="qr-btn" data-emoji="${e}" title="React ${e}">${e}</button>`;
            }).join("")}
        </div>
    `;

    line.innerHTML = `
        <span class="msg-line-time">${formatTime(messageDate)}</span>
        <div class="msg-line-body">
            ${message.reply_to_username ? replyQuoteHtml(message) : ""}
            <div class="msg-content">${buildBodyHtml(message)}</div>
            <div class="reaction-chips"></div>
        </div>
        ${tools}
    `;

    group.querySelector(".msg-lines").appendChild(line);

    const replyBtn = line.querySelector(".reply-tool");
    if (replyBtn) replyBtn.addEventListener("click", function () { startReply(message); });

    const reactBtn = line.querySelector(".react-tool");
    const quickBar = line.querySelector(".quick-react");
    if (reactBtn) {
        reactBtn.addEventListener("click", function () {
            if (quickBar) quickBar.classList.toggle("show");
        });
    }
    line.querySelectorAll(".qr-btn").forEach(function (btn) {
        btn.addEventListener("click", function () { toggleReaction(line, btn.dataset.emoji); });
    });

    const quote = line.querySelector(".reply-quote");
    if (quote) quote.addEventListener("click", function () { jumpToMessage(message.reply_to); });

    renderChips(line, message.reactions || [], message.my_reactions || []);

    const timeEl = group.querySelector(".msg-group-time");
    if (timeEl) timeEl.textContent = formatTime(messageDate);
}

function renderMessage(message) {
    removeWelcome();

    const isMine = CURRENT_USER && message.user_id === CURRENT_USER.id;
    const messageDate = new Date(message.created_at + "Z");

    maybeInsertDateDivider(messageDate);

    let group;
    if (lastSender === message.username && lastGroupEl) {
        group = lastGroupEl;
    } else {
        group = startNewGroup(message.username, isMine, message.avatar_color, message.avatar);
    }

    appendLine(group, message, isMine, messageDate);

    chatBox.scrollTop = chatBox.scrollHeight;
}

function renderIncomingMessage(message) {
    if (renderedIds.has(message.id)) return;
    renderedIds.add(message.id);
    renderMessage(message);
    lastMessageId = Math.max(lastMessageId, message.id);
}

// ===============================
// POLLING FOR NEW MESSAGES
// ===============================

async function pollMessages() {
    try {
        const res = await fetch(`/community/${ANIME_SLUG}/messages?after_id=${lastMessageId}`);
        const data = await res.json();
        if (!data.success) return;

        data.messages.forEach(renderIncomingMessage);
    } catch (err) {
        // network hiccup -- just try again on the next tick
    }
}

// ===============================
// SEND TEXT MESSAGE
// ===============================

async function sendMessage() {
    const text = input.value.trim();
    if (text === "" || !CURRENT_USER) return;

    const payload = { kind: "text", content: text };
    if (replyTarget) payload.reply_to = replyTarget.id;

    // Optimistic send: render the message instantly so Enter feels
    // immediate, even when the backend round-trip is slow.
    const tempId = -Date.now();
    const optimisticMsg = {
        id: tempId,
        anime_slug: ANIME_SLUG,
        user_id: CURRENT_USER.id,
        username: CURRENT_USER.username,
        avatar_color: CURRENT_USER.avatar_color || colorForName(CURRENT_USER.username),
        avatar: CURRENT_USER.avatar || null,
        kind: "text",
        content: text,
        reply_to: replyTarget ? replyTarget.id : null,
        created_at: new Date().toISOString().replace("T", " ").slice(0, 19),
        reactions: [],
        my_reactions: [],
        reply_to_username: replyTarget ? replyTarget.username : null,
        reply_to_content: replyTarget ? replyTarget.content : null,
        reply_to_kind: replyTarget ? "text" : null,
        _temp: true,
    };
    renderIncomingMessage(optimisticMsg);
    input.value = "";
    resizeInput();
    cancelReply();
    closePanels();

    try {
        const res = await fetch(`/community/${ANIME_SLUG}/messages`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();

        if (data.success) {
            // Swap the optimistic message with the server's real copy
            // (server has the real id, created_at, avatar, etc.).
            const tempLine = chatBox.querySelector(`.msg-line[data-message-id="${tempId}"]`);
            if (tempLine) {
                tempLine.dataset.messageId = data.message.id;
                renderedIds.delete(tempId);
                renderedIds.add(data.message.id);
                lastMessageId = Math.max(lastMessageId, data.message.id);
            } else {
                renderIncomingMessage(data.message);
            }
        } else {
            // Remove the optimistic message on failure
            const tempLine = chatBox.querySelector(`.msg-line[data-message-id="${tempId}"]`);
            if (tempLine) {
                const group = tempLine.closest(".msg-group");
                tempLine.remove();
                if (group && !group.querySelector(".msg-line")) group.remove();
            }
            renderedIds.delete(tempId);
            showToast(data.error || "Couldn't send that message.");
        }
    } catch (err) {
        // Remove the optimistic message on network error
        const tempLine = chatBox.querySelector(`.msg-line[data-message-id="${tempId}"]`);
        if (tempLine) {
            const group = tempLine.closest(".msg-group");
            tempLine.remove();
            if (group && !group.querySelector(".msg-line")) group.remove();
        }
        renderedIds.delete(tempId);
        showToast("Network error -- try again.");
    }
}

// ===============================
// SEND GIF MESSAGE
// ===============================

async function sendGif(url) {
    if (!CURRENT_USER) return;

    const payload = { kind: "gif", content: url };
    if (replyTarget) payload.reply_to = replyTarget.id;

    try {
        const res = await fetch(`/community/${ANIME_SLUG}/messages`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();

        if (data.success) {
            renderIncomingMessage(data.message);
            cancelReply();
        } else {
            showToast(data.error || "Couldn't send that gif.");
        }
    } catch (err) {
        showToast("Network error -- try again.");
    }
}

// ===============================
// SIDEBAR TABS + GIF GALLERY
// ===============================

let galleryLoaded = false;
const galleryGridEl = document.getElementById("galleryGrid");

document.querySelectorAll(".sidebar-item").forEach(function (btn) {
    btn.addEventListener("click", function () {
        const tab = this.dataset.tab;
        if (!tab) return;

        document.querySelectorAll(".sidebar-item").forEach(function (b) { b.classList.remove("active"); });
        this.classList.add("active");

        document.querySelectorAll(".tab-panel").forEach(function (p) { p.hidden = true; });

        const panel = document.getElementById("panel-" + tab);
        if (panel) {
            panel.hidden = false;
            if (tab === "gallery" && !galleryLoaded) loadGallery();
        }
    });
});

async function loadGallery() {
    galleryLoaded = true;
    if (!galleryGridEl) return;

    galleryGridEl.innerHTML = `<div class="gallery-empty">Loading the gallery...</div>`;

    try {
        const res = await fetch(`/community/${ANIME_SLUG}/gifs`);
        const data = await res.json();
        if (!data.success) {
            galleryGridEl.innerHTML = `<div class="gallery-empty">Couldn't load the gallery.</div>`;
            return;
        }
        if (!data.gifs.length) {
            galleryGridEl.innerHTML = `<div class="gallery-empty">No GIFs shared yet. Be the first!</div>`;
            return;
        }

        galleryGridEl.innerHTML = "";
        data.gifs.forEach(function (gif) {
            const item = document.createElement("div");
            item.className = "gallery-item";
            item.innerHTML = `<img src="${gif.url}" alt="community gif" loading="lazy">`;
            item.title = "Click to copy the GIF link";
            item.addEventListener("click", function () {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(gif.url).then(
                        function () { showToast("GIF link copied — paste it in the chat 💬"); },
                        function () { showToast(gif.url); }
                    );
                } else {
                    showToast(gif.url);
                }
            });
            galleryGridEl.appendChild(item);
        });
    } catch (err) {
        galleryGridEl.innerHTML = `<div class="gallery-empty">Couldn't load the gallery.</div>`;
    }
}

// ===============================
// SPOILER REVEAL (click to show)
// ===============================

chatBox.addEventListener("click", function (e) {
    const spoiler = e.target.closest(".spoiler");
    if (spoiler) {
        e.stopPropagation();
        spoiler.classList.toggle("revealed");
        return;
    }

    const inQuickBar = e.target.closest(".qr-btn");
    const inReactTool = e.target.closest(".react-tool");
    if (!inQuickBar && !inReactTool) {
        chatBox.querySelectorAll(".quick-react.show").forEach(function (bar) {
            bar.classList.remove("show");
        });
    }
});

// ===============================
// AUTO-EXPANDING TEXTAREA
// ===============================

function resizeInput() {
    if (!input) return;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
}

if (input) input.addEventListener("input", resizeInput);

// ===============================
// STICKY HEADER SHADOW ON SCROLL
// ===============================

if (chatBox) {
    chatBox.addEventListener("scroll", function () {
        if (chatBox.scrollTop > 4) {
            chatHeader.classList.add("scrolled");
        } else {
            chatHeader.classList.remove("scrolled");
        }
    });
}

// ===============================
// EVENTS
// ===============================

if (sendBtn) sendBtn.addEventListener("click", sendMessage);

if (input) {
    input.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            sendMessage();
        }
    });
}

// ===============================
// BOOTSTRAP
// ===============================

pollMessages();
refreshPresence();

setInterval(pollMessages, 1200);
setInterval(pollReactions, 2000);
setInterval(refreshPresence, 8000);
