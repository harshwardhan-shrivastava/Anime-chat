// ===============================
// SETUP
// ===============================

const animeSlug = document.body.dataset.animeSlug;

const bigRating = document.getElementById("bigRating");
const ratingStarsDisplay = document.getElementById("ratingStarsDisplay");
const reviewCountLabel = document.getElementById("reviewCountLabel");
const statReviewCount = document.getElementById("statReviewCount");
const ratingBreakdown = document.getElementById("ratingBreakdown");
const heroScore = document.getElementById("heroScore");
const heroStars = document.getElementById("heroStars");
const heroVotesLabel = document.getElementById("heroVotesLabel");

const userStars = document.querySelectorAll("#userStars i");
const selectedRatingLabel = document.getElementById("selectedRatingLabel");
const reviewComment = document.getElementById("reviewComment");
const postReviewBtn = document.getElementById("postReviewBtn");
const reviewError = document.getElementById("reviewError");
const reviewsContainer = document.getElementById("reviewsContainer");
const noReviewsMsg = document.getElementById("noReviewsMsg");

// Injected from the server: logged-in user id (or null for guests).
const currentUserId = document.body.dataset.userId
    ? parseInt(document.body.dataset.userId, 10)
    : null;

let selectedRating = 0;

// ===============================
// 3-DOT REVIEW MENU (Delete / Share)
// ===============================

function buildReviewMenu(reviewId, card) {
    const wrap = document.createElement("div");
    wrap.className = "review-menu-wrap";
    wrap.style.marginLeft = "auto";
    wrap.style.position = "relative";

    const dotBtn = document.createElement("button");
    dotBtn.type = "button";
    dotBtn.className = "review-menu-btn";
    dotBtn.setAttribute("aria-label", "Review options");
    dotBtn.style.cssText = "background:none;border:none;color:#9ca3af;cursor:pointer;font-size:1.6rem;padding:4px 12px;border-radius:8px;line-height:1;";
    dotBtn.textContent = "\u22ee";

    const menu = document.createElement("div");
    menu.className = "review-menu";
    menu.style.cssText = "display:none;position:absolute;right:0;top:100%;background:#1f2937;border:1px solid #374151;border-radius:8px;min-width:130px;z-index:20;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,.4);";

    const deleteOpt = document.createElement("button");
    deleteOpt.type = "button";
    deleteOpt.textContent = "\ud83d\uddd1 Delete";
    deleteOpt.style.cssText = "display:block;width:100%;text-align:left;background:none;border:none;color:#f87171;padding:10px 14px;cursor:pointer;font-size:0.9rem;";
    deleteOpt.addEventListener("mouseenter", () => deleteOpt.style.background = "#374151");
    deleteOpt.addEventListener("mouseleave", () => deleteOpt.style.background = "none");
    deleteOpt.addEventListener("click", () => {
        if (!confirm("Delete your review? You can write a new one after.")) return;
        fetch("/delete-review", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ review_id: reviewId })
        })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    // Review deleted — bring the review form back and reload stats.
                    showReviewForm();
                    loadStats();
                } else {
                    alert(data.error || "Could not delete review.");
                }
            })
            .catch(() => alert("Network error. Please try again."));
    });

    const shareOpt = document.createElement("button");
    shareOpt.type = "button";
    shareOpt.textContent = "\ud83d\udd17 Share";
    shareOpt.style.cssText = "display:block;width:100%;text-align:left;background:none;border:none;color:#e5e7eb;padding:10px 14px;cursor:pointer;font-size:0.9rem;";
    shareOpt.addEventListener("mouseenter", () => shareOpt.style.background = "#374151");
    shareOpt.addEventListener("mouseleave", () => shareOpt.style.background = "none");
    shareOpt.addEventListener("click", () => {
        // Share is a placeholder for now.
        alert("Sharing coming soon!");
        menu.style.display = "none";
    });

    menu.appendChild(deleteOpt);
    menu.appendChild(shareOpt);

    dotBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        menu.style.display = menu.style.display === "none" ? "block" : "none";
    });
    document.addEventListener("click", () => {
        menu.style.display = "none";
    });

    wrap.appendChild(dotBtn);
    wrap.appendChild(menu);
    return wrap;
}

