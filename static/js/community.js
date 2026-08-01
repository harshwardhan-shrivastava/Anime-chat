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

// ===============================
// STATE
// ===============================

let lastSender = null;
let lastGroupEl = null;
let lastMessageTime = null;

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

// ===============================
// ONLINE MEMBERS SIDEBAR (demo data)
// Replace with a real presence feed once
// the backend/WebSocket layer is wired up.
// ===============================

const ONLINE_MEMBERS = [

    { name: "Tanjiro_K", role: "Corps member" },
    { name: "Nezuko_Fan", role: "Corps member" },
    { name: "GiyuStan", role: "Moderator" },
    { name: "InosukeWild", role: "Corps member" },
    { name: "ZenitsuNaps", role: "Corps member" },
    { name: "You", role: "You" }

];

ONLINE_MEMBERS.forEach(function(member) {

    const item = document.createElement("div");

    item.className = "member-item";

    item.innerHTML = `

        <div class="member-avatar" style="background:${colorForName(member.name)}">

            ${initials(member.name)}

        </div>

        <div>

            <span class="member-name">${member.name}</span>

            <span class="member-role">${member.role}</span>

        </div>

    `;

    memberList.appendChild(item);

});

// ===============================
// EMOJI PICKER
// ===============================

const EMOJIS = [
    "😀","😂","😍","😭","😡","👍","🔥","❤️",
    "😱","🎉","😴","🤔","💀","👀","⚔️","🩸",
    "😤","🥲","👏","✨"
];

EMOJIS.forEach(function(emoji) {

    const btn = document.createElement("button");

    btn.textContent = emoji;

    btn.addEventListener("click", function() {

        input.value += emoji;

        input.focus();

        resizeInput();

    });

    emojiGrid.appendChild(btn);

});

// ===============================
// GIF PICKER (demo library)
// ===============================

const GIF_LIBRARY = {

    hype: [
        "https://media.giphy.com/media/f9k1MDpBGdIye/giphy.gif",
        "https://media.giphy.com/media/3o7aD2X7dnQqXvGKcw/giphy.gif"
    ],

    cry: [
        "https://media.giphy.com/media/OPU6wzx8JrHna/giphy.gif",
        "https://media.giphy.com/media/d2lcHJTG5Tscg/giphy.gif"
    ],

    laugh: [
        "https://media.giphy.com/media/l2JehQ2GitHGdVG9y/giphy.gif",
        "https://media.giphy.com/media/BPJmthQ3YRwD6QqcVD/giphy.gif"
    ],

    shock: [
        "https://media.giphy.com/media/26ufp2gNyIiTdyxq4/giphy.gif",
        "https://media.giphy.com/media/xT9IgG50Fb7Mi0prBC/giphy.gif"
    ]

};

function renderGifs(list) {

    gifGrid.innerHTML = "";

    list.forEach(function(url) {

        const img = document.createElement("img");

        img.src = url;

        img.alt = "gif option";

        img.addEventListener("click", function() {

            sendGif(url);

            closePanels();

        });

        gifGrid.appendChild(img);

    });

}

renderGifs([...GIF_LIBRARY.hype, ...GIF_LIBRARY.laugh]);

gifSearch.addEventListener("input", function() {

    const query = gifSearch.value.trim().toLowerCase();

    const match = Object.keys(GIF_LIBRARY).find(function(key) {

        return key.includes(query);

    });

    renderGifs(match ? GIF_LIBRARY[match] : [...GIF_LIBRARY.hype, ...GIF_LIBRARY.laugh]);

});

// ===============================
// PANEL TOGGLING
// ===============================

function closePanels() {

    emojiPanel.classList.remove("show");

    gifPanel.classList.remove("show");

    emojiBtn.classList.remove("active");

    gifBtn.classList.remove("active");

}

emojiBtn.addEventListener("click", function() {

    const isOpen = emojiPanel.classList.contains("show");

    closePanels();

    if (!isOpen) {

        emojiPanel.classList.add("show");

        emojiBtn.classList.add("active");

    }

});

gifBtn.addEventListener("click", function() {

    const isOpen = gifPanel.classList.contains("show");

    closePanels();

    if (!isOpen) {

        gifPanel.classList.add("show");

        gifBtn.classList.add("active");

    }

});

