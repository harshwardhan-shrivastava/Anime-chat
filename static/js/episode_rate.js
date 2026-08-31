// ===============================
// EPISODE RATE PAGE (10-star UI, stored as 1-10)
// AJAX submission — no page reload
// ===============================

const wireStars = document.querySelectorAll("#wireStars .wire-star");
const wireLabel = document.getElementById("wireLabel");
const ratingInput = document.getElementById("ratingInput");
const wireSubmit = document.getElementById("wireSubmit");
const wireError = document.getElementById("wireError");

// Stored value is already /10, which is the star value.
let storedRating = ratingInput ? parseInt(ratingInput.value, 10) || 0 : 0;
let selectedStars = storedRating;

function paintStars(stars) {
    wireStars.forEach(star => {
        const starValue = parseInt(star.dataset.value, 10);
        if (starValue <= stars) {
            star.classList.remove("far");
            star.classList.add("fas", "filled");
        } else {
            star.classList.remove("fas", "filled");
            star.classList.add("far");
        }
    });
}

function getStarLabel(stars) {
    if (stars === 0) return "Tap a star to rate out of 10";
    const labels = ["", "Terrible", "Bad", "Weak", "Okay", "Decent", "Good", "Great", "Excellent", "Outstanding", "Masterpiece"];
    return `Your rating: ${stars}/10 — ${labels[stars]}`;
}

// Initial paint from a previously submitted review (if any).
if (wireStars.length) {
    paintStars(selectedStars);
    if (selectedStars > 0 && wireLabel) {
        wireLabel.textContent = getStarLabel(selectedStars);
    }
}

wireStars.forEach(star => {
    star.addEventListener("mouseenter", () => {
        paintStars(parseInt(star.dataset.value, 10));
    });

    star.addEventListener("click", () => {
        selectedStars = parseInt(star.dataset.value, 10);
        storedRating = selectedStars; // 10 stars → stored /10 directly
        ratingInput.value = storedRating;
        paintStars(selectedStars);
        wireLabel.textContent = getStarLabel(selectedStars);
        if (wireSubmit) wireSubmit.disabled = false;
        if (wireError) wireError.textContent = "";
    });
});

const wireStarsWrap = document.getElementById("wireStars");
if (wireStarsWrap) {
    wireStarsWrap.addEventListener("mouseleave", () => {
        paintStars(selectedStars);
    });
}

// AJAX submission — no page reload
if (wireSubmit) {
    wireSubmit.addEventListener("click", function (e) {
        e.preventDefault();
        const value = parseInt(ratingInput.value, 10) || 0;
        if (value < 1 || value > 10) {
            wireError.textContent = "Please tap a star between 1 and 10 before submitting.";
            return;
        }

        const body = document.body;
        const slug = body.dataset.animeSlug;
        const sIdx = body.dataset.seasonIdx;
        const epNum = body.dataset.episodeNumber;
        const comment = document.getElementById("wireComment");

        wireSubmit.disabled = true;
        wireSubmit.textContent = "Posting...";
        wireError.textContent = "";

        fetch("/api/episode-rate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                anime_slug: slug,
                season_name: sIdx,
                episode_number: epNum,
                rating: value,
                comment: comment ? comment.value.trim() : ""
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                // Instantly swap form for the already-reviewed notice.
                var wireReview = document.querySelector(".wire-review");
                if (wireReview) wireReview.style.display = "none";
                var warning = document.getElementById("epAlreadyReviewed");
                if (!warning) {
                    var box = document.createElement("div");
                    box.id = "epAlreadyReviewed";
                    box.style.cssText = "background:linear-gradient(135deg,rgba(245,158,11,0.1),rgba(239,68,68,0.08));border:1px solid rgba(245,158,11,0.3);border-radius:12px;padding:20px;margin-bottom:20px;";
                    box.innerHTML = '<p style="margin:0 0 8px;color:#f59e0b;font-weight:700;font-size:1rem;"><i class="fas fa-exclamation-triangle"></i> You already reviewed this episode</p>' +
                        '<p style="margin:0 0 12px;color:#9ca3af;font-size:0.85rem;line-height:1.5;">Reviews <strong style="color:#f87171;">cannot be edited</strong> after posting. If you want to review again, delete this one first — but you\u2019ll <strong style="color:#f87171;">lose all XP</strong> from its likes and dislikes.</p>' +
                        '<button type="button" id="deleteMyEpReviewBtn" style="background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.4);color:#f87171;padding:8px 18px;border-radius:8px;cursor:pointer;font-weight:600;font-size:0.85rem;"><i class="fas fa-trash"></i> Delete &amp; Re-review</button>';
                    var rateH2 = document.querySelector(".content-section h2");
                    if (rateH2 && rateH2.parentNode) rateH2.parentNode.insertBefore(box, rateH2.nextSibling);
                    var delBtn = document.getElementById("deleteMyEpReviewBtn");
                    if (delBtn) delBtn.addEventListener("click", function() {
                        if (!confirm("Delete your review? You'll lose all XP from likes/dislikes.")) return;
                        fetch("/delete-episode-review", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ anime_slug: slug, season_name: sIdx, episode_number: epNum })
                        }).then(function(r){ return r.json(); }).then(function(d){
                            if (d.success) window.location.reload();
                            else alert(d.error || "Could not delete.");
                        }).catch(function(){ alert("Network error."); });
                    });
                }
                // Add the review card to the reviews section instantly.
                var reviewsDiv = document.querySelector(".reviews");
                if (reviewsDiv) {
                    var noReview = reviewsDiv.querySelector(".no-reviews");
                    if (noReview) noReview.remove();
                    var card = document.createElement("div");
                    card.className = "review-card";
                    card.style.borderColor = "#22c55e";
                    var starsText = "";
                    for (var i = 1; i <= 10; i++) { starsText += i <= selectedStars ? "\u2605" : "\u2606"; }
                    card.innerHTML = '<div class="review-header"><div class="review-avatar-initial" style="background:#00c16a">Y</div><div><h3>Your Review</h3><span>' + starsText + ' <span class="review-score">' + value + '/10</span></span></div></div>' +
                        '<p>' + (comment && comment.value.trim() ? comment.value.trim() : '(No written review, just a rating.)') + '</p>' +
                        '<div class="own-review-note"><i class="fas fa-user-check"></i> Your review</div>';
                    reviewsDiv.prepend(card);
                }
            } else {
                wireError.textContent = data.error || "Could not rate.";
                wireSubmit.textContent = "Rate Episode";
                wireSubmit.disabled = false;
            }
        })
        .catch(() => {
            wireError.textContent = "Network error. Please try again.";
            wireSubmit.textContent = "Rate Episode";
            wireSubmit.disabled = false;
        });
    });
}
