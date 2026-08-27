// ===============================
// EPISODE RATE PAGE (5-star UI, stored as 1-10)
// User clicks 1-5 stars → stored as 2-10 (×2)
// AJAX submission — no page reload
// ===============================

const wireStars = document.querySelectorAll("#wireStars .wire-star");
const wireLabel = document.getElementById("wireLabel");
const ratingInput = document.getElementById("ratingInput");
const wireSubmit = document.getElementById("wireSubmit");
const wireError = document.getElementById("wireError");

// Convert stored value (1-10) back to star display (1-5)
let storedRating = ratingInput ? parseInt(ratingInput.value, 10) || 0 : 0;
let selectedStars = Math.round(storedRating / 2);

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
    if (stars === 0) return "Tap a star to rate out of 5";
    const val = stars * 2;
    const labels = ["", "Terrible", "Bad", "Okay", "Good", "Masterpiece"];
    return `Your rating: ${stars}/5 (${val}/10) — ${labels[stars]}`;
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
        storedRating = selectedStars * 2; // 5 stars → store as 10
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
        if (value < 2 || value > 10) {
            wireError.textContent = "Please tap a star between 1 and 5 before submitting.";
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
                // Show success inline — no redirect
                wireSubmit.textContent = "✓ Rated!";
                wireSubmit.style.background = "#22c55e";
                setTimeout(() => {
                    wireSubmit.textContent = "Update Review";
                    wireSubmit.style.background = "";
                    wireSubmit.disabled = false;
                }, 2000);
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
