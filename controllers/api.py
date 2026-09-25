"""
JSON API endpoints.

Groups:
  /api/slots/<location_id>          — live slot data and drag-to-reorder
  /api/sessions/...                 — active session queries for dashboard
  /api/camera/...                   — CCTV stream proxy and plate OCR (EasyOCR)
  /api/mpesa/...                    — M-Pesa STK push, callback, and status polling
"""
from __future__ import annotations

import base64
import hmac
import io
import re
import threading
from datetime import datetime, timedelta, timezone

import requests as http_requests

from auth.decorators import login_required, guard_required, staff_required, admin_required
from core.http import Request, Response
from core.router import Blueprint
from database.db import Database
from models.camera import get_camera_by_id
from models.location import get_location_by_id
from models.session import get_session_by_id, get_active_sessions_by_location, get_active_session_by_plate, complete_session
from models.slot import (get_slots_by_location, get_available_slots as get_available_slots_for_location,
                          count_slots_by_status, update_slot_status, update_slot_order,
                          STATUS_AVAILABLE)
from models.user import get_user_by_id
from utils.fee_calculator import calculate_current_fee, calculate_fee
from utils.plate_validator import is_valid_plate
from utils.mpesa import get_mpesa_credentials
from config import Config
from services.email import send_receipt_email
from core.templating import format_eat_time

api_bp = Blueprint("api")


def err(message):
    """Shorthand for the repeated {"success": False, "error": ...} JSON reply."""
    return Response.json({"success": False, "error": message})
# This helper function creates a standard JSON error response.
# It receives the error message and returns it in a consistent
# format so that the frontend knows the operation was unsuccessful.

# --- Slot data and drag-to-reorder ---

def _slot_to_dict(slot):
    # Convert a database slot row into a normal Python dictionary.
    # This creates a clean structure that can easily be converted
    # to JSON and sent to the frontend.
    return {
        "id": str(slot["id"]),
        "slot_number": slot["slot_number"],
        "level": slot["level"] or "G",
        "type": slot["type"] or "standard",
        "status": slot["status"],
        "sort_order": slot["sort_order"] or 0,
    }


@api_bp.route("/api/slots/<location_id>")
@login_required
def get_slots(request: Request) -> Response:
    """Return all slots and a status summary for a location."""
    # This endpoint returns all parking slots belonging to a
    # particular parking location together with a summary of
    # their current statuses.
    location_id = request.param("location_id")
    slots = get_slots_by_location(location_id)
    summary = count_slots_by_status(location_id)
    return Response.json({"slots": [_slot_to_dict(s) for s in slots], "summary": summary})



@api_bp.route("/api/slots/<location_id>/available")
@login_required
def get_available_slots(request: Request) -> Response:
    """Return only the available slots for a location."""
    location_id = request.param("location_id")
    available = [_slot_to_dict(s) for s in get_available_slots_for_location(location_id)]
    # Get the available slots from the database, convert each slot
    # into a JSON-friendly dictionary, and store the resulting list.
    return Response.json({"slots": available, "count": len(available)})
    # Return the available slots and their total number as JSON.


@api_bp.route("/api/slots/<location_id>/reorder", methods=["POST"])
@login_required
@admin_required
def reorder_slots(request: Request) -> Response:
    """
    Save a new display order for slots.
    Expects JSON body: { "order": ["slot_id_1", "slot_id_2", ...] }
    The position in the list becomes each slot's sort_order value.
    Only admins can reorder slots.
    """
    # This endpoint saves a new display order after an administrator
    # rearranges parking slots using the drag-and-drop interface.
    data = request.get_json()
    # Read the JSON data sent by the frontend.
    if not data or "order" not in data:
        return err("No order provided.")
    # Make sure data was supplied and that it contains the required "order" field.

    order = data["order"]
    # Extract the list containing the slot IDs in their new order.
    for position, slot_id in enumerate(order):
        # enumerate() provides both the position of each slot and its ID.
        # The position becomes the slot's new sort_order value.
        if not slot_id:
            continue
        # update_slot_order() already logs and swallows a bad/unrecognised
        # slot_id itself, so one broken entry can't fail the whole
        # drag-drop payload — it just gets skipped.
        update_slot_order(slot_id, position)

    return Response.json({"success": True})


# --- Live session data (polled by the dashboard) ---

@api_bp.route("/api/sessions/active")
@login_required
@staff_required
def active_sessions(request: Request) -> Response:
    """
    Return all active sessions for the current user's location as JSON.
    Called by the dashboard JavaScript to refresh the vehicle list every
    few seconds, so guards always see an up-to-date list without
    refreshing the whole page.
    """
    # The dashboard JavaScript can call this endpoint repeatedly
    # to refresh the vehicle list without reloading the entire page.
    location = get_location_by_id(request.current_user.location_id)
    hourly_rate = location["hourly_rate"] if location else 30
    # If the location cannot be found, use 30 as a fallback rate.

    sessions = get_active_sessions_by_location(request.current_user.location_id)
    # Retrieve all currently active parking sessions at the user's location.

    result = []
    # Create an empty list that will contain the information sent back to the dashboard.
    for s in sessions:
        fee_info = calculate_current_fee(datetime.fromisoformat(s["entry_time"]), hourly_rate)
        result.append({
            "session_id": str(s["id"]),
            "plate_number": s["plate_number"],
            "entry_time": format_eat_time(s["entry_time"]),
            "duration": fee_info["duration_display"],
            "current_fee": fee_info["fee_kes"],
            "slot_id": s["slot_id"],
        })

    return Response.json({"sessions": result, "count": len(result)})