// Like/dislike bar under each review card.
function buildVoteBar(review) {
    // Don't show vote buttons on your own review.
    if (currentUserId && review.user_id === currentUserId) {
        const note = document.createElement("div");
        note.className = "own-review-note";
        note.innerHTML = '<i class="fas fa-user-check"></i> Your review';
        return note;
    }
    const bar = document.createElement("div");
    bar.className = "review-vote-bar";

    function makeBtn(kind) {
        const active = review.user_vote === (kind === "like" ? 1 : 0);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.dataset.kind = kind;
        btn.className = "review-vote-btn" + (active ? (kind === "like" ? " vote-active voted-like" : " vote-active voted-dislike") : "");
        btn.innerHTML = (kind === "like" ? "\ud83d\udc4d" : "\ud83d\udc4e") +
            ' <span class="vote-count">' + (kind === "like" ? (review.likes || 0) : (review.dislikes || 0)) + "</span>";
        return btn;
    }

    const likeBtn = makeBtn("like");
    const dislikeBtn = makeBtn("dislike");
    bar.appendChild(likeBtn);
    bar.appendChild(dislikeBtn);

    function refresh(review_) {
        review = review_;
        bar.innerHTML = "";
        bar.appendChild(makeBtn("like"));
        bar.appendChild(makeBtn("dislike"));
        wire();
    }

    function vote(isLike) {
        if (!currentUserId) {
            alert("Please log in to vote on reviews.");
            return;
        }
        fetch(`/api/anime-review/${review.id}/vote`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ is_like: isLike })
        })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    review.likes = data.likes;
                    review.dislikes = data.dislikes;
                    review.user_vote = data.user_vote;
                    refresh(review);
                } else {
                    alert(data.error || "Could not vote.");
                }
            })
            .catch(() => alert("Network error."));
    }

    function wire() {
        bar.querySelectorAll(".review-vote-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                vote(btn.dataset.kind === "like");
            });
        });
    }
    wire();

    return bar;
}

// Show/hide the review form (used when a review is deleted).
function showReviewForm() {
    const box = document.querySelector(".review-box");
    const existing = document.getElementById("alreadyReviewedMsg");
    if (box) box.style.display = "block";
    if (existing) existing.remove();
}

function hideReviewFormFor(myReview) {
    // User already reviewed: swap the form for a warning notice with delete option.
    const box = document.querySelector(".review-box");
    if (!box) return;
    box.style.display = "none";
    // Hide the "no reviews yet" placeholder while showing the already-reviewed notice.
    if (noReviewsMsg) noReviewsMsg.style.display = "none";

    if (document.getElementById("alreadyReviewedMsg")) return;
    const msg = document.createElement("div");
    msg.id = "alreadyReviewedMsg";
    msg.style.cssText = "background:linear-gradient(135deg,rgba(245,158,11,0.1),rgba(239,68,68,0.08));border:1px solid rgba(245,158,11,0.3);border-radius:12px;padding:20px;margin-bottom:20px;";
    msg.innerHTML = `
        <p style="margin:0 0 8px;color:#f59e0b;font-weight:700;font-size:1rem;">
            <i class="fas fa-exclamation-triangle"></i> You already reviewed this anime
        </p>
        <p style="margin:0 0 12px;color:#9ca3af;font-size:0.85rem;line-height:1.5;">
            Reviews <strong style="color:#f87171;">cannot be edited</strong> after posting. Double-check your spelling before you post!
            If you want to review again, delete this one first — but you'll <strong style="color:#f87171;">lose all XP</strong> from its likes and dislikes.
        </p>
        <button type="button" id="deleteMyReviewBtn" style="background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.4);color:#f87171;padding:8px 18px;border-radius:8px;cursor:pointer;font-weight:600;font-size:0.85rem;">
            <i class="fas fa-trash"></i> Delete &amp; Re-review
        </button>
    `;
    box.parentNode.insertBefore(msg, box);
    document.getElementById("deleteMyReviewBtn").addEventListener("click", function() {
        if (!confirm("Delete your review? You'll lose all XP from likes/dislikes on this review. You can write a new one after.")) return;
        fetch("/delete-review", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ review_id: myReview.id })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showReviewForm();
                loadStats();
            } else {
                alert(data.error || "Could not delete review.");
            }
        })
        .catch(() => alert("Network error. Please try again."));
    });
}

function starsForValue(value) {
    const rounded = Math.round(value * 2) / 2;
    let out = "";
    for (let i = 1; i <= 10; i++) {
        if (rounded >= i) {
            out += "\u2605";
        } else if (rounded + 0.5 === i) {
            out += "\u2bea";
        } else {
            out += "\u2606";
        }
    }
    return out;
}

function emptyStars() {
    return "\u2606\u2606\u2606\u2606\u2606\u2606\u2606\u2606\u2606\u2606";
}

function initialsAvatar(username, avatar, avatarColor) {
    // Real profile picture when the reviewer has one, initials otherwise.
    if (avatar) {
        const img = document.createElement("img");
        img.className = "review-avatar-initial";
        img.style.objectFit = "cover";
        img.src = `/static/images/avatars/${avatar}`;
        img.alt = username || "";
        img.onerror = function () { img.replaceWith(initialsFallback(username, avatarColor)); };
        return img;
    }
    return initialsFallback(username, avatarColor);
}

