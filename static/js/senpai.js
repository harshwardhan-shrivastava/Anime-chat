/* ======================================================
   "YOUR SENPAI" — AI character chat
   Picker → lock modal → chat interface
====================================================== */

(function () {
    "use strict";

    // ---- DOM refs ----
    var pickerView = document.getElementById("senpaiPickerView");
    var pickerSection = document.getElementById("senpaiPickerSection");
    var chatView = document.getElementById("senpaiChatView");
    var messagesEl = document.getElementById("senpaiMessages");
    var inputEl = document.getElementById("senpaiInput");
    var sendBtn = document.getElementById("senpaiSendBtn");
    var typingEl = document.getElementById("senpaiTyping");
    var switchBtn = document.getElementById("senpaiSwitchBtn");
    var cooldownEl = document.getElementById("senpaiCooldown");
    var cooldownLabel = document.getElementById("senpaiCooldownLabel");
    var chatAvatar = document.getElementById("senpaiChatAvatar");
    var chatName = document.getElementById("senpaiChatName");
    var chatAnime = document.getElementById("senpaiChatAnime");

    // Search refs
    var searchInput = document.getElementById("senpaiSearch");
    var searchClear = document.getElementById("senpaiSearchClear");
    var searchGrid = document.getElementById("senpaiSearchGrid");
    var searchLoading = document.getElementById("senpaiSearchLoading");
    var searchEmpty = document.getElementById("senpaiSearchEmpty");

    // Modal refs
    var lockModal = document.getElementById("senpaiLockModal");
    var lockText = document.getElementById("senpaiLockText");
    var lockCancel = document.getElementById("senpaiLockCancel");
    var lockConfirm = document.getElementById("senpaiLockConfirm");
    var genOverlay = document.getElementById("senpaiGenOverlay");
    var genName = document.getElementById("senpaiGenName");
    var genCancel = document.getElementById("senpaiGenCancel");

    var QUICK_PICKS = window.SENPAI_QUICK_PICKS || [];

    // Pending character for the lock modal
    var pendingChar = null;
    var cooldownTimer = null;
    var searchDebounce = null;
    var sending = false;
    var currentAbort = null;  // AbortController for in-flight choose request

    // ---- Helpers ----
    function esc(text) {
        var d = document.createElement("div");
        d.textContent = text || "";
        return d.innerHTML;
    }

    function api(url, method, body) {
        var opts = { method: method || "GET", headers: {} };
        if (body) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        // 60s timeout — persona gen can be slow on cold starts
        var ctrl = new AbortController();
        opts.signal = ctrl.signal;
        currentAbort = ctrl;
        var timeout = setTimeout(function () { ctrl.abort(); }, 60000);
        return fetch(url, opts).then(function (r) {
            clearTimeout(timeout);
            currentAbort = null;
            return r.json().then(function (data) {
                if (!r.ok) return Promise.reject(data);
                return data;
            });
        }).catch(function (err) {
            clearTimeout(timeout);
            currentAbort = null;
            if (err.name === "AbortError") {
                return Promise.reject({ error: "Request timed out. Try again." });
            }
            return Promise.reject(err);
        });
    }

    function showPicker() {
        pickerView.hidden = false;
        pickerSection.hidden = false;
        chatView.hidden = true;
    }

    function showChat() {
        pickerView.hidden = true;
        pickerSection.hidden = true;
        chatView.hidden = false;
        inputEl.focus();
    }

    // ---- Message rendering ----
    function renderMessage(role, content) {
        var wrap = document.createElement("div");
        wrap.className = "senpai-msg senpai-msg-" + role;
        var bubble = document.createElement("div");
        bubble.className = "senpai-msg-bubble";
        bubble.textContent = content;
        wrap.appendChild(bubble);
        return wrap;
    }

    function renderHistory(history) {
        messagesEl.innerHTML = "";
        (history || []).forEach(function (m) {
            messagesEl.appendChild(renderMessage(m.role, m.content));
        });
        scrollToBottom();
    }

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // ---- Cooldown ----
    function startCooldown(seconds) {
        if (cooldownTimer) clearInterval(cooldownTimer);
        var remaining = seconds;
        function tick() {
            if (remaining <= 0) {
                clearInterval(cooldownTimer);
                cooldownTimer = null;
                cooldownEl.hidden = true;
                switchBtn.hidden = false;
                return;
            }
            var h = Math.floor(remaining / 3600);
            var m = Math.floor((remaining % 3600) / 60);
            cooldownLabel.textContent = "Switch in " + (h > 0 ? h + "h " : "") + m + "m";
            remaining--;
        }
        tick();
        cooldownTimer = setInterval(tick, 1000);
    }

    // ---- Lock modal ----
    function openLockModal(char) {
        pendingChar = char;
        lockText.textContent = 'You\u2019ve chosen ' + char.name + ' as your senpai. You can switch again in 24 hours.';
        lockModal.hidden = false;
    }

    function closeLockModal() {
        lockModal.hidden = true;
        pendingChar = null;
    }

    lockCancel.addEventListener("click", closeLockModal);
    lockModal.addEventListener("click", function (e) {
        if (e.target === lockModal) closeLockModal();
    });

    // Gen overlay cancel — abort in-flight request and hide overlay
    genCancel.addEventListener("click", function () {
        if (currentAbort) { currentAbort.abort(); currentAbort = null; }
        genOverlay.hidden = true;
    });
    genOverlay.addEventListener("click", function (e) {
        if (e.target === genOverlay) {
            if (currentAbort) { currentAbort.abort(); currentAbort = null; }
            genOverlay.hidden = true;
        }
    });

    lockConfirm.addEventListener("click", function () {
        if (!pendingChar) return;
        var char = pendingChar;
        closeLockModal();

        // Show persona-gen overlay (first-time generation can take a few seconds)
        genName.textContent = char.name;
        genOverlay.hidden = false;

        api("/senpai/choose", "POST", { character_id: char.id })
            .then(function (data) {
                genOverlay.hidden = true;
                if (data.error === "cooldown") {
                    alert(data.message || "You can't switch yet.");
                    return;
                }
                if (data.error) {
                    alert(data.error);
                    return;
                }
                // Load the chat
                chatAvatar.src = data.character.image || "";
                chatName.textContent = data.character.name;
                chatAnime.textContent = data.character.anime || "";
                renderHistory(data.history);
                showChat();
                startCooldown(data.remaining_seconds);
            })
            .catch(function (err) {
                genOverlay.hidden = true;
                var msg = (err && err.error) || "Something went wrong. Try again.";
                if (err && err.message) msg = err.message;
                alert(msg);
            });
    });

    // ---- Quick pick buttons ----
    document.querySelectorAll(".senpai-quickpick").forEach(function (btn) {
        btn.addEventListener("click", function () {
            openLockModal({
                id: btn.dataset.id,
                name: btn.dataset.name,
                image: btn.querySelector("img").src,
                anime: btn.querySelector(".senpai-quickpick-anime").textContent,
            });
        });
    });

    // ---- Search ----
    function doSearch(q) {
        q = (q || "").trim();
        if (!q) {
            searchGrid.innerHTML = "";
            searchEmpty.hidden = true;
            searchLoading.hidden = true;
            return;
        }
        searchLoading.hidden = false;
        searchEmpty.hidden = true;
        searchGrid.innerHTML = "";

        fetch("/api/characters/search?q=" + encodeURIComponent(q) + "&limit=24")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                searchLoading.hidden = true;
                var results = data.results || [];
                if (!results.length) {
                    searchEmpty.hidden = false;
                    return;
                }
                results.forEach(function (c) {
                    var card = document.createElement("button");
                    card.className = "senpai-search-card";
                    card.innerHTML =
                        '<img src="' + esc(c.image) + '" alt="' + esc(c.name) + '" loading="lazy">' +
                        '<span class="senpai-search-name">' + esc(c.name) + '</span>' +
                        '<span class="senpai-search-anime">' + esc(c.title) + '</span>';
                    card.addEventListener("click", function () {
                        openLockModal({
                            id: c.id,
                            name: c.name,
                            image: c.image,
                            anime: c.title,
                        });
                    });
                    searchGrid.appendChild(card);
                });
            })
            .catch(function () {
                searchLoading.hidden = true;
                searchEmpty.hidden = false;
            });
    }

    searchInput.addEventListener("input", function () {
        searchClear.hidden = !searchInput.value;
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(function () {
            doSearch(searchInput.value);
        }, 300);
    });

    searchClear.addEventListener("click", function () {
        searchInput.value = "";
        searchClear.hidden = true;
        doSearch("");
        searchInput.focus();
    });

    // ---- Send message ----
    function sendMessage() {
        if (sending) return;
        var text = inputEl.value.trim();
        if (!text) return;

        sending = true;
        sendBtn.disabled = true;

        // Show user message immediately
        messagesEl.appendChild(renderMessage("user", text));
        inputEl.value = "";
        scrollToBottom();

        // Show typing indicator
        typingEl.hidden = false;
        scrollToBottom();

        api("/senpai/message", "POST", { message: text })
            .then(function (data) {
                typingEl.hidden = true;
                if (data.error) {
                    messagesEl.appendChild(renderMessage("assistant", data.error));
                } else {
                    renderHistory(data.history);
                }
                scrollToBottom();
            })
            .catch(function (err) {
                typingEl.hidden = true;
                var msg = (err && err.error) || "Connection error. Try again.";
                messagesEl.appendChild(renderMessage("assistant", msg));
                scrollToBottom();
            })
            .finally(function () {
                sending = false;
                sendBtn.disabled = false;
                inputEl.focus();
            });
    }

    sendBtn.addEventListener("click", sendMessage);
    inputEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // ---- Switch senpai ----
    switchBtn.addEventListener("click", function () {
        showPicker();
        if (cooldownTimer) { clearInterval(cooldownTimer); cooldownTimer = null; }
        cooldownEl.hidden = true;
    });

    // ---- Init: check if user already has an active senpai ----
    api("/senpai/status", "GET")
        .then(function (data) {
            if (data.active && data.character) {
                chatAvatar.src = data.character.image || "";
                chatName.textContent = data.character.name;
                chatAnime.textContent = data.character.anime || "";
                renderHistory(data.history);
                showChat();
                if (data.can_switch) {
                    switchBtn.hidden = false;
                    cooldownEl.hidden = true;
                } else {
                    switchBtn.hidden = true;
                    startCooldown(data.remaining_seconds);
                }
            } else {
                showPicker();
            }
        })
        .catch(function () {
            // Not logged in or error — show picker (which will prompt login)
            showPicker();
        });

})();