# --- CCTV stream proxy + OCR plate scanning ---

# OCR reader — loaded once when the first scan request arrives, then reused.
# Loading easyocr takes a few seconds so we do it once and keep it in memory.
# A threading.Lock prevents two concurrent first-requests from both creating
# an EasyOCR instance simultaneously.
_ocr_reader = None
_ocr_reader_lock = threading.Lock()


def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is not None:
        return _ocr_reader
    with _ocr_reader_lock:
        if _ocr_reader is None:
            try:
                import easyocr
            # Load the EasyOCR library only when it is actually needed.
            except ImportError:
                return None
            _ocr_reader = easyocr.Reader(["en"], gpu=False)
            # gpu=False tells EasyOCR to run using the CPU rather than
            # requiring a graphics processing unit.
    return _ocr_reader


def _get_image_bytes(data_url: str) -> bytes:
    """Strip the data-URL prefix and return raw image bytes.
    A browser sends images as data:image/jpeg;base64,/9j/4AAQ... — we only
    need the part after the comma."""
    # This function converts a browser-supplied Base64 image data URL
    # into the raw image bytes needed for image processing.
    if "," in data_url:
        # split(",", 1) separates the string at the first comma.
        # [1] selects the part after that comma.
        data_url = data_url.split(",", 1)[1]
    return base64.b64decode(data_url)
# Decode the Base64 text back into the original binary image bytes.
# These bytes can then be passed to image/OCR processing.


def _extract_plate(raw_text: str):
    """Try to find a valid Kenyan plate in a raw OCR string.
    OCR often reads a plate like "KCZ469Q" or splits it into pieces.
    We clean the text, fix common mistakes (O vs 0, I vs 1), then validate.
    """
    # This function attempts to extract a valid Kenyan vehicle
    # registration number from text produced by OCR.
    text = re.sub(r"[^A-Z0-9]", "", raw_text.upper())
    # Check whether the cleaned text follows the expected Kenyan plate structure
    # ^ means the pattern must start at the beginning of the text.
    # #$ means the pattern must end at the end of the text.

    # Kenyan plate pattern: 2-3 letters, 3 digits, 1 letter e.g. KCZ469Q
    match = re.match(r"^([A-Z0-9]{2,3})([0-9O]{3})([A-Z0-9])$", text)
    # ^ means the pattern must start at the beginning of the text.
    # $ means the pattern must end at the end of the text.
    if not match:
        return None

    prefix = match.group(1).replace("0", "O").replace("1", "I")
    # Get the first captured section of the plate.
    # Correct those common recognition errors in the letter section
    digits = match.group(2).replace("O", "0")
    suffix = match.group(3).replace("0", "O")
    # Get the final character and correct an OCR 0 to the letter O

    candidate = f"{prefix} {digits}{suffix}"
    # Construct the standardized plate string from the cleaned parts.
    return candidate if is_valid_plate(candidate) else None
    # Pass the candidate through the project's final plate validator.
    # Return the candidate only when the validator confirms it is valid


def _find_plate_in_results(ocr_results):
    """Try all combinations of nearby OCR tokens to find a plate.
    Sometimes OCR splits "KCZ 469Q" into ["KCZ", "469Q"] or smaller pieces.
    We try joining 4, 3, 2, then 1 token at a time."""
    # OCR does not always recognize an entire vehicle plate as one
    # piece of text. It may return several nearby text tokens.
    tokens = [result[1] for result in ocr_results]
    # Extract the recognized text from each OCR result.
    # result[1] represents the recognized text portion of each OCR result.

    for group_size in (4, 3, 2):
        # Try combining four, three, and two nearby OCR tokens.
        # This handles plates that have been split into multiple pieces.
        for i in range(len(tokens) - group_size + 1):
            # Move through the token list and select each possible group of the required size.
            joined = "".join(tokens[i:i + group_size])
            # Join the selected OCR tokens together without spaces.
            plate = _extract_plate(joined)
            # Send the combined text to _extract_plate() for cleaning,
            # pattern checking, correction, and final validation.
            if plate:
                return plate

    for token in tokens:
        # If no combination produced a valid plate, check each individual OCR token separately.
        plate = _extract_plate(token)
        # Attempt to extract a valid plate from the individual token.
        if plate:
            # Return immediately when a valid plate is found.
            return plate

    return None


@api_bp.route("/api/camera/stream/<location_id>")
@login_required
def proxy_stream(request: Request):
    """
    Forward a CCTV camera URL to the browser (the camera is usually on a
    private IP that only the server can reach). Old single-camera route,
    kept so locations that never moved to location_cameras still work.
    """
    # This endpoint provides the camera stream associated with a location.
    location_id = request.param("location_id")
    location = get_location_by_id(location_id)

    if not location or not location["camera_url"]:
        # Check that the location exists and that a camera URL has been configured.
        return Response.json({"error": "No camera URL set for this location."}, status=404)

    camera_url = location["camera_url"]
    # Retrieve the configured camera URL.
    return _proxy_camera_url(camera_url)
    # Pass the URL to the camera proxy function, which handles
    # forwarding the camera stream to the browser.


