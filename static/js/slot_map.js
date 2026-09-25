// Draws the clickable parking slot grid used by guards and drivers.

var selectedSlot = null;
var onSelectCb = null;

var SlotMap = {

  // containerId: id of the target <div>. slots: array from the server.
  // options.onSelect(slot) fires on selection change; options.inputId
  // is a hidden form field to keep in sync with the current pick.
  init: function (containerId, slots, options) {
    if (!options) options = {};

    var container = document.getElementById(containerId);
    if (!container) return;

    onSelectCb = options.onSelect || null;

    renderMap(container, slots, options);
    storeStats(container, slots);
  },

  getSelected: function () {
    return selectedSlot;
  }

};

function renderMap(container, slots, options) {
  container.innerHTML = "";

  container.appendChild(buildStatsBar(slots));

  var groups = groupByAisle(slots);
  var aisleNames = Object.keys(groups);

  for (var i = 0; i < aisleNames.length; i++) {
    var aisleName = aisleNames[i];
    var group = groups[aisleName];

    if (aisleName) {
      var label = document.createElement("div");
      label.className = "aisle-label";
      label.textContent = "Aisle " + aisleName;
      container.appendChild(label);
    }

    var row = document.createElement("div");
    row.className = "slot-row";

    for (var j = 0; j < group.length; j++) {
      row.appendChild(buildSlotCell(group[j], options));
    }

    container.appendChild(row);
  }

  container.appendChild(buildLegend());
}

function buildSlotCell(slot, options) {
  var cell = document.createElement("div");
  var status = slot.status ? slot.status.toLowerCase() : "available";

  cell.className = "slot-cell " + status;
  cell.dataset.slotId = slot.id;
  cell.dataset.status = status;

  var num = document.createElement("span");
  num.className = "slot-num";
  num.textContent = slot.label || slot.id;

  var icon = document.createElement("span");
  icon.className = "slot-icon";
  icon.innerHTML = getIconForStatus(status);

  cell.appendChild(num);
  cell.appendChild(icon);

  var tip = document.createElement("span");
  tip.className = "slot-tooltip";
  tip.textContent = getTooltipText(slot);
  cell.appendChild(tip);

  // occupied/reserved slots are view-only
  if (status === "available") {
    cell.addEventListener("click", function () {
      handleSlotClick(cell, slot, options);
    });
  }

  return cell;
}

function handleSlotClick(cell, slot, options) {
  if (selectedSlot) {
    var prevCell = document.querySelector("[data-slot-id='" + selectedSlot + "']");
    if (prevCell) prevCell.classList.remove("selected");
  }

  if (selectedSlot === slot.id) {
    selectedSlot = null;
    if (typeof onSelectCb === "function") onSelectCb(null);
    return;
  }

  selectedSlot = slot.id;
  cell.classList.add("selected");

  if (options && options.inputId) {
    var input = document.getElementById(options.inputId);
    if (input) input.value = slot.id;
  }

  if (typeof onSelectCb === "function") onSelectCb(slot);
}

function buildStatsBar(slots) {
  var counts = { available: 0, occupied: 0, reserved: 0 };

  for (var i = 0; i < slots.length; i++) {
    var s = slots[i].status ? slots[i].status.toLowerCase() : "available";
    if (counts[s] !== undefined) counts[s] = counts[s] + 1;
  }

  var bar = document.createElement("div");
  bar.className = "slot-stats-bar";

  var statuses = ["available", "occupied", "reserved"];
  for (var j = 0; j < statuses.length; j++) {
    var status = statuses[j];
    var item = document.createElement("div");
    item.className = "slot-stat " + status;

    var countSpan = document.createElement("span");
    countSpan.className = "count";
    countSpan.textContent = counts[status];

    var labelSpan = document.createElement("span");
    labelSpan.textContent = status.charAt(0).toUpperCase() + status.slice(1);

    item.appendChild(countSpan);
    item.appendChild(labelSpan);
    bar.appendChild(item);
  }

  return bar;
}

function buildLegend() {
  var legend = document.createElement("div");
  legend.className = "slot-map-legend";

  var items = [
    { key: "available", label: "Available" },
    { key: "occupied", label: "Occupied" },
    { key: "reserved", label: "Reserved" },
    { key: "disabled", label: "Disabled" },
  ];

  for (var i = 0; i < items.length; i++) {
    var item = document.createElement("div");
    item.className = "legend-item";
    item.innerHTML = '<span class="legend-swatch ' + items[i].key + '"></span>' + items[i].label;
    legend.appendChild(item);
  }

  return legend;
}

function groupByAisle(slots) {
  var groups = {};

  for (var i = 0; i < slots.length; i++) {
    var aisle = slots[i].aisle || "";
    if (!groups[aisle]) groups[aisle] = [];
    groups[aisle].push(slots[i]);
  }

  return groups;
}

function storeStats(container, slots) {
  var counts = { available: 0, occupied: 0, reserved: 0, total: slots.length };

  for (var i = 0; i < slots.length; i++) {
    var s = slots[i].status ? slots[i].status.toLowerCase() : "available";
    if (counts[s] !== undefined) counts[s] = counts[s] + 1;
  }

  container.dataset.stats = JSON.stringify(counts);
}

function getIconForStatus(status) {
  if (status === "available") return '<i class="icon icon-check-circle"></i>';
  if (status === "occupied") return '<i class="icon icon-car-front"></i>';
  if (status === "reserved") return '<i class="icon icon-clock-history"></i>';
  if (status === "disabled") return '<i class="icon icon-exclamation-circle"></i>';
  return "";
}

function getTooltipText(slot) {
  var status = slot.status ? slot.status.toLowerCase() : "available";
  var label = slot.label || slot.id;

  if (status === "available") return "Slot " + label + " — Click to select";
  if (status === "occupied") return "Slot " + label + " — Occupied" + (slot.plate ? " (" + slot.plate + ")" : "");
  if (status === "reserved") return "Slot " + label + " — Reserved";
  return "Slot " + label + " — Disabled";
}
