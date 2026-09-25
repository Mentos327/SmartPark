// Camera capture for license plates, used by guards on the check-in
// and scan pages.

var cameraStream = null;
var videoElement = null;
var canvasElement = null;
var capturedBlob = null;
var captureCallback = null;

var CameraCapture = {

  init: function (videoId, canvasId, options) {
    if (!options) options = {};
    videoElement = document.getElementById(videoId);
    canvasElement = document.getElementById(canvasId);
    captureCallback = options.onCapture || null;
  },

  // Prefers the rear camera so the guard points the phone at the vehicle
  // rather than the selfie camera. Falls through to looser constraints
  // because laptops usually only have a front webcam and don't support
  // facingMode:"environment" as an exact match — that throws
  // OverconstrainedError instead of just picking whatever's available.
  start: async function () {
    if (!videoElement) return false;

    var constraintAttempts = [
      { video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false },
        //environment means the rear-facing camera on devices that have one.
      { video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false },
      { video: true, audio: false },
    ];

    for (var i = 0; i < constraintAttempts.length; i++) {
      try {
        cameraStream = await navigator.mediaDevices.getUserMedia(constraintAttempts[i]);
        //The camera produces a stream
        //navigator represents information and capabilities provided by the browser.
        //mediaDevices gives JavaScript access to devices that provide media.
        //getUserMedia() this asks the browser for access to the camera and/or microphone.
        videoElement.srcObject = cameraStream;
        //hat puts the live camera stream into the video element on your webpage.
        await videoElement.play();
        return true;
      } catch (err) {
        console.warn("Camera attempt " + (i + 1) + " failed:", err);
      }
    }

    console.error("Camera not available on this device.");
    return false;
  },

  // Stopping all tracks matters — otherwise the browser's recording
  // indicator stays on and other apps can't use the camera.
  stop: function () {
    if (cameraStream) {
      var tracks = cameraStream.getTracks();
      for (var i = 0; i < tracks.length; i++) tracks[i].stop();
      cameraStream = null;
    }
    if (videoElement) videoElement.srcObject = null;
  },

  capture: function () {
    if (!videoElement || !canvasElement) return null;

    canvasElement.width = videoElement.videoWidth;
    canvasElement.height = videoElement.videoHeight;

    var ctx = canvasElement.getContext("2d");
    ctx.drawImage(videoElement, 0, 0, canvasElement.width, canvasElement.height);
    //the canvas is basically an area in the webpage where JavaScript can draw an image.

    // 0.85 = JPEG quality, a good balance of clarity vs. file size
    canvasElement.toBlob(function (blob) {
      capturedBlob = blob;
      if (typeof captureCallback === "function") {
        captureCallback(blob, canvasElement.toDataURL("image/jpeg", 0.85));
      }
    }, "image/jpeg", 0.85);

    return canvasElement.toDataURL("image/jpeg", 0.85);
    //This converts the captured image into a form that can be sent to the server.
  },

  retake: function () {
    capturedBlob = null;
    if (canvasElement) {
      var ctx = canvasElement.getContext("2d");
      ctx.clearRect(0, 0, canvasElement.width, canvasElement.height);
    }
  },

  attachToInput: function (fileInputId) {
    var input = document.getElementById(fileInputId);
    if (!input || !capturedBlob) return false;

    var filename = "capture_" + Date.now() + ".jpg";
    var file = new File([capturedBlob], filename, { type: "image/jpeg" });

    // DataTransfer is the only way to set files on a file input from JS
    var dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;

    return true;
  },

  isSupported: function () {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  },

  getBlob: function () {
    return capturedBlob;
  }

};

