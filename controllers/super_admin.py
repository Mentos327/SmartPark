"""
Super-admin routes (URL prefix: /super-admin).

Covers the system dashboard, location management, admin account creation,
super-admin profile, and the system-wide audit log. All routes require
@super_admin_required.
"""
from __future__ import annotations

import json

from auth.decorators import super_admin_required
from auth.session_manager import login_user, flash
from core.http import Request, Response
from core.router import Blueprint
from database.db import Database
from models.location import (create_location, get_all_locations, get_location_by_id,
                              update_location, delete_location, KENYA_COUNTIES, LOCATION_TYPES)
from models.audit_log import log_action
from models.slot import count_slots_by_status
from models.user import (create_user, get_all_users_by_role, get_all_users, delete_user,
                          check_password, update_password, User, get_user_by_id,
                          update_admin_profile)

super_admin_bp = Blueprint("super_admin")

_render = None
_router = None


# This function sets up (configures) the tools the module needs:
# - a render function (to show templates)
# - a router (to handle navigation)
def configure(render_func, router):
    global _render, _router
    # Declare that we are modifying the global variables _render and _router
    _render = render_func
    # Assign the provided render function to the global _render variable
    # This allows other parts of the module to call _render without importing directly
    _router = router
    # Save the given router into _router
    # Now other parts of this file can use _router for navigation


def render(request: Request, template_name: str, **context) -> Response:
    html = _render(template_name, request, context)
    # Use the saved _render function to turn a template into HTML
    return Response.html(html)
    # Wrap the HTML inside a Response object so it can be sent back to the browser
#This helper wraps the injected _render function and returns a proper Response object.

def _get_admin_by_id(admin_id):
    """Fetch a user row with role='admin' by id, or None if it doesn't
    exist / isn't an admin / the id isn't a valid integer."""
    try:
        return Database.fetch_one(
            "SELECT * FROM users WHERE id = ? AND role = 'admin'", (int(admin_id),)
        )
    except (TypeError, ValueError):
        return None
#Executes a parameterized SQL query to prevent injection.
#If conversion fails, it catches the exception and returns None.



#Registers this function as the handler for the /dashboard URL within the super admin blueprint.
#Only a Super Admin may execute this function.
@super_admin_bp.route("/dashboard")
@super_admin_required
def dashboard(request: Request) -> Response:
    """Super admin home — overview of all locations."""
    locations = get_all_locations()
    admins = get_all_users_by_role("admin")
    # Start the total parking-slot counter at zero.
    total_slots = 0
    # Go through every parking location.
    for loc in locations:
        # Count the slots belonging to this location by status
        # such as available, occupied, etc.
        loc["slot_summary"] = count_slots_by_status(str(loc["id"]))
        # Stores this dictionary in loc["slot_summary"] and adds the sum of all slot counts to total_slots.
        total_slots += sum(loc["slot_summary"].values())

    # Send the collected information to the dashboard HTML page.
    return render(
        request, "super_admin/dashboard.html",
        locations=locations, admins=admins, total_slots=total_slots,
    )
# This route shows the super admin dashboard, listing all parking locations, their admins, and total slot counts. Only super admins can access it.
#"super_admin/dashboard.html" → the template file. locations, admins, total_slots → data for the template.


#Registers this function as the handler for the /dashboard URL within the super admin blueprint.
@super_admin_bp.route("/locations")
@super_admin_required
def locations(request: Request) -> Response:
    # Show ALL locations including suspended ones so super admin can reactivate them.
    # get_all_locations() only returns active ones, so we query directly here.
    all_locations = Database.fetch_all("SELECT * FROM locations ORDER BY name ASC")
    #retrieves all rows from the locations table, sorted alphabetically by name.
    return render(request, "super_admin/locations.html", locations=all_locations)
    #Passes the results to the template.