// ===============================
// HELPERS
// ===============================

function timeNow() {

    return new Date();

}

function formatTime(date) {

    return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

}

function removeWelcome() {

    const welcome = document.querySelector(".welcome-box");

    if (welcome) {

        welcome.remove();

    }

}

function maybeInsertDateDivider() {

    const now = timeNow();

    // insert a divider if this is the first message, or 15+ minutes since the last one

    const gapMinutes = lastMessageTime ? (now - lastMessageTime) / 60000 : Infinity;

    if (gapMinutes >= 15) {

        const divider = document.createElement("div");

        divider.className = "date-divider";

        divider.textContent = formatTime(now);

        chatBox.appendChild(divider);

        lastSender = null; // force a fresh group after a divider

        lastGroupEl = null;

    }

    lastMessageTime = now;

}

function attachVoteRow(container) {

    const row = document.createElement("div");

    row.className = "vote-row";

    row.innerHTML = `

        <span class="vote-pill"><i class="fas fa-arrow-up"></i> 0</span>

        <span class="add-reaction-pill">+ react</span>

    `;

    row.querySelector(".vote-pill").addEventListener("click", function() {

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

function startNewGroup(sender, isMine) {

    const group = document.createElement("div");

    group.className = "msg-group" + (isMine ? " mine" : "");

    const color = isMine ? "#3b82f6" : colorForName(sender);

    group.innerHTML = `

        <div class="msg-group-head">

            <div class="avatar" style="background:${color}">${initials(sender)}</div>

            <span class="msg-group-name" style="color:${color}">${sender}</span>

            <span class="msg-group-time">${formatTime(timeNow())}</span>

        </div>

        <div class="msg-lines"></div>

    `;

    chatBox.appendChild(group);

    lastSender = sender;

    lastGroupEl = group;

    return group;

}

function appendLine(group, html) {

    const line = document.createElement("div");

    line.className = "msg-line";

    line.innerHTML = `

        <span class="msg-line-time">${formatTime(timeNow())}</span>

        <div>${html}</div>

    `;

    group.querySelector(".msg-lines").appendChild(line);

}

function renderMessage(sender, isMine, bodyHtml) {

    removeWelcome();

    maybeInsertDateDivider();

    let group;

    if (lastSender === sender && lastGroupEl) {

        group = lastGroupEl;

        appendLine(group, bodyHtml);

    } else {

        group = startNewGroup(sender, isMine);

        appendLine(group, bodyHtml);

    }

    // refresh the vote row so it always sits at the bottom of the group

    const oldRow = group.querySelector(".vote-row");

    if (oldRow) oldRow.remove();

    attachVoteRow(group);

    chatBox.scrollTop = chatBox.scrollHeight;

}

// ===============================
// SEND TEXT MESSAGE
// ===============================

function sendMessage() {

    const text = input.value.trim();

    if (text === "") return;

    renderMessage("You", true, `<p>${text}</p>`);

    input.value = "";

    resizeInput();

    closePanels();

    simulateReply();

}

// ===============================
// SEND GIF MESSAGE
// ===============================

function sendGif(url) {

    renderMessage("You", true, `<div class="gif-attachment"><img src="${url}" alt="sent gif"></div>`);

}

// ===============================
// FAKE TYPING INDICATOR (demo only)
// ===============================

function simulateReply() {

    typingIndicator.classList.add("show");

    setTimeout(function() {

        typingIndicator.classList.remove("show");

    }, 1800);

}

// ===============================
// AUTO-EXPANDING TEXTAREA
// ===============================

function resizeInput() {

    input.style.height = "auto";

    input.style.height = Math.min(input.scrollHeight, 120) + "px";

}

input.addEventListener("input", resizeInput);

// ===============================
// STICKY HEADER SHADOW ON SCROLL
// ===============================

chatBox.addEventListener("scroll", function() {

    if (chatBox.scrollTop > 4) {

        chatHeader.classList.add("scrolled");

    } else {

        chatHeader.classList.remove("scrolled");

    }

});

// ===============================
// EVENTS
// ===============================

sendBtn.addEventListener("click", sendMessage);

input.addEventListener("keydown", function(event) {

    if (event.key === "Enter" && !event.shiftKey) {

        event.preventDefault();

        sendMessage();

    }

});