// camera.html / camera_scan.html call these directly via onclick=,
// which just delegate to CameraCapture above.
function startCamera() {
  if (!CameraCapture.isSupported()) {
    alert("Camera access is not supported in this browser.");
    return;
  }

  // getUserMedia needs a secure context (HTTPS or localhost) — warn
  // early instead of letting the browser silently reject it
  if (!window.isSecureContext) {
    alert("Camera access requires HTTPS (or localhost). Please open this page over a secure connection.");
    return;
  }

  CameraCapture.init("cameraFeed", "captureCanvas");

  var statusBadge = document.getElementById("cameraStatus");
  if (statusBadge) { statusBadge.textContent = "Requesting..."; statusBadge.className = "badge bg-warning"; }

  CameraCapture.start().then(function (ok) {
    //CameraCapture.start() The browser requests camera access.
    var btnStart = document.getElementById("btnStartCamera");
    var btnStop = document.getElementById("btnStop");
    var btnScan = document.getElementById("btnScan");

    if (ok) {
      if (statusBadge) { statusBadge.textContent = "Live"; statusBadge.className = "badge bg-success"; }
      if (btnStart) { btnStart.disabled = true; }
      if (btnStop) { btnStop.disabled = false; }
      if (btnScan) { btnScan.disabled = false; }
    } else {
      if (statusBadge) { statusBadge.textContent = "Camera Off"; statusBadge.className = "badge bg-danger"; }
      alert("Could not access a camera on this device. Please check that a webcam is connected and that camera permission is allowed for this site, then try again.");
    }
  });
}

function stopCamera() {
  CameraCapture.stop();

  var statusBadge = document.getElementById("cameraStatus");
  var btnStart = document.getElementById("btnStartCamera");
  var btnStop = document.getElementById("btnStop");
  var btnScan = document.getElementById("btnScan");

  if (statusBadge) { statusBadge.textContent = "Camera Off"; statusBadge.className = "badge bg-danger"; }
  if (btnStart) { btnStart.disabled = false; }
  if (btnStop) { btnStop.disabled = true; }
  if (btnScan) { btnScan.disabled = true; }
}

// --------------------------------------------------------------------
// Scan flow for camera_scan.html — captures a frame, sends it to the
// OCR endpoint, then looks up whether the plate is currently parked.
// These were referenced by camera_scan.html's onclick= handlers but
// were never implemented, so the Scan button did nothing.
// --------------------------------------------------------------------

function _showScanState(state) {
  var idle = document.getElementById("scanIdle");
  var spinner = document.getElementById("scanSpinner");
  var result = document.getElementById("scanResult");
  if (idle) idle.classList.add("d-none");
  if (spinner) spinner.classList.add("d-none");
  if (result) result.classList.add("d-none");

  if (state === "idle" && idle) idle.classList.remove("d-none");
  if (state === "loading" && spinner) spinner.classList.remove("d-none");
  if (state === "result" && result) result.classList.remove("d-none");
}

function captureAndScan() {
  if (!videoElement) {
    alert("Start the camera first.");
    return;
  }

  var dataUrl = CameraCapture.capture();
  if (!dataUrl) {
    alert("Could not capture a frame. Make sure the camera is running and try again.");
    return;
  }

  _showScanState("loading");

  fetch("/api/camera/scan-plate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image: dataUrl }),
  })
    .then(function (resp) { return resp.json(); })
    .then(function (data) {
      if (!data.success || !data.plate) {
        _showScanState("idle");
        alert(data.error || "No plate found. Adjust angle and try again.");
        return;
      }
      _renderScanResult(data.plate, data.lookup);
    })
    .catch(function (err) {
      console.error("Scan error:", err);
      _showScanState("idle");
      alert("Scan failed. Please try again.");
    });
}

function _renderScanResult(plate, lookup) {
  var plateEl = document.getElementById("scannedPlate");
  var foundBox = document.getElementById("sessionFound");
  var notFoundBox = document.getElementById("sessionNotFound");
  var durationEl = document.getElementById("sessionDuration");
  var feeEl = document.getElementById("sessionFee");
  var checkoutBtn = document.getElementById("checkoutBtn");
  var checkinBtn = document.getElementById("checkinBtn");

  if (plateEl) plateEl.textContent = plate;

  if (lookup && lookup.found) {
    if (foundBox) foundBox.classList.remove("d-none");
    if (notFoundBox) notFoundBox.classList.add("d-none");
    if (durationEl) durationEl.textContent = "Parked for " + lookup.duration;
    if (feeEl) feeEl.textContent = lookup.current_fee;
    if (checkoutBtn) checkoutBtn.href = lookup.checkout_url;
  } else {
    if (foundBox) foundBox.classList.add("d-none");
    if (notFoundBox) notFoundBox.classList.remove("d-none");
    if (checkinBtn) checkinBtn.href = "/guard/checkin?plate=" + encodeURIComponent(plate);
  }

  _showScanState("result");
}

function resetScan() {
  _showScanState("idle");
}