@api_bp.route("/api/camera/feed/<camera_id>")
@staff_required
def proxy_camera_feed(request: Request):
    """
    Same as proxy_stream but for one camera out of a location's set
    (see location_cameras). Only staff at that camera's own location
    can view it, so admins can't just guess another location's camera id.
    """

    camera_id = request.param("camera_id")
    camera = get_camera_by_id(camera_id)

    if not camera:
        return Response.json({"error": "Camera not found."}, status=404)

    user = request.current_user
    # Get information about the currently logged-in user.
    if user.role != "super_admin" and str(user.location_id) != str(camera["location_id"]):
        return Response.json({"error": "Not authorised for this camera."}, status=403)

    return _proxy_camera_url(camera["camera_url"])


def _proxy_camera_url(camera_url: str) -> Response:
    """Shared streaming logic for both camera routes above."""
    #It receives the camera URL, connects to the
    # camera, and forwards the camera stream to the user's browser.
    try:
        # Send an HTTP GET request to the camera.
        # stream=True allows the camera data to be received continuously
        # instead of waiting for the entire stream to finish.
        # timeout=(5, None) allows 5 seconds to establish the connection,
        # while the read timeout is unlimited for the continuous stream.
        upstream = http_requests.get(camera_url, stream=True, timeout=(5, None))
        # Get the content type supplied by the camera.
        # If the camera does not provide one, JPEG is used as the default.
        content_type = upstream.headers.get("Content-Type", "image/jpeg")

        # This generator reads the camera response in small pieces.
        # Reading chunks prevents the entire camera stream from being
        # loaded into memory at once.
        def chunks():
            try:
                for chunk in upstream.iter_content(chunk_size=4096):
                    # Read the camera stream 4096 bytes at a time.
                    if chunk:
                        # Only send non-empty pieces of data.
                        yield chunk
                        # yield sends each piece to the browser as it
                        # becomes available
            except Exception:
                # If the camera disconnects or the stream cannot be read,
                # stop the generator cleanly instead of crashing the request
                pass

        return Response.stream(chunks(), content_type=content_type)
    # Create a streaming response that forwards the camera data
    # to the user's browser using the same content type.

    except http_requests.exceptions.ConnectionError:
        return Response.json({"error": "Cannot reach camera. Check the URL and network."}, status=502)
    # Return HTTP 502 when the application cannot communicate
    # successfully with the camera.
    except http_requests.exceptions.Timeout:
        return Response.json({"error": "Camera did not respond in time."}, status=504)
    # Return HTTP 504 when the camera does not respond within
    # the allowed connection time.
    except Exception as e:
        # Handle any other unexpected error and return HTTP 500.
        return Response.json({"error": f"Stream error: {str(e)}"}, status=500)


# Maximum number of pixels allowed on the longest side of an image
# before it is resized for OCR processing.
# This reduces processing time and memory usage while keeping the image
# detailed enough for number-plate recognition.
_OCR_MAX_DIMENSION = 900


def _prepare_ocr_image(image_bytes: bytes):
    """Decode, greyscale, and downscale a captured frame for faster OCR."""
    from PIL import Image
    # Import Pillow's Image module, which is used to open and manipulate
    # the image received from the browser.

    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    # Convert the raw image bytes into an image object.
    # io.BytesIO allows the bytes to be treated as an in-memory file.
    # convert("L") changes the image to grayscale, which reduces the
    # amount of image information that needs to be processed.

    width, height = img.size
    # Get the image's width and height in pixels.
    longest = max(width, height)
    # Determine which side of the image is longer.
    # This is used to decide whether the image needs to be resized.
    if longest > _OCR_MAX_DIMENSION:
        # Resize the image only when its longest side is larger than
        # the maximum dimension defined above.
        scale = _OCR_MAX_DIMENSION / longest
        # Calculate the scaling factor needed to reduce the longest
        # side to 900 pixels while maintaining the image's proportions.
        img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.BILINEAR)
        # Resize both width and height using the same scaling factor.
        # This keeps the original aspect ratio and prevents distortion.
        # max(1, ...) ensures that the resulting dimensions are never zero.
        # Image.BILINEAR specifies the interpolation method used during resizing.

    return img.convert("RGB")
# Convert the processed image from grayscale back to RGB.
    # EasyOCR expects the image in a compatible RGB format for recognition.


def _lookup_plate_dict(plate: str) -> dict:
    """Shared by scan_plate and lookup_scanned_plate: is this plate parked right now?"""
    # Search the database for an active parking session associated
    # with the supplied number plate.
    session = get_active_session_by_plate(plate)
    # If there is no active session, the vehicle is not currently
    # recorded as being parked in the system.
    if not session:
        return {"found": False, "plate": plate}

    location = get_location_by_id(session["location_id"])
    # Retrieve the parking location associated with the active session.
    hourly_rate = location["hourly_rate"] if location else 30
    # Obtain the hourly parking rate for that location.
    # If the location cannot be found, use 30 as the fallback hourly rate.
    fee_info = calculate_current_fee(datetime.fromisoformat(session["entry_time"]), hourly_rate)
    # Convert the recorded entry time into a datetime object and
    # calculate the vehicle's current parking fee

    return {
        "found": True,
        "plate_number": session["plate_number"],
        "duration": fee_info["duration_display"],
        "current_fee": fee_info["fee_kes"],
        # Create the URL that the guard can use to proceed
        # to the vehicle's checkout page.
        "checkout_url": f"/guard/checkout/{session['id']}",
    }
