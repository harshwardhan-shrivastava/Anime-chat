// ===============================
// EPISODE RATE PAGE (5-star UI, stored as 1-10)
// User clicks 1-5 stars → stored as 2-10 (×2)
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

if (wireSubmit) {
    wireSubmit.addEventListener("click", (event) => {
        const value = parseInt(ratingInput.value, 10) || 0;
        if (value < 2 || value > 10) {
            event.preventDefault();
            wireError.textContent = "Please tap a star between 1 and 5 before submitting.";
        }
    });
}