@super_admin_bp.route("/locations/create", methods=["GET", "POST"])
@super_admin_required
def create_location_view(request: Request) -> Response:
    """Form to create a new parking location."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        county = request.form.get("county", "").strip()
        loc_type = request.form.get("type", "public")

        # Convert number fields safely — flash an error if user typed letters.
        # Total Slots is just a capacity estimate shown on the location card;
        # it's fine to leave blank since slots are added individually later
        # by the location's admin on the Slots page.
        total_slots_raw = request.form.get("total_slots", "").strip()
        hourly_rate_raw = request.form.get("hourly_rate", "").strip()
        try:
            total_slots = int(total_slots_raw) if total_slots_raw else 0
            hourly_rate = float(hourly_rate_raw) if hourly_rate_raw else 30.0
        except (ValueError, TypeError):
            flash(request, "Total slots must be a whole number and hourly rate must be a number.", "danger")
            return render(request, "super_admin/create_location.html", counties=KENYA_COUNTIES)
        #checks required fields.
        if not name or not address or not county:
            flash(request, "Name, address, and county are required.", "danger")
        else:
            # Warn if a location with the same name already exists.
            # Duplicates are allowed but confusing — admins and drivers
            # may pick the wrong one from dropdowns and lists.
            existing_name = Database.fetch_one(
                "SELECT id FROM locations WHERE LOWER(name) = ?", (name.strip().lower(),)
            )
            if existing_name:
                flash(
                    request,
                    f"Warning: a location named '{name}' already exists. "
                    "The new location was still created — make sure the address "
                    "is different so admins and drivers can tell them apart.",
                    "warning",
                )
            #Calls create_location() to save the new location in the database.
            create_location(name, address, county, loc_type, total_slots, hourly_rate)
            if not existing_name:
                flash(request, f"Location '{name}' created successfully.", "success")
            return Response.redirect(_router.url_for("super_admin.locations"))

    return render(request, "super_admin/create_location.html", counties=KENYA_COUNTIES)
# This route lets super admins create new parking locations. It shows a form (GET) and processes submissions (POST), validating input and saving the new location to the database.


@super_admin_bp.route("/locations/<location_id>/edit", methods=["GET", "POST"])
@super_admin_required
def edit_location(request: Request) -> Response:
    """Edit an existing location's core details."""
    location_id = request.param("location_id")
    location = get_location_by_id(location_id)

    if not location:
        flash(request, "Location not found.", "danger")
        return Response.redirect(_router.url_for("super_admin.locations"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        county = request.form.get("county", "").strip()
        loc_type = request.form.get("type", "public")
        total_slots_raw = request.form.get("total_slots", "").strip()
        hourly_rate_raw = request.form.get("hourly_rate", "").strip()

        try:
            total_slots = int(total_slots_raw) if total_slots_raw else 0
            hourly_rate = float(hourly_rate_raw) if hourly_rate_raw else 30.0
        except (ValueError, TypeError):
            flash(request, "Total slots must be a whole number and hourly rate must be a number.", "danger")
            return render(request, "super_admin/edit_location.html", location=location, counties=KENYA_COUNTIES,
                           location_types=LOCATION_TYPES)

        if not name or not address or not county:
            flash(request, "Name, address, and county are required.", "danger")
        else:
            update_location(location_id, {
                "name": name,
                "address": address,
                "county": county,
                "type": loc_type,
                "total_slots": total_slots,
                "hourly_rate": hourly_rate,
            })
            flash(request, f"'{name}' has been updated.", "success")
            return Response.redirect(_router.url_for("super_admin.locations"))

    return render(request, "super_admin/edit_location.html", location=location, counties=KENYA_COUNTIES,
                   location_types=LOCATION_TYPES)


@super_admin_bp.route("/admins")
@super_admin_required
def admins(request: Request) -> Response:
    """List all location admins."""
    all_admins = get_all_users_by_role("admin")
    all_locations = get_all_locations()
    return render(request, "super_admin/admins.html", admins=all_admins, locations=all_locations)
#Together, these datasets allow the template to show:

@super_admin_bp.route("/users")
@super_admin_required
def all_users(request: Request) -> Response:
    """
    System-wide user directory. Shows every account — super admins, location
    admins, guards, and drivers — with optional role and location filters.
    """
    role_filter = request.args.get("role", "").strip()
    location_filter = request.args.get("location", "").strip()

    all_users_list = get_all_users()
    all_locations = get_all_locations()
    location_names = {str(loc["id"]): loc["name"] for loc in all_locations}
    #Builds a dictionary mapping location IDs to human-readable names.

    role_counts = {"super_admin": 0, "admin": 0, "guard": 0, "driver": 0}
    for u in all_users_list:
        if u["role"] in role_counts:
            role_counts[u["role"]] += 1
   #Initializes counters for each role. Loops through all users and increments the count for their role.
    users = all_users_list
    if role_filter:
        users = [u for u in users if u["role"] == role_filter]
    if location_filter:
        users = [u for u in users if str(u["location_id"] or "") == location_filter]
    #If a role filter is set, only users with that role are kept.
    #If a location filter is set, only users at that location are kept.
    for u in users:
        u["location_name"] = location_names.get(str(u["location_id"] or ""))
    #Adds a human-readable location name to each user record.
    return render(
        request, "super_admin/all_users.html",
        users=users, locations=all_locations, role_filter=role_filter,
        location_filter=location_filter, role_counts=role_counts,
    )
# This route shows a system-wide user directory for super admins. It supports filtering by role and location, counts users by role, and displays location names alongside user records.


@super_admin_bp.route("/admins/create", methods=["GET", "POST"])
@super_admin_required
def create_admin(request: Request) -> Response:
    """Create a new location admin account."""
    all_locations = get_all_locations()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        location_id = request.form.get("location_id")
        phone = request.form.get("phone", "").strip() or None
        national_id = request.form.get("national_id", "").strip() or None
        business_permit = request.form.get("business_permit", "").strip() or None

        if not name or not email or not password or not location_id:
            flash(request, "All fields are required.", "danger")
        else:
            user_id = create_user(
                name=name, email=email, password=password, role="admin",
                location_id=location_id, phone=phone, national_id=national_id,
                business_permit=business_permit,
            )
            if user_id:
                flash(request, f"Admin account for {name} created.", "success")
                return Response.redirect(_router.url_for("super_admin.admins"))
            else:
                flash(request, "Email already in use.", "danger")

    return render(request, "super_admin/create_admin.html", locations=all_locations)
# This route lets super admins create new admin accounts by filling out a form. It validates input, saves the admin to the database, and shows success or error messages.


@super_admin_bp.route("/admins/<admin_id>/edit", methods=["GET", "POST"])
@super_admin_required
def edit_admin(request: Request) -> Response:
    """Edit a location admin's details, including reassigning their location."""
    admin_id = request.param("admin_id")
    all_locations = get_all_locations()
    admin = _get_admin_by_id(admin_id)
    #Fetches the admin record by ID, ensuring they have the "admin" role.
    if not admin:
        flash(request, "Admin not found.", "danger")
        return Response.redirect(_router.url_for("super_admin.admins"))

    if request.method == "POST":
        name            = request.form.get("name", "").strip()
        email           = request.form.get("email", "").strip().lower()
        phone           = request.form.get("phone", "").strip() or None
        national_id     = request.form.get("national_id", "").strip() or None
        business_permit = request.form.get("business_permit", "").strip() or None
        location_id     = request.form.get("location_id")

        if not name or not email or not location_id:
            flash(request, "Name, email, and location are required.", "danger")
        elif update_admin_profile(admin_id, name, email, phone, national_id,
                                    business_permit, location_id=location_id):
            flash(request, f"{name}'s details have been updated.", "success")
            return Response.redirect(_router.url_for("super_admin.admins"))
        else:
            flash(request, "That email address is already in use.", "danger")
        # Reloads the admin’s data so the form reflects what was submitted, even if there was an error.
        admin = Database.fetch_one("SELECT * FROM users WHERE id = ?", (int(admin_id),))

    return render(request, "super_admin/edit_admin.html", admin=admin, locations=all_locations)
# Passes:admin → current details to pre-fill the form.locations=all_locations → for reassignment dropdown.


@super_admin_bp.route("/admins/<admin_id>/remove", methods=["POST"])
@super_admin_required
def remove_admin(request: Request) -> Response:
    """Remove a location admin account."""
    admin_id = request.param("admin_id")
    admin = _get_admin_by_id(admin_id)

    if not admin:
        flash(request, "Admin not found.", "danger")
        return Response.redirect(_router.url_for("super_admin.admins"))

    admin_name = admin["name"] or "Admin"
    delete_user(admin_id)
    flash(request, f"{admin_name}'s admin account has been removed.", "success")
    return Response.redirect(_router.url_for("super_admin.admins"))
# This route lets super admins remove a location admin account. It checks if the admin exists, deletes their record, shows a success message, and redirects back to the admin list.


@super_admin_bp.route("/locations/<location_id>/toggle-active", methods=["POST"])
@super_admin_required
def toggle_location_active(request: Request) -> Response:
    # Suspend a location temporarily without deleting it.
    # Suspended locations do not appear in driver searches or on guard dashboards.
    location_id = request.param("location_id")
    location = get_location_by_id(location_id)
    if not location:
        flash(request, "Location not found.", "danger")
        return Response.redirect(_router.url_for("super_admin.locations"))

    # Check the current status of the location.
    #If is_active is missing, defaults to True (active).
    # Flips the status:1 → 0 (active → suspended).0 → 1 (suspended → active).
    currently_active = bool(location["is_active"]) if location["is_active"] is not None else True
    update_location(location_id, {"is_active": 0 if currently_active else 1})

    if currently_active:
        flash(request, f"{location['name']} has been suspended. It will not appear to drivers or guards.", "warning")
    else:
        flash(request, f"{location['name']} has been reactivated.", "success")

    return Response.redirect(_router.url_for("super_admin.locations"))
# This route lets super admins temporarily suspend or reactivate a parking location. It flips the 'is_active' status and shows a message confirming the change.


@super_admin_bp.route("/locations/<location_id>/delete", methods=["POST"])
@super_admin_required
def delete_location_view(request: Request) -> Response:
    """
    Permanently delete a location.

    Only allowed when the location has no session or reservation history —
    otherwise deleting it would orphan those records. Locations with
    history should be suspended instead (see toggle_location_active).
    """
    location_id = request.param("location_id")
    location = get_location_by_id(location_id)
    if not location:
        flash(request, "Location not found.", "danger")
        return Response.redirect(_router.url_for("super_admin.locations"))

    location_name = location["name"]
    deleted = delete_location(location_id)
    if deleted:
        log_action(
            user_id=request.current_user.id,
            role="super_admin",
            action="delete_location",
            details={"location_id": str(location_id), "name": location_name},
        )
        flash(request, f"{location_name} has been permanently deleted.", "success")
    else:
        flash(
            request,
            f"{location_name} has recorded sessions or reservations and can't be deleted. "
            "Suspend it instead to hide it from drivers and guards.",
            "danger",
        )

    return Response.redirect(_router.url_for("super_admin.locations"))
# This route lets super admins permanently delete a location only if it has no session or reservation history. Otherwise, the location must be suspended instead. All deletions are logged for auditing.


@super_admin_bp.route("/profile", methods=["GET", "POST"])
@super_admin_required
def profile(request: Request) -> Response:
    """Super admin views and updates their own profile details."""
    super_admin_user = get_user_by_id(request.current_user.id)
    #Retrieves the currently logged-in super admin’s details from the database.

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_profile":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()

            if not name or not email:
                flash(request, "Name and email are required.", "danger")
            else:
                existing = Database.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
                if existing and str(existing["id"]) != str(request.current_user.id):
                    flash(request, "That email is already used by another account.", "danger")
                else:
                    Database.execute(
                        "UPDATE users SET name = ?, email = ? WHERE id = ?",
                        (name, email, int(request.current_user.id)),
                    )
                    #Fetch the updated user record.
                    #Re-log the user so their session reflects the new details.
                    updated = get_user_by_id(request.current_user.id)
                    login_user(request, User(updated))
                    flash(request, "Profile updated.", "success")

        elif action == "change_password":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")

            if not check_password(request.current_user.email, current_pw):
                flash(request, "Current password is incorrect.", "danger")
            elif len(new_pw) < 6:
                flash(request, "New password must be at least 6 characters.", "danger")
            elif new_pw != confirm_pw:
                flash(request, "Passwords do not match.", "danger")
            else:
                update_password(request.current_user.id, new_pw)
                flash(request, "Password changed successfully.", "success")

        return Response.redirect(_router.url_for("super_admin.profile"))

    return render(request, "super_admin/profile.html", super_admin=super_admin_user)
# This route lets super admins view and update their own profile. They can change name/email or update their password, with validation and feedback messages.


@super_admin_bp.route("/logs")
@super_admin_required
def system_logs(request: Request) -> Response:
    # Show the most recent 200 audit log entries across all locations.
    # Allows filtering logs by role,action and location. Filters let the super admin narrow by role, action type, or location.
    #Reads optional query parameters from the request URL.
    role_filter = request.args.get("role", "").strip()
    action_filter = request.args.get("action", "").strip()
    location_filter = request.args.get("location", "").strip()

    sql = "SELECT * FROM audit_logs WHERE 1=1"
    params: list = []
    #If a filter is provided, it adds that condition to the query.
    if role_filter:
        sql += " AND role = ?"
        params.append(role_filter)
    if action_filter:
        sql += " AND action = ?"
        params.append(action_filter)
    if location_filter:
        sql += " AND location_id = ?"
        params.append(location_filter)
    sql += " ORDER BY timestamp DESC LIMIT 200"

    logs = Database.fetch_all(sql, tuple(params))
    locations = get_all_locations()
    #Runs the query against the database, with the filters applied.

    #Each log has a details column stored as a JSON string (like {"location_id":12,"name":"Westlands Parking"}).
    #This converts that string into a real Python dictionary so the template can loop through it easily.
    #If the conversion fails, it sets details to an empty dictionary.
    for log in logs:
        if log.get("details") is not None:
            try:
                log["details"] = json.loads(log["details"])
            except (TypeError, ValueError):
                log["details"] = {}

    # Attach a human-readable user name to each log entry
    user_cache: dict = {}
    for log in logs:
        uid = log["user_id"]
        if uid and uid != "system":
            if uid not in user_cache:
                try:
                    user = get_user_by_id(uid)
                    user_cache[uid] = (user["name"] or uid) if user else uid
                    #If the user exists, store their name in the cache.If the name is missing, fall back to the ID.
                    #If the user doesn’t exist at all, also fall back to the ID.
                except Exception:
                    user_cache[uid] = uid
                    #If something goes wrong (like a database error), just store the ID instead of crashing.
            log["user_name"] = user_cache[uid]
            #Adds a new field user_name to the log.
        else:
            log["user_name"] = "System"
            #If the uid was "system", then we set the name to "System".

    return render(
        request, "super_admin/logs.html",
        logs=logs, locations=locations, role_filter=role_filter,
        action_filter=action_filter, location_filter=location_filter,
    )
