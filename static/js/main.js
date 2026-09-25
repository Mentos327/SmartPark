// Global UI helpers shared across every page: sidebar toggle and flash
// message dismissal. Plain JS, no jQuery.

// Sidebar toggle (mobile)
var sidebar = document.getElementById("sidebar");
var overlay = document.querySelector(".sidebar-overlay");
var toggleBtns = document.querySelectorAll(".sidebar-toggle");

function openSidebar() {
  if (sidebar) sidebar.classList.add("open");
  if (overlay) overlay.classList.add("active");
  document.body.style.overflow = "hidden";
}

function closeSidebar() {
  if (sidebar) sidebar.classList.remove("open");
  if (overlay) overlay.classList.remove("active");
  document.body.style.overflow = "";
}

for (var i = 0; i < toggleBtns.length; i++) {
  toggleBtns[i].addEventListener("click", function () {
    if (sidebar && sidebar.classList.contains("open")) {
      closeSidebar();
    } else {
      openSidebar();
    }
  });
}

if (overlay) {
  overlay.addEventListener("click", closeSidebar);
}

window.addEventListener("resize", function () {
  if (window.innerWidth >= 992) closeSidebar();
});

// Flash messages: dismissible with the × button, and .alert-auto ones
// fade out on their own after 5s.
// Shared by templates that wire up their own eye-icon button via
// onclick="togglePwd('fieldId','iconId')" (login, register, profile
// password-change forms). Kept global so multiple templates don't each
// need to define their own copy.
function togglePwd(fieldId, iconId) {
  var field = document.getElementById(fieldId);
  var icon = document.getElementById(iconId);
  if (!field) return;
  field.type = field.type === "password" ? "text" : "password";
  if (icon) icon.className = field.type === "password" ? "icon icon-eye" : "icon icon-eye";
}

// PASSWORD / SECRET SHOW-HIDE TOGGLE
// Runs on every page (login, registration, profile forms, M-Pesa
// credential fields, etc.) and makes sure every input[type=password]
// has an eye-icon button to reveal/hide its value — even ones added
// later that don't have a manual togglePwd()/togglePassword() wired
// up in their own template.

function initPasswordToggle(field) {
  if (!field || field.dataset.toggleAttached === "1") return;

  // Already has a manual toggle button (e.g. togglePwd()/togglePassword()
  // wired up inline in the template) — don't add a second one.
  var existingGroup = field.closest(".input-group");
  if (existingGroup && existingGroup.querySelector(
    "button[data-pwd-toggle], button[onclick*='ogglePwd'], button[onclick*='ogglePassword']"
  )) {
    field.dataset.toggleAttached = "1";
    return;
  }

  var btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-outline-secondary";
  btn.setAttribute("data-pwd-toggle", "1");
  btn.setAttribute("tabindex", "-1");
  btn.setAttribute("aria-label", "Show or hide value");
  btn.innerHTML = '<i class="icon icon-eye"></i>';

  btn.addEventListener("click", function () {
    var icon = btn.querySelector("i");
    if (field.type === "password") {
      field.type = "text";
      icon.className = "icon icon-eye";
    } else {
      field.type = "password";
      icon.className = "icon icon-eye";
    }
  });

  if (existingGroup) {
    // Field is already inside an .input-group — just add the button to it.
    existingGroup.appendChild(btn);
  } else {
    // Wrap the bare field in a new .input-group so the button sits flush
    // against it, matching the styling used elsewhere in the app.
    var wrapper = document.createElement("div");
    wrapper.className = "input-group";
    field.parentNode.insertBefore(wrapper, field);
    wrapper.appendChild(field);
    wrapper.appendChild(btn);
  }

  field.dataset.toggleAttached = "1";
}

var _pwFields = document.querySelectorAll('input[type="password"]');
for (var _p = 0; _p < _pwFields.length; _p++) {
  initPasswordToggle(_pwFields[_p]);
}

var alerts = document.querySelectorAll(".alert-dismissible");

for (var a = 0; a < alerts.length; a++) {
  (function (alert) {
    var closeBtn = alert.querySelector(".alert-dismiss-btn");
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        alert.remove();
      });
    }

    if (alert.classList.contains("alert-auto")) {
      setTimeout(function () {
        alert.style.transition = "opacity 0.4s";
        alert.style.opacity = "0";
        setTimeout(function () { alert.remove(); }, 420);
      }, 5000);
    }
  })(alerts[a]);
}
