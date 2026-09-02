/* ======================================================
   Ota-chan — Otakul AI assistant chat
   Simple chat interface: fetch-on-send, no pickers/locks
====================================================== */

(function () {
    "use strict";

    var messagesEl = document.getElementById("otachanMessages");
    var inputEl = document.getElementById("otachanInput");
    var sendBtn = document.getElementById("otachanSendBtn");
    var typingEl = document.getElementById("otachanTyping");

    var sending = false;
    var initialized = false;

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
        var ctrl = new AbortController();
        opts.signal = ctrl.signal;
        var timeout = setTimeout(function () { ctrl.abort(); }, 60000);
        return fetch(url, opts).then(function (r) {
            clearTimeout(timeout);
            return r.json().then(function (data) {
                if (!r.ok) return Promise.reject(data);
                return data;
            });
        }).catch(function (err) {
            clearTimeout(timeout);
            if (err.name === "AbortError") {
                return Promise.reject({ error: "Request timed out. Try again." });
            }
            return Promise.reject(err);
        });
    }

    // ---- Message rendering ----
    function renderMessage(role, content) {
        var wrap = document.createElement("div");
        wrap.className = "otachan-msg otachan-msg-" + role;
        var bubble = document.createElement("div");
        bubble.className = "otachan-msg-bubble";
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

    function showTyping() {
        typingEl.hidden = false;
        scrollToBottom();
    }

    function hideTyping() {
        typingEl.hidden = true;
    }

    // ---- Send message ----
    function sendMessage() {
        var message = (inputEl.value || "").trim();
        if (!message || sending) return;

        sending = true;
        inputEl.value = "";
        sendBtn.disabled = true;

        // Show user message immediately
        messagesEl.appendChild(renderMessage("user", message));
        scrollToBottom();

        // Show typing indicator
        showTyping();

        api("/otachan/message", "POST", { message: message })
            .then(function (data) {
                hideTyping();
                if (data.error) {
                    messagesEl.appendChild(renderMessage("assistant", "Oops, something went wrong: " + data.error));
                } else {
                    messagesEl.appendChild(renderMessage("assistant", data.reply));
                }
                scrollToBottom();
            })
            .catch(function (err) {
                hideTyping();
                var msg = (err && err.error) || "Something went wrong. Try again.";
                messagesEl.appendChild(renderMessage("assistant", "Oops, " + msg));
                scrollToBottom();
            })
            .finally(function () {
                sending = false;
                sendBtn.disabled = false;
                inputEl.focus();
            });
    }

    // ---- Event listeners ----
    sendBtn.addEventListener("click", sendMessage);
    inputEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // ---- Initialize: load status and show greeting/history ----
    function init() {
        if (initialized) return;
        initialized = true;

        api("/otachan/status", "GET")
            .then(function (data) {
                if (data.has_history && data.history.length > 0) {
                    renderHistory(data.history);
                } else {
                    // Show greeting
                    messagesEl.appendChild(renderMessage("assistant", data.greeting));
                    scrollToBottom();
                }
                inputEl.focus();
            })
            .catch(function (err) {
                // Show default greeting on error
                messagesEl.appendChild(renderMessage("assistant", "Hii, I'm Ota-chan! Ask me anything about Otakul or anime — I've got you."));
                scrollToBottom();
                inputEl.focus();
            });
    }

    // Start on DOM ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