# Return the information needed by the guard interface.
# found=True confirms that the vehicle currently has an active session.

# Define the API endpoint used when a guard submits a captured
# number-plate image for OCR processing.
# POST is used because the browser is sending image data to the server.
@api_bp.route("/api/camera/scan-plate", methods=["POST"])
@login_required
@guard_required
def scan_plate(request: Request) -> Response:
    """Guard takes a photo and sends it here. We read the plate text, and in
    the same round trip check whether that plate is currently parked — this
    used to be two sequential requests (scan, then lookup); combining them
    cuts a full network round trip off every scan."""
    data = request.get_json()
    # Read the JSON data sent by the guard's browser.
    if not data or not data.get("image"):
        # Check that request data exists and contains an image.
        # Without an image, there is nothing for the OCR system to process.
        return err("No image received.")


    reader = get_ocr_reader()
    # Obtain the application's shared EasyOCR reader.
    # The reader is initialized when needed and can then be reused.
    if reader is None:
        return Response.json({
            "success": False,
            "error": "OCR scanning is not available. Enter the plate manually.",
        })
    # If the OCR reader is unavailable, automatic plate recognition
    # cannot be performed, so the guard is instructed to enter the
    # plate number manually.

    try:
        image_bytes = _get_image_bytes(data["image"])
        # Extract and decode the Base64 image received from the browser
        # into raw image bytes.
        img = _prepare_ocr_image(image_bytes)
        # Prepare the image for OCR by decoding it, converting it to
        # grayscale, resizing it when necessary, and returning it


        import numpy as np
        # NumPy is required to convert the Pillow image into an array
        # format that EasyOCR can process.
        results = reader.readtext(np.array(img), detail=1, decoder="greedy")
        # Run EasyOCR on the processed image.
        # np.array(img):
        # Converts the Pillow image into a NumPy array
        # detail=1: # Requests detailed OCR results, including the detected text
        # and its position in the image.
        # decoder="greedy":
        # Specifies the greedy decoding method for interpreting
        # the recognized characters.


        results = sorted(results, key=lambda r: r[0][0][0])
        # Sort the detected OCR text from left to right.
        # This helps maintain the correct order when the characters
        # of a number plate are detected as separate text regions.
        # r[0] represents the bounding box of a detected text region.
        # r[0][0] represents the top-left point of that bounding box.
        # r[0][0][0] represents the x-coordinate of that point.

        plate = _find_plate_in_results(results)
        # Search the OCR results for text that forms a valid
        # vehicle number plate according to the system's
        # plate-detection and validation rules.

        if not plate:
            return err("No plate found. Adjust angle and try again.")

        return Response.json({"success": True, "plate": plate, "lookup": _lookup_plate_dict(plate)})
    # The lookup is performed in the same request, avoiding the
    # need for a separate request from the browser.

    except Exception as e:
        return err(f"Scan error: {str(e)}")
    # Catch unexpected errors during image decoding, preprocessing,
    # OCR, or plate processing and return a controlled error response.


@api_bp.route("/api/camera/lookup-plate", methods=["POST"])
@login_required
@guard_required
def lookup_scanned_plate(request: Request) -> Response:
    """
    After scanning a plate, check whether that vehicle is currently parked.
    Returns checkout URL if parked, or signals the vehicle is not here yet.

    Kept as a standalone endpoint for manual plate lookups elsewhere; the
    camera scan flow now gets this in the same response as scan-plate.
    """
    data = request.get_json() or {}
    # Read the JSON request.
    # If no JSON data is supplied, use an empty dictionary instead
    # of causing an error when trying to access its values
    plate = (data.get("plate") or "").strip().upper()
    # Retrieve the plate value from the request.
    # If no plate is provided, use an empty string.
    # strip() removes unnecessary spaces.
    # upper() converts the plate to uppercase for consistent processing.

    if not plate:
        return Response.json({"found": False, "error": "No plate provided."})
    # Reject the request when no plate number was provided.

    return Response.json(_lookup_plate_dict(plate))
# Perform the actual active-session lookup using the shared
    # function and return the result as JSON.


# --- Fee refresh + M-Pesa STK push (Daraja API) ---

MPESA_CALLBACK_URL = Config.MPESA_CALLBACK_URL
# Load the configured M-Pesa callback URL from the application settings.
MPESA_ENV = Config.MPESA_ENV
# Load the configured M-Pesa environment.
# This determines whether the system uses the live or sandbox Daraja API.
_SIMULATION_FALLBACK = Config.MPESA_SIMULATION
# Load the configuration that determines whether the system
# can use a simulation fallback when real M-Pesa processing is unavailable
MPESA_CALLBACK_SECRET = Config.MPESA_CALLBACK_SECRET
# Load the secret used to help verify incoming M-Pesa callbacks.

# Safaricom's published callback IP ranges (sandbox + production).
# Store the IP addresses that the application recognizes as
# Safaricom callback addresses.
# A set is used because it is designed for storing unique values
# and allows efficient membership checking.
_SAFARICOM_IPS = {
    "196.201.214.200", "196.201.214.206", "196.201.213.114",
    "196.201.214.207", "196.201.214.208", "196.201.213.44",
    "196.201.212.127", "196.201.212.138", "196.201.212.129",
    "196.201.212.136", "196.201.212.74", "196.201.212.69",
}