function initialsFallback(username, avatarColor) {
    const letter = (username || "A").trim().charAt(0).toUpperCase() || "A";
    const div = document.createElement("div");
    div.className = "review-avatar-initial";
    div.textContent = letter;
    if (avatarColor) div.style.background = avatarColor;
    return div;
}

function timeAgo(isoString) {
    if (!isoString) return "";
    const then = new Date(isoString.replace(" ", "T") + "Z");
    const diffMs = Date.now() - then.getTime();
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}

// ===============================
// RENDER STATS (average, breakdown, reviews)
// ===============================

function renderStats(data) {
    const average = data.average || 0;
    const votes = data.votes || 0;

    if (bigRating) bigRating.textContent = votes > 0 ? average.toFixed(2) : "N/A";
    if (ratingStarsDisplay) ratingStarsDisplay.textContent = votes > 0 ? starsForValue(average) : emptyStars();
    if (reviewCountLabel) reviewCountLabel.textContent = `${votes} Community Review${votes === 1 ? "" : "s"}`;
    if (statReviewCount) statReviewCount.textContent = votes;

    heroScore.textContent = votes > 0 ? average.toFixed(1) : "N/A";
    heroStars.textContent = votes > 0 ? starsForValue(average) : emptyStars();
    heroVotesLabel.textContent = votes > 0 ? `${votes} vote${votes === 1 ? "" : "s"}` : "No ratings yet";

    if (ratingBreakdown) {
        ratingBreakdown.innerHTML = "";
        for (let star = 10; star >= 1; star--) {
            const count = (data.breakdown && data.breakdown[String(star)]) || 0;
            const pct = votes > 0 ? Math.round((count / votes) * 100) : 0;

            const row = document.createElement("div");
            row.className = "breakdown-row";
            row.innerHTML = `
                <span class="breakdown-label">${star}\u2605</span>
                <div class="breakdown-track">
                    <div class="breakdown-fill" style="width:${pct}%"></div>
                </div>
                <span class="breakdown-count">${count}</span>
            `;
            ratingBreakdown.appendChild(row);
        }
    }

    reviewsContainer.querySelectorAll(".review-card").forEach(el => el.remove());

    // Only show the current user's own review on the anime page.
    // Other users' reviews are on the /reviews page.
    const myReviews = (data.reviews || []).filter(r => currentUserId && r.user_id === currentUserId);

    if (myReviews.length === 0) {
        noReviewsMsg.style.display = "block";
    } else {
        noReviewsMsg.style.display = "none";

        myReviews.forEach(review => {
            const card = document.createElement("div");
            card.className = "review-card";

            const header = document.createElement("div");
            header.className = "review-header";
            header.appendChild(initialsAvatar(review.username, review.avatar, review.avatar_color));

            const infoWrap = document.createElement("div");
            const nameEl = document.createElement("h3");
            nameEl.textContent = review.username;
            // Reviewer rank badge (D through S+, F for dislike-ratio abusers)
            if (review.rank) {
                const rankBadge = document.createElement("span");
                rankBadge.textContent = review.rank;
                rankBadge.title = `Rank ${review.rank}`;
                rankBadge.className = `rank-badge rank-${review.rank}`;
                nameEl.appendChild(rankBadge);
            }
            const ratingEl = document.createElement("span");
            ratingEl.textContent = `${starsForValue(review.rating)} ${review.rating}/10 \u00b7 ${timeAgo(review.created_at)}`;
            infoWrap.appendChild(nameEl);
            infoWrap.appendChild(ratingEl);
            header.appendChild(infoWrap);

            // 3-dot menu on your own review: Delete / Share
            if (currentUserId && review.user_id === currentUserId) {
                card.style.borderColor = "#22c55e";
                header.appendChild(buildReviewMenu(review.id, card));
            }

            const commentEl = document.createElement("p");
            commentEl.textContent = review.comment && review.comment.length > 0
                ? review.comment
                : "(No written review, just a rating.)";

            card.appendChild(header);
            card.appendChild(commentEl);
            card.appendChild(buildVoteBar(review));
            reviewsContainer.appendChild(card);
        });
    }
}

// ===============================
// FETCH CURRENT STATS ON LOAD
// ===============================

function loadStats() {
    fetch(`/anime-reviews/${animeSlug}`)
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                renderStats(data);
                if (data.my_review) {
                    hideReviewFormFor(data.my_review);
                }
            }
        })
        .catch(() => {
            // Leave server-rendered fallback values in place.
        });
}

loadStats();

// ===============================
// STAR SELECTION (user picking their rating)
// ===============================

