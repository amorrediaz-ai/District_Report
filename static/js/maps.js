(function () {
  "use strict";
  var element = document.getElementById("location_map");
  if (!element) return;
  var feedback = document.getElementById("map_feedback"), mode = element.dataset.mode;
  if (!window.L) {
    feedback.textContent += " Карта не загрузилась. Используйте координаты или список обращений ниже.";
    return;
  }
  var map = L.map(element, {scrollWheelZoom: false}).setView([51.1282, 71.4304], 12);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map).on("tileerror", function () {
    feedback.textContent = "Не удалось загрузить фон карты. Координаты и ссылки на обращения остаются доступны.";
  });
  function point(lat, lng) {
    if (lat === "" || lng === "") return null;
    lat = Number(lat); lng = Number(lng);
    return Number.isFinite(lat) && Number.isFinite(lng) && Math.abs(lat) <= 90 && Math.abs(lng) <= 180 ? [lat, lng] : null;
  }
  function dot(position, status) {
    var colors = {"На рассмотрении": "#9b6a14", "В работе": "#256ca0", "Решено": "#35734b", "Отклонено": "#ad4141"};
    return L.circleMarker(position, {radius: 9, weight: 3, color: "#fff", fillColor: colors[status] || "#35734b", fillOpacity: 1}).addTo(map);
  }
  if (mode === "edit") {
    var latitude = document.getElementById("latitude"), longitude = document.getElementById("longitude"), marker;
    var clear = document.getElementById("clear_location"); clear.hidden = false;
    function refresh(recenter) {
      var position = point(latitude.value, longitude.value);
      if (marker) { map.removeLayer(marker); marker = null; }
      if (position) {
        marker = dot(position);
        if (recenter) map.setView(position, 15);
        feedback.textContent = "Выбрано: " + position[0].toFixed(6) + ", " + position[1].toFixed(6);
      } else { feedback.textContent = "Метка не выбрана. Нажмите на карту или заполните обе координаты."; }
    }
    map.on("click", function (event) {
      latitude.value = event.latlng.lat.toFixed(6);
      longitude.value = (((event.latlng.lng + 180) % 360 + 360) % 360 - 180).toFixed(6);
      latitude.dispatchEvent(new Event("input", {bubbles: true}));
      longitude.dispatchEvent(new Event("input", {bubbles: true}));
      refresh(false);
    });
    [latitude, longitude].forEach(function (field) { field.addEventListener("change", function () { refresh(true); }); });
    clear.addEventListener("click", function () {
      latitude.value = ""; longitude.value = "";
      latitude.dispatchEvent(new Event("input", {bubbles: true})); refresh(false);
    });
    refresh(true);
  } else if (mode === "detail") {
    var position = point(element.dataset.latitude, element.dataset.longitude);
    if (position) { dot(position); map.setView(position, 15); }
  } else {
    var pins = JSON.parse(document.getElementById("map_pins").textContent), bounds = [];
    pins.forEach(function (pin) {
      var position = point(pin.latitude, pin.longitude);
      if (!position) return;
      bounds.push(position);
      // User text is always textContent, never popup HTML.
      var content = document.createElement("div"), link = document.createElement("a"), status = document.createElement("p"), address = document.createElement("p");
      link.href = pin.url; link.textContent = "№ " + pin.id + " · " + pin.title;
      status.textContent = pin.status; address.textContent = pin.location;
      content.append(link, status, address);
      dot(position, pin.status).bindPopup(content);
    });
    if (bounds.length) map.fitBounds(bounds, {padding: [25, 25], maxZoom: 15});
  }
})();
