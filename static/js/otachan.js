/* ======================================================
   Ota-chan — Otakul AI assistant chat
   Renders text bubbles + tappable anime card rails (max 5)
====================================================== */

(function () {
    "use strict";

    var messagesEl = document.getElementById("otachanMessages");
    var inputEl = document.getElementById("otachanInput");
    var sendBtn = document.getElementById("otachanSendBtn");
    var typingEl = document.getElementById("otachanTyping");
    var quickEl = document.getElementById("otachanQuick");

    var sending = false;
    var initialized = false;

    // ---- Helpers ----
    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function api(url, method, body) {
        var opts = { method: method || "GET", headers: {} };
        if (body) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        var ctrl = new AbortController();
        opts.signal = ctrl.signal;
        var timeout = setTimeout(function () { ctrl.abort(); }, 90000);
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

    // ---- Card rail (anime recommendation cards, max 5) ----
    function renderCards(cards) {
        if (!cards || !cards.length) return null;

        var rail = el("div", "otachan-cards");
        cards.slice(0, 5).forEach(function (card) {
            var slug = card.slug || "";
            var link = el("a", "oc-card");
            link.href = "/anime/" + encodeURIComponent(slug);
            link.setAttribute("aria-label", card.title);

            var thumb = el("div", "oc-thumb");
            var img = el("img", "oc-img");
            img.loading = "lazy";
            img.alt = card.title || "Anime poster";
            img.src = card.image || "";
            img.addEventListener("error", function () {
                img.style.display = "none";
                thumb.classList.add("oc-noimg");
            });
            thumb.appendChild(img);

            if (card.dub) {
                thumb.appendChild(el("span", "oc-dub", "DUB"));
            }

            var meta = el("div", "oc-meta");
            meta.appendChild(el("div", "oc-title", card.title));
            var subParts = [];
            if (card.rating) subParts.push("\u2605 " + card.rating);
            if (card.eps) subParts.push(card.eps + " eps");
            if (card.members) subParts.push(card.members + " members");
            meta.appendChild(el("div", "oc-sub", subParts.join(" \u00b7 ")));

            link.appendChild(thumb);
            link.appendChild(meta);
            rail.appendChild(link);
        });
        return rail;
    }

    // ---- Message rendering ----
    function renderAssistant(entry) {
        var wrap = el("div", "otachan-msg otachan-msg-assistant");
        var bubble = el("div", "otachan-msg-bubble", entry.content);
        var rail = renderCards(entry.cards);
        if (rail) {
            var holder = el("div", "otachan-msg-holder");
            holder.appendChild(bubble);
            holder.appendChild(rail);
            wrap.appendChild(holder);
        } else {
            wrap.appendChild(bubble);
        }
        return wrap;
    }

    function renderMessage(role, content) {
        var wrap = el("div", "otachan-msg otachan-msg-" + role);
        wrap.appendChild(el("div", "otachan-msg-bubble", content));
        return wrap;
    }

    function renderHistory(history) {
        messagesEl.innerHTML = "";
        (history || []).forEach(function (m) {
            if (m.role === "assistant" && m.cards) {
                messagesEl.appendChild(renderAssistant(m));
            } else {
                messagesEl.appendChild(renderMessage(m.role, m.content));
            }
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
    function sendMessage(messageText) {
        var message = (messageText !== undefined) ? messageText : (inputEl.value || "").trim();
        if (!message || sending) return;

        sending = true;
        inputEl.value = "";
        sendBtn.disabled = true;

        messagesEl.appendChild(renderMessage("user", message));
        scrollToBottom();
        showTyping();

        api("/otachan/message", "POST", { message: message })
            .then(function (data) {
                hideTyping();
                if (data.error) {
                    messagesEl.appendChild(renderMessage("assistant", "Oops, something went wrong: " + data.error));
                } else {
                    messagesEl.appendChild(renderAssistant({ content: data.reply, cards: data.cards }));
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
    sendBtn.addEventListener("click", function () { sendMessage(); });
    inputEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    if (quickEl) {
        quickEl.addEventListener("click", function (e) {
            var btn = e.target.closest(".otachan-chip");
            if (!btn || sending) return;
            sendMessage(btn.getAttribute("data-q"));
        });
    }

    // ---- Initialize: load status and show greeting/history ----
    function init() {
        if (initialized) return;
        initialized = true;

        api("/otachan/status", "GET")
            .then(function (data) {
                if (data.has_history && data.history.length > 0) {
                    renderHistory(data.history);
                } else {
                    messagesEl.appendChild(renderMessage("assistant", data.greeting));
                    scrollToBottom();
                }
                inputEl.focus();
            })
            .catch(function () {
                messagesEl.appendChild(renderMessage("assistant", "Hii, I'm Ota-chan! Ask me anything about Otakul or anime — I've got you."));
                scrollToBottom();
                inputEl.focus();
            });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
