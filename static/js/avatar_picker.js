(function () {
    "use strict";

    var popup = document.getElementById("avatarPopup");
    var confirmPopup = document.getElementById("avatarConfirmPopup");
    var openBtn = document.getElementById("openAvatarPicker");
    var closeBtn = document.getElementById("closeAvatarPicker");
    var confirmImage = document.getElementById("confirmAvatarImage");
    var previewImage = document.getElementById("selectedAvatarPreview");
    var avatarInput = document.getElementById("selectedAvatarInput");
    var confirmBtn = document.getElementById("confirmAvatar");
    var cancelBtn = document.getElementById("cancelAvatar");
    var chosen = null;

    function avatarUrl(name) {
        return "/static/images/avatars/" + name;
    }

    function closePopups() {
        if (popup) popup.hidden = true;
        if (confirmPopup) confirmPopup.hidden = true;
        document.body.style.overflow = "";
    }

    if (openBtn && popup) {
        openBtn.addEventListener("click", function () {
            chosen = null;
            popup.hidden = false;
            document.body.style.overflow = "hidden";
        });
    }

    if (closeBtn) {
        closeBtn.addEventListener("click", closePopups);
    }

    if (popup) {
        popup.addEventListener("click", function (e) {
            if (e.target === popup) closePopups();
        });
        popup.querySelectorAll(".avatar-option").forEach(function (option) {
            option.addEventListener("click", function () {
                popup.querySelectorAll(".avatar-option").forEach(function (o) {
                    o.classList.remove("selected");
                });
                option.classList.add("selected");
                chosen = option.getAttribute("data-avatar");
                confirmImage.src = avatarUrl(chosen);
                confirmPopup.hidden = false;
            });
        });
    }

    if (confirmBtn) {
        confirmBtn.addEventListener("click", function () {
            if (!chosen) return;
            if (previewImage) previewImage.src = avatarUrl(chosen);
            if (avatarInput) avatarInput.value = chosen;
            closePopups();
        });
    }

    if (cancelBtn) {
        cancelBtn.addEventListener("click", closePopups);
    }

    if (confirmPopup) {
        confirmPopup.addEventListener("click", function (e) {
            if (e.target === confirmPopup) closePopups();
        });
    }

    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closePopups();
    });
})();
