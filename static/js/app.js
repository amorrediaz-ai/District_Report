(function () {
  "use strict";

  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  const headerMenu = document.querySelector(".site-menu");
  if (headerMenu) {
    document.addEventListener("click", function (event) {
      if (!headerMenu.contains(event.target)) headerMenu.open = false;
    });
    headerMenu.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && headerMenu.open) {
        headerMenu.open = false;
        headerMenu.querySelector("summary").focus();
      }
    });
    headerMenu.addEventListener("focusout", function (event) {
      if (event.relatedTarget && !headerMenu.contains(event.relatedTarget)) headerMenu.open = false;
    });
  }

  /* Счётчики и предупреждение о несохранённых изменениях формы. */
  var editor = document.getElementById("report_editor");
  if (editor) {
    var dirty = false;
    function updateCounts() {
      editor.querySelectorAll("[data-count-for]").forEach(function (counter) {
        var field = editor.elements.namedItem(counter.dataset.countFor);
        counter.textContent = field.value.length + " / " + field.maxLength;
      });
    }
    editor.addEventListener("input", function () { dirty = true; updateCounts(); });
    editor.addEventListener("change", function () { dirty = true; });
    editor.addEventListener("submit", function () { dirty = false; });
    window.addEventListener("beforeunload", function (event) {
      if (dirty) { event.preventDefault(); event.returnValue = ""; }
    });
    updateCounts();
  }

  /* ---------- Лайки ---------- */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".like_btn");
    if (!btn) return;
    e.preventDefault();
    if (btn.disabled) return;
    btn.disabled = true;

    fetch(btn.dataset.likeUrl, {
      method: "POST",
      headers: { "X-CSRF-Token": csrf, "Accept": "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        btn.classList.toggle("liked", data.liked);
        btn.setAttribute("aria-pressed", data.liked ? "true" : "false");
        btn.querySelector(".like_count").textContent = data.likes;
      })
      .catch(function () { alert("Не удалось поставить отметку. Обновите страницу и попробуйте снова."); })
      .then(function () { btn.disabled = false; });
  });

  /* ---------- Живой поиск и фильтры (без перезагрузки страницы) ---------- */
  var form = document.getElementById("filter_form");
  var results = document.getElementById("results");
  if (form && results) {
    var timer = null, ctrl = null;

    function refresh() {
      var params = new URLSearchParams(new FormData(form));
      Array.from(params.keys()).forEach(function (k) { if (!params.get(k)) params.delete(k); });
      if (params.get("sort") === "new") params.delete("sort");
      var url = form.action.split("?")[0] + (params.toString() ? "?" + params : "");

      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      results.classList.add("loading");

      fetch(url, { signal: ctrl.signal, credentials: "same-origin" })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, "text/html");
          var fresh = doc.getElementById("results");
          if (!fresh) { window.location.href = url; return; }
          results.innerHTML = fresh.innerHTML;
          var reset = doc.querySelector(".filter_row .reset_link");
          var cur = form.querySelector(".reset_link");
          if (reset && !cur) form.querySelector(".filter_row").appendChild(reset);
          if (!reset && cur) cur.remove();
          history.replaceState(null, "", url);
        })
        .catch(function (err) { if (err.name !== "AbortError") window.location.href = url; })
        .then(function () { results.classList.remove("loading"); });
    }

    form.addEventListener("submit", function (e) { e.preventDefault(); clearTimeout(timer); refresh(); });
    form.querySelector('input[name="q"]').addEventListener("input", function () {
      clearTimeout(timer); timer = setTimeout(refresh, 300);
    });
    form.querySelectorAll("select").forEach(function (s) { s.addEventListener("change", refresh); });
  }


  /* Несколько фотографий: предпросмотр и общий лимит с сохранёнными фото. */
  var input = document.getElementById("photo_input");
  if (input) {
    var drop = document.getElementById("photo_drop"),
        preview = document.getElementById("photo_preview"),
        clearBtn = document.getElementById("photo_clear"),
        err = document.getElementById("photo_error"),
        objectUrls = [], maxMb = Number(input.dataset.maxMb),
        maxPhotos = Number(input.dataset.maxPhotos), existing = Number(input.dataset.existingCount);

    function photoError() {
      var files = Array.from(input.files);
      var removed = input.form.querySelectorAll('input[name="remove_photos"]:checked').length;
      if (files.length + existing - removed > maxPhotos) return "Всего можно сохранить не более " + maxPhotos + " фото. Уберите лишние или отметьте старые для удаления.";
      for (var file of files) {
        if (!/^image\/(jpeg|png|webp|gif)$/.test(file.type)) return "Допустимы JPG, PNG, WebP и GIF.";
        if (file.size > maxMb * 1024 * 1024) return "Каждое фото должно быть не больше " + maxMb + " МБ.";
      }
      return "";
    }
    function showPhotos() {
      objectUrls.forEach(function (url) { URL.revokeObjectURL(url); });
      objectUrls = []; preview.replaceChildren();
      err.textContent = photoError();
      var files = Array.from(input.files);
      files.slice(0, maxPhotos).forEach(function (file) {
        if (!/^image\/(jpeg|png|webp|gif)$/.test(file.type) || file.size > maxMb * 1024 * 1024) return;
        var figure = document.createElement("figure"), img = document.createElement("img"), caption = document.createElement("figcaption");
        var url = URL.createObjectURL(file); objectUrls.push(url);
        img.src = url; img.alt = "Выбранное фото: " + file.name;
        caption.textContent = file.name;
        figure.append(img, caption); preview.appendChild(figure);
      });
      preview.hidden = !preview.childElementCount; clearBtn.hidden = !files.length;
    }
    input.addEventListener("change", showPhotos);
    input.form.querySelectorAll('input[name="remove_photos"]').forEach(function (box) {
      box.addEventListener("change", function () { err.textContent = photoError(); });
    });
    input.form.addEventListener("submit", function (event) {
      var error = photoError();
      if (error) { event.preventDefault(); dirty = true; err.textContent = error; input.focus(); }
    });
    clearBtn.addEventListener("click", function () { input.value = ""; showPhotos(); input.dispatchEvent(new Event("change", {bubbles: true})); });
    ["dragenter", "dragover"].forEach(function (name) { drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.add("drag"); }); });
    ["dragleave", "drop"].forEach(function (name) { drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.remove("drag"); }); });
    drop.addEventListener("drop", function (event) {
      if (event.dataTransfer.files.length) {
        try { input.files = event.dataTransfer.files; input.dispatchEvent(new Event("change", {bubbles: true})); }
        catch (_) { err.textContent = "Выберите фото с помощью кнопки загрузки."; }
      }
    });
  }

  /* Только счётчик внутри аккаунта: без email и браузерных push-разрешений. */
  var badge = document.getElementById("notification_count"), checking = false;
  if (badge) {
    setInterval(function () {
      if (document.hidden || checking) return;
      checking = true;
      var controller = new AbortController(), timeout = setTimeout(function () { controller.abort(); }, 10000);
      fetch(badge.dataset.url, {credentials: "same-origin", cache: "no-store", signal: controller.signal})
        .then(function (response) { if (!response.ok) throw new Error(response.status); return response.json(); })
        .then(function (data) { badge.textContent = data.unread; badge.hidden = !data.unread; badge.setAttribute("aria-label", "Непрочитанных: " + data.unread); })
        .catch(function () {})
        .finally(function () { clearTimeout(timeout); checking = false; });
    }, 60000);
  }

  /* ---------- Автоскрытие уведомлений ---------- */
  document.querySelectorAll(".flash_wrap .flash_success, .flash_wrap .flash_info").forEach(function (el) {
    setTimeout(function () { el.style.transition = "opacity .4s"; el.style.opacity = "0"; setTimeout(function () { el.remove(); }, 450); }, 4500);
  });
})();