DARAJA_BASE = (
    "https://api.safaricom.co.ke" if MPESA_ENV == "live" else "https://sandbox.safaricom.co.ke"
)
# Select the correct Safaricom Daraja base URL according to
# the configured environment.
# "live" uses the production Daraja API.
# Any other configured environment uses the sandbox API.

def _verify_mpesa_callback(request: Request) -> bool:
    """
    Return True only if this request looks like it came from Safaricom.
    Two-layer check:
      1. If MPESA_CALLBACK_SECRET is set, the ?secret= query param must match.
      2. In production mode, the caller's IP must be in Safaricom's known range.
    Either failure -> reject.
    """
    # Only perform the secret check when a callback secret
    # has actually been configured.
    if MPESA_CALLBACK_SECRET:
        provided = request.args.get("secret", "")
        # Read the "secret" value supplied in the callback URL.
        # If it is missing, use an empty string.
        if not hmac.compare_digest(provided, MPESA_CALLBACK_SECRET):
            return False
        # Securely compare the supplied secret with the configured secret.
        # compare_digest is designed for safer secret comparisons.
        # If they do not match, reject the callback.

    if MPESA_ENV == "live":
        # Perform the IP address check only when using the live
        # production M-Pesa environment.
        caller_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        # Obtain the original caller IP from X-Forwarded-For when available.
        # If it is unavailable, use the direct remote address.
        caller_ip = caller_ip.split(",")[0].strip()
        # X-Forwarded-For can contain multiple IP addresses.
        # The first address is selected and surrounding spaces are removed.
        if caller_ip not in _SAFARICOM_IPS:
            return False
        # Reject the callback if the caller's IP is not in
        # the application's allowed Safaricom IP set.

    return True


def _get_mpesa_credentials(location):
    return get_mpesa_credentials(location)
# Retrieve the M-Pesa credentials associated with the specified location.
 # Keeping this in a helper function allows the rest of the payment
# code to use one consistent method for obtaining credentials.


def _all_credentials_filled(creds) -> bool:
    return all([creds["consumer_key"], creds["consumer_secret"], creds["shortcode"], creds["passkey"]])
# Check that all four required M-Pesa credentials contain values:
# consumer key, consumer secret, shortcode, and passkey.
# all() returns True only when every item in the list is true

def _get_mpesa_access_token(creds) -> str:
    encoded = base64.b64encode(f"{creds['consumer_key']}:{creds['consumer_secret']}".encode()).decode()
    # Combine the consumer key and consumer secret in the format
    # required for Daraja Basic Authentication.
    # encode() converts the text into bytes because Base64 encoding
    # operates on bytes.
    response = http_requests.get(
        f"{DARAJA_BASE}/oauth/v1/generate?grant_type=client_credentials",
        headers={"Authorization": f"Basic {encoded}"},
        timeout=10,
    )
    # Send a GET request to the Daraja OAuth endpoint to obtain an access token.
    # The Authorization header contains the Base64-encoded credentials.
    # timeout=10 prevents the application from waiting indefinitely
    # for Safaricom's response.
    response.raise_for_status()
    # Raise an exception if Safaricom responds with an unsuccessful
    # HTTP status code.
    return response.json()["access_token"]
    # Convert the response into JSON and retrieve the access token.
    # The token is required for authenticated Daraja API requests


def _build_stk_password(shortcode, passkey, timestamp) -> str:
    raw = f"{shortcode}{passkey}{timestamp}"
    # Combine the shortcode, M-Pesa passkey, and timestamp
    # in the order required by the Daraja STK Push API.
    return base64.b64encode(raw.encode()).decode()
    # Encode the combined value using Base64 and return it as text.
    # This produces the password value required for the STK Push request.


def _normalise_phone(phone: str):
    """Accepts 07XXXXXXXX, 01XXXXXXXX, +254XXXXXXXXX, 254XXXXXXXXX."""
    phone = phone.replace(" ", "").replace("-", "")
    # Remove spaces and hyphens so that formatting does not interfere
    # with phone-number validation.
    if phone.startswith("+"):
        phone = phone[1:]
    # Remove the "+" when the number is supplied in international format.
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    # Convert a Kenyan number beginning with 0 into international
    # 254 format by replacing the leading zero with 254.
    if phone.startswith("254") and len(phone) == 12 and phone.isdigit():
        return phone
    # Accept the number only if:
    # - it begins with 254,
    # - it contains exactly 12 characters,
    # - and every character is a digit.
    return None
    # Return None when the phone number does not satisfy the
    # required format.

# API endpoint used by the checkout page to retrieve the
# current parking fee for a particular parking session.
@api_bp.route("/api/payments/fee/<session_id>")
@login_required
def get_fee(request: Request) -> Response:
    """Polled by the checkout page JS every 60 seconds to keep the running total current."""
    session_id = request.param("session_id")
    # Retrieve the parking session ID from the URL.
    session = get_session_by_id(session_id)
    # Search the database for the corresponding parking session.
    if not session:
        return Response.json({"error": "Session not found"}, status=404) \
         # If the session does not exist, return HTTP 404
        # because the requested resource cannot be found.

    # If the M-Pesa callback already completed this session while the page
    # was open, return the confirmed amount rather than recalculating.
    if session["status"] == "completed" and session["fee_kes"] is not None:
        # Return the stored confirmed fee instead of calculating
        # a new running fee for an already completed session.
        return Response.json({
            "fee_kes": session["fee_kes"],
            "duration_display": "Paid",
            "per_minute_rate": 0,
            "hourly_rate": 0,
            "completed": True,
        })

    location = get_location_by_id(session["location_id"])
    hourly_rate = location["hourly_rate"] if location else 30
    fee_info = calculate_current_fee(datetime.fromisoformat(session["entry_time"]), hourly_rate)
    # Convert the stored entry-time string into a datetime object
    # and calculate the current parking fee using the location's rate.

    return Response.json({
        "fee_kes": fee_info["fee_kes"],
        "duration_display": fee_info["duration_display"],
        "per_minute_rate": fee_info["per_minute_rate"],
        "hourly_rate": hourly_rate,
    })
