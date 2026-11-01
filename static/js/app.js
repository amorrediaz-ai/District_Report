(function () {
  "use strict";

  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

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


  /* ---------- Фото в форме: предпросмотр и проверка размера ---------- */
  var input = document.getElementById("photo_input");
  if (input) {
    var drop = document.getElementById("photo_drop"),
        preview = document.getElementById("photo_preview"),
        hint = document.getElementById("photo_hint"),
        clearBtn = document.getElementById("photo_clear"),
        err = document.getElementById("photo_error"),
        maxMb = parseFloat(input.dataset.maxMb) || 5,
        objectUrl = null;

    function reset() {
      input.value = "";
      if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
      preview.hidden = true; preview.removeAttribute("src");
      hint.hidden = false; clearBtn.hidden = true;
    }

    function handle(file) {
      err.textContent = "";
      if (!file) { reset(); return; }
      if (!/^image\/(jpeg|png|webp|gif)$/.test(file.type)) {
        reset(); err.textContent = "Допустимы только изображения JPG, PNG, WebP или GIF."; return;
      }
      if (file.size > maxMb * 1024 * 1024) {
        reset(); err.textContent = "Фото слишком большое (максимум " + maxMb + " МБ)."; return;
      }
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(file);
      preview.src = objectUrl; preview.hidden = false;
      hint.hidden = true; clearBtn.hidden = false;
    }

    input.addEventListener("change", function () { handle(input.files[0]); });
    clearBtn.addEventListener("click", function () { err.textContent = ""; reset(); });

    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("drag"); });
    });
    drop.addEventListener("drop", function (e) {
      if (e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files; handle(input.files[0]);
      }
    });
  }

  /* ---------- Автоскрытие уведомлений ---------- */
  document.querySelectorAll(".flash_wrap .flash_success, .flash_wrap .flash_info").forEach(function (el) {
    setTimeout(function () { el.style.transition = "opacity .4s"; el.style.opacity = "0"; setTimeout(function () { el.remove(); }, 450); }, 4500);
  });
})();
