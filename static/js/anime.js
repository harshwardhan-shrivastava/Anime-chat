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
const reviewUsername = document.getElementById("reviewUsername");
const reviewComment = document.getElementById("reviewComment");
const postReviewBtn = document.getElementById("postReviewBtn");
const reviewError = document.getElementById("reviewError");
const reviewsContainer = document.getElementById("reviewsContainer");
const noReviewsMsg = document.getElementById("noReviewsMsg");

let selectedRating = 0;

// ===============================
// STAR RENDERING HELPERS
// ===============================

function starsForValue(value) {
    const rounded = Math.round(value * 2) / 2;
    let out = "";
    for (let i = 1; i <= 5; i++) {
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

function initialsAvatar(username) {
    const letter = (username || "A").trim().charAt(0).toUpperCase() || "A";
    const div = document.createElement("div");
    div.className = "review-avatar-initial";
    div.textContent = letter;
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

    bigRating.textContent = votes > 0 ? average.toFixed(2) : "N/A";
    ratingStarsDisplay.textContent = votes > 0 ? starsForValue(average) : "\u2606\u2606\u2606\u2606\u2606";
    reviewCountLabel.textContent = `${votes} Community Review${votes === 1 ? "" : "s"}`;
    statReviewCount.textContent = votes;

    heroScore.textContent = votes > 0 ? average.toFixed(1) : "N/A";
    heroStars.textContent = votes > 0 ? starsForValue(average) : "\u2606\u2606\u2606\u2606\u2606";
    heroVotesLabel.textContent = votes > 0 ? `${votes} vote${votes === 1 ? "" : "s"}` : "No ratings yet";

    ratingBreakdown.innerHTML = "";
    for (let star = 5; star >= 1; star--) {
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

    reviewsContainer.querySelectorAll(".review-card").forEach(el => el.remove());

    if (!data.reviews || data.reviews.length === 0) {
        noReviewsMsg.style.display = "block";
    } else {
        noReviewsMsg.style.display = "none";

        data.reviews.forEach(review => {
            const card = document.createElement("div");
            card.className = "review-card";

            const header = document.createElement("div");
            header.className = "review-header";
            header.appendChild(initialsAvatar(review.username));

            const infoWrap = document.createElement("div");
            const nameEl = document.createElement("h3");
            nameEl.textContent = review.username;
            const ratingEl = document.createElement("span");
            ratingEl.textContent = `\u2605 ${review.rating}/5 \u00b7 ${timeAgo(review.created_at)}`;
            infoWrap.appendChild(nameEl);
            infoWrap.appendChild(ratingEl);
            header.appendChild(infoWrap);

            const commentEl = document.createElement("p");
            commentEl.textContent = review.comment && review.comment.length > 0
                ? review.comment
                : "(No written review, just a rating.)";

            card.appendChild(header);
            card.appendChild(commentEl);
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
            username: reviewUsername.value.trim(),
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

            selectedRating = 0;
            paintUserStars(0);
            selectedRatingLabel.textContent = "Tap a star to choose your rating";
            reviewUsername.value = "";
            reviewComment.value = "";
        })
        .catch(() => {
            postReviewBtn.disabled = false;
            postReviewBtn.textContent = "Post Review";
            reviewError.textContent = "Network error. Please try again.";
        });
}

postReviewBtn.addEventListener("click", postReview);