# Return the current fee information as JSON so the checkout
    # page can update the displayed parking charges.


@api_bp.route("/api/payments/mpesa/push", methods=["POST"])
@login_required
@guard_required
def mpesa_push(request: Request) -> Response:
    """POST body: { "session_id": "...", "phone": "0712345678", "amount": 120 }"""
    data = request.get_json(silent=True) or {}
    # Read the JSON data sent by the guard's browser.
    # silent=True prevents an exception when the request does not
    # contain valid JSON. If no data is received, use an empty dictionary.
    session_id = (data.get("session_id") or "").strip()
    # Get the parking session ID from the request.
    # If it is missing, use an empty string and remove surrounding whitespace.
    phone = (data.get("phone") or "").strip()
    # Get the customer's phone number and remove surrounding whitespace.

    if not session_id:
        return err("Session ID is required.")
    # A session ID is required because the payment must be associated
    # with a specific active parking session.

    try:
        # Convert the supplied payment amount to a floating-point number
        # first and then to an integer because M-Pesa expects a whole-number
        # amount.
        amount = int(float(data.get("amount", 0)))
    except (ValueError, TypeError):
        return err("Amount must be a number.")
    # Handle cases where the amount is missing, invalid, or cannot
    # be converted into a number.

    if not phone:
        return err("Phone number is required.")
    # A phone number is required because Safaricom will send
    # the STK Push prompt to this number.
    if amount <= 0:
        return err("Amount must be greater than zero.")
    # The payment amount must be greater than zero.

    phone = _normalise_phone(phone)
    # Convert the supplied phone number into the standard Kenyan
    # 254XXXXXXXXX format required by the M-Pesa API.
    if not phone:
        return err("Enter a valid Kenyan number e.g. 0712 345 678")
    # If the phone number does not match the accepted format,
    # stop the payment request and ask for a valid number.

    session = get_session_by_id(session_id)
    # Retrieve the parking session from the database.
    # This confirms that the supplied session ID actually exists.
    if not session:
        return err("Session not found.")

    location = get_location_by_id(session["location_id"])
    # Retrieve the parking location associated with this session.
    # The location determines which M-Pesa configuration and
    # parking rate belong to the session.
    if not location:
        return err("Location not found.")
    # Stop if the session refers to a location that does not exist.

    creds = _get_mpesa_credentials(location)
    # Retrieve the M-Pesa credentials configured for this parking location.


    display_phone = "0" + phone[3:6] + " " + phone[6:9] + " " + phone[9:]
    # Convert the normalized 254-format phone number into a more
    # readable format for messages displayed to the guard.
    # The actual M-Pesa request continues using the 254-format number.
    # This formatting is only for display purposes.

    if not _all_credentials_filled(creds):
        # Check whether all required M-Pesa credentials have been configured.
        if _SIMULATION_FALLBACK:
            # If real credentials are missing but simulation mode is enabled,
            # return a successful simulated response instead of contacting

            return Response.json({
                "success": True,
                "simulated": True,
                "message": (
                    f"[SIMULATION] M-Pesa push sent to {display_phone}. "
                    f"Customer would pay KES {amount}. "
                    "Configure real M-Pesa credentials via Admin -> Pricing to enable live payments."
                ),
                # Inform the guard that this is only a simulation.
                # No real M-Pesa transaction is being performed here.
                "phone": display_phone,
                "checkout_request_id": "SIMULATED",
                # This fixed value indicates that no real Safaricom
                # CheckoutRequestID was generated.
            })
        # If simulation is disabled and real credentials are missing,
        # return an error explaining how the administrator can configure
        # M-Pesa for the location.
        return Response.json({
            "success": False,
            "error": (
                "M-Pesa credentials are not configured for this location. "
                "Go to Admin -> Pricing to add them, or set MPESA_SIMULATION=true "
                "in .env to enable simulation fallback."
            ),
        })

    try:
        token = _get_mpesa_access_token(creds)
        # Authenticate with Safaricom's Daraja API and obtain an
        # access token that will be used for the STK Push request.
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        # Generate the current UTC timestamp in the format required
        # by the Daraja API for the STK Push request.
        password = _build_stk_password(creds["shortcode"], creds["passkey"], timestamp)
        # Generate the STK Push password using the business shortcode,
        # M-Pesa passkey, and timestamp.

        transaction_type = (
            "CustomerPayBillOnline" if creds["account_type"] == "paybill"
            else "CustomerBuyGoodsOnline"
        )
        # Select the transaction type based on how the parking location
        # receives M-Pesa payments.
        # PayBill accounts use CustomerPayBillOnline.
        # Till/Buy Goods accounts use CustomerBuyGoodsOnline.

        payload = {
            "BusinessShortCode": creds["shortcode"],
            # The business shortcode belonging to the parking location.
            "Password": password,
            # Password generated from the shortcode, passkey, and timestamp.
            "Timestamp": timestamp,
            # Timestamp used when generating the STK password.
            "TransactionType": transaction_type,
            # Specifies whether the payment is processed as
            # PayBill or Buy Goods.
            "Amount": amount,
            # The amount the customer is being asked to pay.
            "PartyA": phone,
            # The customer's phone number that receives the STK prompt.
            "PartyB": creds["shortcode"],
            # The business shortcode or till receiving the payment.
            "PhoneNumber": phone,
            # Phone number to which Safaricom sends the payment prompt.
            "CallBackURL": MPESA_CALLBACK_URL,
            # URL where Safaricom will send the final payment result.
            "AccountReference": session["receipt_number"],
            # Use the parking session's receipt number as the
            # payment account reference so the transaction can be
            # associated with the correct parking session.
            "TransactionDesc": f"Parking {session['plate_number']}",
            # Description identifying the transaction as a parking payment
            # and including the vehicle's plate number.
        }

        response = http_requests.post(
            # Send the STK Push request to Safaricom's Daraja API
            f"{DARAJA_BASE}/mpesa/stkpush/v1/processrequest",
            # Select the correct live or sandbox API endpoint.
            headers={"Authorization": f"Bearer {token}"},
            # Send the previously obtained OAuth access token.
            # Bearer authentication tells Safaricom that this request
            # has been authenticated.
            json=payload,
            # Send the payment information as JSON.
            timeout=15,
            # Stop waiting if Safaricom does not respond within 15 seconds.
        )
        result = response.json()
        # Convert Safaricom's JSON response into a Python dictionary
        # so that its values can be checked.

        if result.get("ResponseCode") == "0":
            # Safaricom uses ResponseCode "0" to indicate that the
            # STK Push request was accepted successfully.
            checkout_request_id = result.get("CheckoutRequestID")
            # Retrieve Safaricom's unique CheckoutRequestID.
            # This ID is important because it identifies this particular
            # STK Push transaction and is later used to match the callback
            # to the correct parking session.
            Database.execute(
                "UPDATE sessions SET mpesa_checkout_request_id = ? WHERE id = ?",
                (checkout_request_id, session["id"]),
            )
            # Store the CheckoutRequestID in the parking session.
            # When Safaricom later sends the callback, the system uses
            # this value to find the correct active parking session.
            return Response.json({
                "success": True,
                "simulated": False,
                "message": f"M-Pesa prompt sent to {display_phone}. Customer must enter their PIN to pay KES {amount}.",
                # Tell the guard that the customer must interact with
                # the M-Pesa prompt and enter their PIN.
                "phone": display_phone,
                "checkout_request_id": checkout_request_id,
                # Return the unique transaction identifier to the frontend.
            })
        # Inform the browser that the STK Push was successfully sent.
        else:
            return Response.json({
                "success": False,
                "error": result.get("errorMessage", "M-Pesa request failed. Try again."),
            })
        # If Safaricom did not accept the STK Push request,
        # return the error supplied by the API.
        # If Safaricom did not provide an error message, use
        # the application's default message.

    except http_requests.exceptions.Timeout:
        return err("Safaricom did not respond in time. Try again.")
    # Handle a situation where Safaricom does not respond
    # within the configured timeout period.
    except Exception as e:
        return err(f"M-Pesa error: {str(e)}")
    # Handle any other unexpected payment-processing error
    # and return a controlled error response.

