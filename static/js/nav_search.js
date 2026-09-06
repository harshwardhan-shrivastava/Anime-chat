// Otakul — header search for anime/episode pages. Uses the same /api/search
// endpoint as the homepage, shown as a dropdown under the nav bar.
(function () {
    "use strict";
    var input = document.getElementById("navSearch");
    var box = document.getElementById("navSearchResults");
    if (!input || !box) return;
    var timer = null;

    function imgSrc(a) {
        if (a.image && a.image.indexOf("http") === 0) return a.image;
        return "/static/images/anime/" + (a.image || "");
    }

    input.addEventListener("input", function () {
        clearTimeout(timer);
        var v = input.value.trim();
        if (!v) {
            box.innerHTML = "";
            box.style.display = "none";
            return;
        }
        timer = setTimeout(function () {
            fetch("/api/search?q=" + encodeURIComponent(v))
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    box.innerHTML = "";
                    if (!d.success || !d.results || !d.results.length) {
                        box.style.display = "none";
                        return;
                    }
                    box.style.display = "block";
                    d.results.slice(0, 6).forEach(function (a) {
                        var row = document.createElement("a");
                        row.className = "nav-search-row";
                        row.href = "/anime/" + a.slug;
                        var img = document.createElement("img");
                        img.src = imgSrc(a);
                        img.alt = "";
                        var info = document.createElement("span");
                        info.className = "nav-search-info";
                        var title = document.createElement("b");
                        title.textContent = a.title;
                        var meta = document.createElement("small");
                        meta.textContent = [
                            a.year || "",
                            a.rating && a.rating !== "N/A" ? "★ " + a.rating : ""
                        ].filter(Boolean).join(" • ");
                        info.appendChild(title);
                        info.appendChild(meta);
                        row.appendChild(img);
                        row.appendChild(info);
                        box.appendChild(row);
                    });
                })
                .catch(function () { box.style.display = "none"; });
        }, 180);
    });

    // Close when clicking outside, Esc to dismiss.
    document.addEventListener("click", function (e) {
        if (!e.target.closest(".nav-search")) box.style.display = "none";
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { box.style.display = "none"; input.blur(); }
    });
})();