function paintUserStars(value) {
    userStars.forEach(star => {
        const starValue = parseInt(star.dataset.value, 10);
        if (starValue <= value) {
            star.classList.remove("far");
            star.classList.add("fas", "filled");
        } else {
            star.classList.remove("fas", "filled");
            star.classList.add("far");
        }
    });
}

userStars.forEach(star => {
    star.addEventListener("mouseenter", () => {
        paintUserStars(parseInt(star.dataset.value, 10));
    });

    star.addEventListener("click", () => {
        selectedRating = parseInt(star.dataset.value, 10);
        paintUserStars(selectedRating);
        selectedRatingLabel.textContent = `Your rating: ${selectedRating} star${selectedRating === 1 ? "" : "s"}`;
    });
});

const userStarsWrapper = document.getElementById("userStars");
userStarsWrapper.addEventListener("mouseleave", () => {
    paintUserStars(selectedRating);
});

// ===============================
// SUBMIT RATING + REVIEW
// ===============================

function postReview() {
    reviewError.textContent = "";

    if (!selectedRating || selectedRating < 1) {
        reviewError.textContent = "Please select a star rating before posting.";
        return;
    }

    postReviewBtn.disabled = true;
    postReviewBtn.textContent = "Posting...";

    fetch("/rate-anime", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            anime_slug: animeSlug,
            rating: selectedRating,
            comment: reviewComment.value.trim()
        })
    })
        .then(res => res.json())
        .then(data => {
            postReviewBtn.disabled = false;
            postReviewBtn.textContent = "Post Review";

            if (!data.success) {
                reviewError.textContent = data.error || "Something went wrong. Please try again.";
                return;
            }

            renderStats(data);

            // Swap the form for the one-review notice right away.
            hideReviewFormFor({ rating: selectedRating, comment: reviewComment.value.trim() });

            selectedRating = 0;
            paintUserStars(0);
            selectedRatingLabel.textContent = "Tap a star to rate out of 10";
            reviewComment.value = "";
        })
        .catch(() => {
            postReviewBtn.disabled = false;
            postReviewBtn.textContent = "Post Review";
            reviewError.textContent = "Network error. Please try again.";
        });
}

if (postReviewBtn) postReviewBtn.addEventListener("click", postReview);

// ===============================
// SEASON EPISODE EXPAND / COLLAPSE
// ===============================

document.querySelectorAll(".expand-btn").forEach(btn => {
    btn.addEventListener("click", () => {
        const card = btn.closest(".season-card");
        const list = card ? card.querySelector(".episode-list") : btn.nextElementSibling;
        if (!list) return;

        const isOpen = list.classList.toggle("show");
        btn.classList.toggle("active", isOpen);
        btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
    });
});

// ===============================
// INLINE EPISODE QUICK-RATE
// ===============================

document.querySelectorAll(".episode-rate-btn").forEach(btn => {
    btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        var link = btn.closest(".episode-link");
        if (!link) return;

        // Remove any existing popup
        var existing = document.querySelector(".quick-rate-popup");
        if (existing) existing.remove();

        // Parse season and episode from the link href
        var href = link.getAttribute("href") || "";
        var parts = href.split("/");
        var seasonIdx = parts[parts.length - 2] || "1";
        var epNum = parts[parts.length - 1] || "1";
        var slug = window.location.pathname.split("/").pop();

        // Build popup
        var popup = document.createElement("div");
        popup.className = "quick-rate-popup";
        var html = '<div class="quick-rate-inner"><span class="quick-rate-title">Rate Episode ' + epNum + '</span><div class="quick-rate-stars">';
        for (var i = 1; i <= 10; i++) {
            html += '<button class="qr-star" data-val="' + i + '">' + i + '</button>';
        }
        html += '</div></div>';
        popup.innerHTML = html;
        btn.parentElement.appendChild(popup);

        // Handle star clicks
        popup.querySelectorAll(".qr-star").forEach(star => {
            star.addEventListener("click", function () {
                var val = parseInt(star.dataset.val);
                fetch("/api/rate-episode", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        anime_slug: slug,
                        season_name: "Season " + seasonIdx,
                        episode_number: parseInt(epNum),
                        rating: val
                    })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        btn.innerHTML = '<i class="fas fa-star"></i> ' + val;
                        btn.classList.add("rated");
                        popup.remove();
                    } else {
                        alert(data.error || "Failed to rate");
                    }
                })
                .catch(() => alert("Network error"));
            });
        });

        // Close on outside click
        setTimeout(function () {
            document.addEventListener("click", function closePopup(ev) {
                if (!popup.contains(ev.target) && ev.target !== btn) {
                    popup.remove();
                    document.removeEventListener("click", closePopup);
                }
            });
        }, 10);
    });
});