# Define the endpoint that receives the final M-Pesa payment
# notification from Safaricom.\
# POST is used because Safaricom sends payment information
# to this endpoint in the request body.
@api_bp.route("/api/payments/mpesa/callback", methods=["POST"])
def mpesa_callback(request: Request) -> Response:
    """
    Safaricom POSTs here after the customer enters their PIN.
    No @login_required — this is called directly by Safaricom's servers.
    Always return 200 or Safaricom will keep retrying.
    """
    if not _verify_mpesa_callback(request):
        # Verify the callback using the application's configured
        # M-Pesa security checks.
        print(
            "[mpesa_callback] REJECTED: failed IP/secret verification from "
            f"{request.headers.get('X-Forwarded-For', request.remote_addr)}"
        )
        # Log the rejected request for server-side monitoring/debugging.
        return Response.json({"ResultCode": 0, "ResultDesc": "Accepted"})
    # Return an accepted response to the caller.
    # The code intentionally returns HTTP success so that the
    # external callback mechanism does not repeatedly retry the request.

    data = request.get_json(silent=True) or {}
    # Read the JSON callback received from Safaricom.
    # If no valid JSON body exists, use an empty dictionary.
    callback_body = data.get("Body", {}).get("stkCallback", {})
    # Navigate through Safaricom's callback structure to obtain
    # the STK callback information.

    try:
        result_code = int(callback_body.get("ResultCode", -1))
        # Retrieve Safaricom's ResultCode and convert it to an integer.
        # ResultCode 0 means the M-Pesa transaction was successful.
    except (ValueError, TypeError):
        result_code = -1
    # If the supplied result code cannot be converted into an integer,
    # treat it as an invalid/failed result.
    if result_code == 0:
        items = callback_body.get("CallbackMetadata", {}).get("Item", [])
        # Retrieve the list of payment metadata items from the callback.
        meta = {item["Name"]: item.get("Value") for item in items}
        # Convert the list of metadata objects into a dictionary.
        # The payment field name becomes the key and its value becomes
        # the corresponding dictionary value.
        mpesa_code = meta.get("MpesaReceiptNumber")
        # Extract the unique M-Pesa receipt/transaction number.
        amount = meta.get("Amount", 0)
        # Extract the amount actually reported by Safaricom.
        phone = str(meta.get("PhoneNumber", ""))
        # Extract the phone number that made the payment.
        # Convert it to a string so it can be stored consistently.

        checkout_request_id = callback_body.get("CheckoutRequestID", "")
        # Retrieve the CheckoutRequestID generated when the STK Push
        # was originally sent.
        if not checkout_request_id:
            return Response.json({"ResultCode": 0, "ResultDesc": "Accepted"})
        # If the callback does not contain a CheckoutRequestID,
        # there is no reliable way to associate the payment with
        # a parking session.

        session = Database.fetch_one(
            "SELECT * FROM sessions WHERE mpesa_checkout_request_id = ? AND status = 'active'",
            (checkout_request_id,),
        )
        # Search for an active parking session whose stored
        # CheckoutRequestID matches the one in Safaricom's callback.
        # The status='active' condition ensures that only an
        # unfinished parking session is processed.

        # Continue only when a matching active session was found.
        if session:

            completed = complete_session(session_id=session["id"], fee_kes=float(amount), payment_method="mpesa")
            # Complete the parking session using the amount confirmed
            # by Safaricom and record M-Pesa as the payment method.
            # complete_session also records the session's exit time.
            # It only completes an active session, which helps prevent
            # the same callback from completing the session twice.

        if session and completed:
            # Continue with post-payment processing only when the session
            # existed and was successfully completed.
            if session["slot_id"]:
                update_slot_status(session["slot_id"], STATUS_AVAILABLE)
            # If the session was occupying a parking slot,
            # make that slot available again.

            # Store the M-Pesa transaction code and payer's phone number
            # in the parking session.
            # This allows the payment information to be available
            # when the receipt is viewed or generated.
            Database.execute(
                "UPDATE sessions SET mpesa_code = ?, payer_phone = ? WHERE id = ?",
                (mpesa_code, phone, session["id"]),
            )

            # Retrieve the session again after completing it.
            # This gives us the updated database values, including
            # the exit_time written by complete_session().
            updated_session = get_session_by_id(session["id"])

            if updated_session and updated_session["driver_id"] and not updated_session["receipt_email_sent"]:
                # Only send a driver receipt when:# 1. the updated session exists,
                # 2. a driver is linked to the session, and # 3. a receipt has not already been sent.
                # The receipt_email_sent check prevents the same receipt # from being sent repeatedly.
                driver = get_user_by_id(updated_session["driver_id"])
                # Retrieve the driver's account information.
                location = get_location_by_id(updated_session["location_id"])
                # Retrieve the parking location information.

                if driver and driver["email"] and location:
                    # Continue only when the driver has an email address
                    # and the parking location exists.
                    exit_time = (
                        datetime.fromisoformat(updated_session["exit_time"])
                        if updated_session["exit_time"] else datetime.now(timezone.utc)
                    )
                    # Convert the stored exit time into a datetime object.
                    # If no exit time exists, use the current UTC time
                    # as a fallback.
                    entry_time = datetime.fromisoformat(updated_session["entry_time"])
                    # Convert the stored entry time into a datetime object
                    # so that the parking duration can be calculated.
                    entry_eat = entry_time + timedelta(hours=3)
                    # Convert UTC time to East Africa Time by adding
                    # three hours for the receipt display.
                    exit_eat = exit_time + timedelta(hours=3)

                    dur = calculate_fee(entry_time, exit_time, location["hourly_rate"] or 30)
                    # Calculate the final parking duration and fee
                    # using the recorded entry and exit times and
                    # the location's hourly rate.

                    sent = send_receipt_email(
                        # Send the completed parking receipt to the driver.
                        driver_email=driver["email"],
                        plate=updated_session["plate_number"],
                        location_name=location["name"],
                        entry_time=entry_eat.strftime("%d %b %Y, %H:%M") + " EAT",
                        # Format the entry time for human-readable display
                        # and identify the timezone as EAT.
                        exit_time=exit_eat.strftime("%d %b %Y, %H:%M") + " EAT",
                        duration=dur["duration_display"],
                        fee_kes=float(amount),
                        # Include the amount confirmed by Safaricom.
                        payment_method=f"M-Pesa ({mpesa_code})",
                        # Identify both the payment method and
                        # the M-Pesa transaction code.
                    )

                    if sent:
                        # Only mark the receipt as sent when the
                        # email function reports successful delivery.
                        Database.execute(
                            "UPDATE sessions SET receipt_email_sent = 1 WHERE id = ?",
                            (session["id"],),
                        )
                        # Store 1 in the database to indicate that
                        # the receipt email has already been sent.
                        # This prevents duplicate receipt emails
                        # if the callback is processed again.

    return Response.json({"ResultCode": 0, "ResultDesc": "Accepted"})
    # Tell Safaricom that the callback was received and accepted.
    # Returning this response is important because Safaricom expects
    # a successful response from the callback endpoint.
