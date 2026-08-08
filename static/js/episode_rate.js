// ===============================
// EPISODE RATE PAGE (1-10 stars)
// ===============================

const wireStars = document.querySelectorAll("#wireStars .wire-star");
const wireLabel = document.getElementById("wireLabel");
const ratingInput = document.getElementById("ratingInput");
const wireSubmit = document.getElementById("wireSubmit");
const wireError = document.getElementById("wireError");

let selectedRating = ratingInput ? parseInt(ratingInput.value, 10) || 0 : 0;

function paintStars(value) {
    wireStars.forEach(star => {
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

// Initial paint from a previously submitted review (if any).
if (wireStars.length) {
    paintStars(selectedRating);
}

wireStars.forEach(star => {
    star.addEventListener("mouseenter", () => {
        paintStars(parseInt(star.dataset.value, 10));
    });

    star.addEventListener("click", () => {
        selectedRating = parseInt(star.dataset.value, 10);
        ratingInput.value = selectedRating;
        paintStars(selectedRating);
        wireLabel.textContent = `Your rating: ${selectedRating}/10`;
        if (wireSubmit) wireSubmit.disabled = false;
        if (wireError) wireError.textContent = "";
    });
});

const wireStarsWrap = document.getElementById("wireStars");
if (wireStarsWrap) {
    wireStarsWrap.addEventListener("mouseleave", () => {
        paintStars(selectedRating);
    });
}

if (wireSubmit) {
    wireSubmit.addEventListener("click", (event) => {
        const value = parseInt(ratingInput.value, 10) || 0;
        if (value < 1 || value > 10) {
            event.preventDefault();
            wireError.textContent = "Please tap a star between 1 and 10 before submitting.";
        }
    });
}
