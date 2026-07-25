(function () {
  "use strict";

  function initClientGallery() {
    var dialog = document.getElementById("gallery-lightbox");
    var items = Array.prototype.slice.call(
      document.querySelectorAll("[data-gallery-item]")
    );
    if (!dialog || !items.length || typeof dialog.showModal !== "function") return;

    var fullImage = dialog.querySelector("[data-lightbox-image]");
    var stage = dialog.querySelector(".lightbox-stage");
    var title = dialog.querySelector("[data-lightbox-title]");
    var position = dialog.querySelector("[data-lightbox-position]");
    var previousButton = dialog.querySelector("[data-lightbox-prev]");
    var nextButton = dialog.querySelector("[data-lightbox-next]");
    var closeButton = dialog.querySelector("[data-lightbox-close]");
    var reviewButton = dialog.querySelector("[data-lightbox-review]");
    var favoriteForm = dialog.querySelector("[data-lightbox-favorite-form]");
    var favoriteValue = dialog.querySelector("[data-lightbox-favorite-value]");
    var favoriteButton = dialog.querySelector("[data-lightbox-favorite]");
    var favoriteLabel = dialog.querySelector("[data-lightbox-favorite-label]");
    var selectionState = dialog.querySelector("[data-lightbox-selection-state]");
    var comments = dialog.querySelector("[data-lightbox-comments]");
    var commentForm = dialog.querySelector("[data-lightbox-comment-form]");
    var proofingActions = document.getElementById("proofing-actions");
    var currentIndex = 0;
    var returnToProofing = false;
    var pointerStart = null;

    function isTypingTarget(target) {
      return Boolean(
        target &&
        (target.matches("input, textarea, select") || target.isContentEditable)
      );
    }

    function isInteractiveTarget(target) {
      return Boolean(
        target && target.closest("button, a, input, textarea, select, label, summary")
      );
    }

    function replaceQuery(imageId) {
      var url = new URL(window.location.href);
      if (imageId) {
        url.searchParams.set("lightbox", imageId);
        url.hash = "img-" + imageId;
      } else {
        url.searchParams.delete("lightbox");
        url.hash = "img-" + items[currentIndex].dataset.imageId;
      }
      history.replaceState(null, "", url.pathname + url.search + url.hash);
    }

    function copyComments(item) {
      var source = item.querySelector("[data-proof-comments]");
      var copies = source
        ? Array.prototype.map.call(source.children, function (comment) {
            return comment.cloneNode(true);
          })
        : [];
      if (!copies.length) {
        var empty = document.createElement("p");
        empty.className = "muted lightbox-no-notes";
        empty.textContent = "No notes yet.";
        copies.push(empty);
      }
      comments.replaceChildren.apply(comments, copies);
    }

    function render(index) {
      var item = items[index];
      var link = item.querySelector("[data-lightbox-open]");
      var thumbnail = link.querySelector("img");
      var caption = item.querySelector("figcaption");
      var gridFavoriteForm = item.querySelector(".proof-favorite-form");
      var gridFavoriteButton = gridFavoriteForm.querySelector("button");
      var gridFavoriteValue = gridFavoriteForm.querySelector('[name="favorite"]');
      var gridCommentForm = item.querySelector(".proof-comment-form");
      var isFavorite = gridFavoriteButton.getAttribute("aria-pressed") === "true";

      currentIndex = index;
      fullImage.src = link.href;
      fullImage.alt = thumbnail.alt;
      title.textContent = caption.textContent;
      position.textContent = String(index + 1) + " of " + String(items.length);
      previousButton.disabled = index === 0;
      nextButton.disabled = index === items.length - 1;

      favoriteForm.action = gridFavoriteForm.action;
      favoriteValue.value = gridFavoriteValue.value;
      favoriteButton.setAttribute("aria-pressed", isFavorite ? "true" : "false");
      favoriteButton.classList.toggle("on", isFavorite);
      favoriteButton.querySelector('[aria-hidden="true"]').textContent = isFavorite ? "♥" : "♡";
      favoriteLabel.textContent = isFavorite ? "Remove favorite" : "Add to favorites";
      selectionState.textContent = isFavorite
        ? "Selected as a favorite"
        : "Not selected as a favorite";

      commentForm.action = gridCommentForm.action;
      commentForm.reset();
      copyComments(item);
      replaceQuery(item.dataset.imageId);
    }

    function openAt(index) {
      if (index < 0 || index >= items.length) return;
      render(index);
      if (!dialog.open) {
        document.body.classList.add("lightbox-open");
        dialog.showModal();
        closeButton.focus({ preventScroll: true });
      }
    }

    function moveBy(offset) {
      var nextIndex = currentIndex + offset;
      if (nextIndex >= 0 && nextIndex < items.length) render(nextIndex);
    }

    items.forEach(function (item, index) {
      var link = item.querySelector("[data-lightbox-open]");
      link.addEventListener("click", function (event) {
        if (
          event.defaultPrevented || event.button !== 0 || event.altKey ||
          event.ctrlKey || event.metaKey || event.shiftKey
        ) return;
        event.preventDefault();
        openAt(index);
      });
    });

    previousButton.addEventListener("click", function () { moveBy(-1); });
    nextButton.addEventListener("click", function () { moveBy(1); });
    closeButton.addEventListener("click", function () { dialog.close(); });

    reviewButton.addEventListener("click", function () {
      returnToProofing = true;
      dialog.close();
    });

    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });

    dialog.addEventListener("keydown", function (event) {
      if (isTypingTarget(event.target)) return;
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        moveBy(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        moveBy(1);
      }
    });

    dialog.addEventListener("close", function () {
      var item = items[currentIndex];
      document.body.classList.remove("lightbox-open");
      fullImage.removeAttribute("src");
      replaceQuery("");
      if (returnToProofing && proofingActions) {
        proofingActions.scrollIntoView({ block: "start" });
        proofingActions.focus({ preventScroll: true });
      } else {
        var currentOpener = item.querySelector("[data-lightbox-open]");
        currentOpener.scrollIntoView({ block: "nearest" });
        currentOpener.focus({ preventScroll: true });
      }
      returnToProofing = false;
    });

    stage.addEventListener("pointerdown", function (event) {
      if (!event.isPrimary || event.pointerType === "mouse" || isInteractiveTarget(event.target)) return;
      pointerStart = { id: event.pointerId, x: event.clientX, y: event.clientY };
      if (stage.setPointerCapture) stage.setPointerCapture(event.pointerId);
    });

    stage.addEventListener("pointerup", function (event) {
      if (!pointerStart || pointerStart.id !== event.pointerId) return;
      var deltaX = event.clientX - pointerStart.x;
      var deltaY = event.clientY - pointerStart.y;
      pointerStart = null;
      if (stage.hasPointerCapture && stage.hasPointerCapture(event.pointerId)) {
        stage.releasePointerCapture(event.pointerId);
      }
      if (Math.abs(deltaX) < 55 || Math.abs(deltaX) <= Math.abs(deltaY) * 1.2) return;
      moveBy(deltaX < 0 ? 1 : -1);
    });

    stage.addEventListener("pointercancel", function () { pointerStart = null; });

    var reopenImage = dialog.dataset.reopenImage;
    if (reopenImage) {
      var reopenIndex = items.findIndex(function (item) {
        return item.dataset.imageId === reopenImage;
      });
      if (reopenIndex >= 0) openAt(reopenIndex);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initClientGallery);
  } else {
    initClientGallery();
  }
}());
