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
        item.innerHTML = `
            <div class="member-avatar" style="background:${member.avatar_color || colorForName(member.username)}">
                ${initials(member.username)}
            </div>
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

async function searchTenor(query) {
    gifGrid.innerHTML = `<div class="gif-empty-state">Searching Tenor...</div>`;

    try {
        const url = query
            ? `/api/gif-search?q=${encodeURIComponent(query)}`
            : `/api/gif-search`;
        const res = await fetch(url);
        const data = await res.json();

        if (!data.success) {
            gifGrid.innerHTML = `<div class="gif-empty-state">${escapeHtml(data.error || "GIF search unavailable.")}</div>`;
            return;
        }

        renderGifResults(data.results);
    } catch (err) {
        gifGrid.innerHTML = `<div class="gif-empty-state">Couldn't reach Tenor. Try again.</div>`;
    }
}

if (gifSearch) {
    // Load trending gifs as soon as the panel exists
    searchTenor("");

    gifSearch.addEventListener("input", function () {
        clearTimeout(gifSearchTimer);
        const query = gifSearch.value.trim();

        gifSearchTimer = setTimeout(function () {
            searchTenor(query);
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
    emojiBtn.classList.remove("active");
    gifBtn.classList.remove("active");
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

function attachVoteRow(container) {
    const row = document.createElement("div");
    row.className = "vote-row";
    row.innerHTML = `
        <span class="vote-pill"><i class="fas fa-arrow-up"></i> 0</span>
        <span class="add-reaction-pill">+ react</span>
    `;

    row.querySelector(".vote-pill").addEventListener("click", function () {
        const pill = this;
        const count = parseInt(pill.textContent.match(/\d+/)[0], 10);
        const upvoted = pill.classList.toggle("upvoted");
        pill.innerHTML = `<i class="fas fa-arrow-up"></i> ${upvoted ? count + 1 : count - 1}`;
    });

    container.appendChild(row);
}

// ===============================
// MESSAGE GROUPING RENDERER
// ===============================

function startNewGroup(sender, isMine, avatarColor) {
    const group = document.createElement("div");
    group.className = "msg-group" + (isMine ? " mine" : "");

    const color = isMine ? "#3b82f6" : (avatarColor || colorForName(sender));

    group.innerHTML = `
        <div class="msg-group-head">
            <div class="avatar" style="background:${color}">${initials(sender)}</div>
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

function appendLine(group, html, messageDate) {
    const line = document.createElement("div");
    line.className = "msg-line";
    line.innerHTML = `
        <span class="msg-line-time">${formatTime(messageDate)}</span>
        <div>${html}</div>
    `;
    group.querySelector(".msg-lines").appendChild(line);

    const timeEl = group.querySelector(".msg-group-time");
    if (timeEl) timeEl.textContent = formatTime(messageDate);
}

function renderMessage(sender, isMine, bodyHtml, avatarColor, messageDate) {
    removeWelcome();
    maybeInsertDateDivider(messageDate);

    let group;

    if (lastSender === sender && lastGroupEl) {
        group = lastGroupEl;
        appendLine(group, bodyHtml, messageDate);
    } else {
        group = startNewGroup(sender, isMine, avatarColor);
        appendLine(group, bodyHtml, messageDate);
    }

    const oldRow = group.querySelector(".vote-row");
    if (oldRow) oldRow.remove();
    attachVoteRow(group);

    chatBox.scrollTop = chatBox.scrollHeight;
}

function renderIncomingMessage(message) {
    if (renderedIds.has(message.id)) return;
    renderedIds.add(message.id);

    const isMine = CURRENT_USER && message.user_id === CURRENT_USER.id;
    const messageDate = new Date(message.created_at + "Z");

    let bodyHtml;
    if (message.kind === "gif") {
        bodyHtml = `<div class="gif-attachment"><img src="${message.content}" alt="sent gif" loading="lazy"></div>`;
    } else {
        bodyHtml = `<p>${escapeHtml(message.content)}</p>`;
    }

    renderMessage(message.username, isMine, bodyHtml, message.avatar_color, messageDate);
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

    sendBtn.disabled = true;

    try {
        const res = await fetch(`/community/${ANIME_SLUG}/messages`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kind: "text", content: text }),
        });
        const data = await res.json();

        if (data.success) {
            renderIncomingMessage(data.message);
            input.value = "";
            resizeInput();
        } else {
            alert(data.error || "Couldn't send that message.");
        }
    } catch (err) {
        alert("Network error -- try again.");
    } finally {
        sendBtn.disabled = false;
        closePanels();
    }
}

// ===============================
// SEND GIF MESSAGE
// ===============================

async function sendGif(url) {
    if (!CURRENT_USER) return;

    try {
        const res = await fetch(`/community/${ANIME_SLUG}/messages`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kind: "gif", content: url }),
        });
        const data = await res.json();

        if (data.success) {
            renderIncomingMessage(data.message);
        } else {
            alert(data.error || "Couldn't send that gif.");
        }
    } catch (err) {
        alert("Network error -- try again.");
    }
}

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

chatBox.addEventListener("scroll", function () {
    if (chatBox.scrollTop > 4) {
        chatHeader.classList.add("scrolled");
    } else {
        chatHeader.classList.remove("scrolled");
    }
});

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

setInterval(pollMessages, 2500);
setInterval(refreshPresence, 8000);
