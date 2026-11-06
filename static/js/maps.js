(function () {
  "use strict";
  const element = document.getElementById("location_map");
  if (!element) return;
  const mode = element.dataset.mode;
  const warning = document.getElementById("map_warning");
  function warn(message) {
    warning.textContent = message;
    warning.hidden = false;
  }
  if (!window.L) {
    const fallback = {overview: "Откройте список адресов под картой.", edit: "Можно ввести координаты вручную.", detail: "Координаты указаны ниже."};
    warn("Не удалось загрузить карту. " + fallback[mode]);
    return;
  }

  const city = [51.1282, 71.4304];
  const map = L.map(element, {scrollWheelZoom: false, zoomControl: false}).setView(city, 12);
  L.control.zoom({zoomInTitle: "Приблизить", zoomOutTitle: "Отдалить"}).addTo(map);
  L.control.scale({imperial: false}).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map).on("tileerror", () => warn("Не удалось загрузить часть карты улиц."))
    .on("load", function () {
      const visibleTiles = this.getContainer().querySelectorAll("img.leaflet-tile");
      if (visibleTiles.length && Array.from(visibleTiles).every(tile => tile.naturalWidth > 0)) warning.hidden = true;
    });

  function point(lat, lng) {
    if (lat == null || lng == null || lat === "" || lng === "") return null;
    lat = Number(lat);
    lng = Number(lng);
    return Number.isFinite(lat) && Number.isFinite(lng) && Math.abs(lat) <= 90 && Math.abs(lng) <= 180 ? [lat, lng] : null;
  }
  function icon(kind, count) {
    const face = document.createElement("span");
    face.className = "pin-face pin-" + kind;
    face.setAttribute("aria-hidden", "true");
    if (count) face.textContent = String(count);
    return L.divIcon({className: "report-pin", html: face, iconSize: [44, 44], iconAnchor: [22, 35], popupAnchor: [0, -28]});
  }
  function popup(reports) {
    const panel = document.createElement("div");
    panel.className = "map-popup";
    reports.forEach(report => {
      const item = document.createElement("article");
      const link = document.createElement("a");
      link.href = report.url;
      link.textContent = "№ " + report.id + " · " + report.title;
      const address = document.createElement("p");
      address.textContent = report.location;
      const status = document.createElement("strong");
      status.textContent = report.status;
      item.append(link, address, status);
      panel.append(item);
    });
    return panel;
  }

  if (mode === "overview") {
    const locations = new Map();
    for (const report of JSON.parse(document.getElementById("map_pins").textContent)) {
      const position = point(report.latitude, report.longitude);
      if (!position) continue;
      const key = position.join(",");
      if (!locations.has(key)) locations.set(key, {position, reports: []});
      locations.get(key).reports.push(report);
    }
    const bounds = [];
    for (const {position, reports} of locations.values()) {
      const multiple = reports.length > 1;
      const title = multiple ? "Обращений в этой точке: " + reports.length : reports[0].title + " — " + reports[0].status;
      const pin = L.marker(position, {title, icon: multiple ? icon("group", reports.length) : icon(reports[0].status_key)});
      pin.addTo(map).bindPopup(() => popup(reports), {maxHeight: 220});
      pin.getElement().setAttribute("aria-label", title);
      bounds.push(position);
    }
    function resetView() {
      map.closePopup();
      if (bounds.length) map.fitBounds(bounds, {padding: [30, 42], maxZoom: 16});
      else map.setView(city, 12);
    }
    const reset = document.querySelector(".map-tools .map-reset");
    reset.hidden = false;
    reset.addEventListener("click", resetView);
    resetView();
    return;
  }
  if (mode === "detail") {
    const position = point(element.dataset.latitude, element.dataset.longitude);
    if (position) {
      L.marker(position, {icon: icon("place"), interactive: false, keyboard: false}).addTo(map);
      map.setView(position, 16);
    }
    return;
  }

  const latitude = document.getElementById("latitude");
  const longitude = document.getElementById("longitude");
  const feedback = document.getElementById("map_feedback");
  const clear = document.getElementById("clear_location");
  let marker;
  function setFields(position) {
    latitude.value = position ? position.lat.toFixed(6) : "";
    longitude.value = position ? position.wrap().lng.toFixed(6) : "";
    for (const field of [latitude, longitude]) field.dispatchEvent(new Event("input", {bubbles: true}));
  }
  function refresh(recenter) {
    const position = point(latitude.value, longitude.value);
    clear.hidden = !latitude.value && !longitude.value;
    if (position) {
      if (!marker) {
        marker = L.marker(position, {icon: icon("place"), draggable: true, title: "Выбранное место"}).addTo(map);
        marker.on("dragend", () => { setFields(marker.getLatLng()); refresh(false); });
      }
      marker.setLatLng(position);
      if (recenter) map.setView(position, 16);
      feedback.textContent = "Место выбрано. Чтобы уточнить его, перетащите метку или нажмите на другое место.";
    } else {
      if (marker) marker.remove();
      marker = null;
      feedback.textContent = clear.hidden ? "Место ещё не отмечено." : "Проверьте широту и долготу.";
    }
  }
  map.on("click", event => { setFields(event.latlng); refresh(false); });
  for (const field of [latitude, longitude]) field.addEventListener("change", () => refresh(true));
  clear.addEventListener("click", () => { setFields(null); refresh(false); map.getContainer().focus(); });
  refresh(true);
})();
