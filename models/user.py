"""
User account model for smart.park.

Roles: super_admin | admin | guard | driver

Key functions:
  User.get_by_id(id)            — reload a user from the DB (used by session manager)
  create_user(...)              — register a new account
  check_password(email, pw)     — verify credentials; returns row or None
  get_user_by_email(email)      — lookup by email
  update_password(id, new_pw)   — hash and save a new password
  delete_user(id)               — permanently remove an account
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from database.db import Database, Row
from utils.security import hash_password, verify_password


class User:
    """
    Lightweight wrapper around a `users` table row.

    Roles:
        super_admin   — owns the whole system
        admin         — manages one location
        guard         — checks vehicles in/out
        driver        — parks vehicles

    The session manager stores user.id and calls User.get_by_id()
    to reload the user on each request.
    """

    def __init__(self, row: Row):
        # Store the user's database ID as a string so it can be used
        # consistently throughout the application.
        self.id = str(row["id"])
        self.name = row["name"] or ""
        self.email = row["email"] or ""
        self.role = row["role"] or "driver"
        self.location_id = row["location_id"]

    @property
    def is_authenticated(self) -> bool:
        # Real (non-anonymous) users are always considered authenticated.
        return True

    @staticmethod
    def get_by_id(user_id) -> Optional["User"]:
        """Find a user by their integer primary key (string or int).
        Called by the session manager on each request to reload the
        currently logged-in user.
        """
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            # If the supplied ID cannot be converted into an integer,
            # it is considered invalid, so return None
            return None
        row = Database.fetch_one("SELECT * FROM users WHERE id = ?", (uid,))
        # If a matching database row exists, convert it into a User
        # object. If no row exists, return None.
        return User(row) if row else None


def create_user(name, email, password, role, location_id=None,
                 phone=None, national_id=None, business_permit=None):
    """
    Create a new user and save to the database.
    Returns the new row's id, or None if email already exists.
    phone and national_id are optional — used for guards.
    Email is normalised to lowercase before storage so that lookups via
    get_user_by_email() (which also lowercases) always find the right record.
    """
    email = email.strip().lower() if email else None
    if email:
        existing = Database.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
        if existing:
            return None  # email already in use

    # Convert the plain-text password into a secure password hash.
    hashed_password = hash_password(password)
    # Record the account creation time using UTC and store it as
    # an ISO-formatted string.
    joined_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    # The `?` symbols are SQL parameter placeholders. The actual values are supplied separately below
    new_id = Database.execute(
        """
        INSERT INTO users (name, email, password, role, location_id, phone,
                            national_id, business_permit, joined_at, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            name, email, hashed_password, role,
            str(location_id) if location_id else None,
            phone, national_id, business_permit, joined_at,
        ),
    )
    return new_id


def check_password(email, password):
    """
    Verify login credentials.
    Returns the user row if correct, None if wrong.
    """
    user_data = Database.fetch_one(
        "SELECT * FROM users WHERE email = ?", (email,)
    )
    # If no account exists with that email, the login attempt fails.
    if not user_data:
        return None  # email not found

    # The password is correct, so return the user's database record.
    if verify_password(user_data["password"], password):
        return user_data

    return None  # wrong password


def get_all_users_by_role(role):
    """Return a list of all users with a given role."""
    # Retrieve every user whose role matches the supplied role.
    return Database.fetch_all("SELECT * FROM users WHERE role = ?", (role,))


def get_all_users():
    """
    Return every user account in the system, regardless of role.
    Used by the super admin's system-wide user directory.
    Newest accounts first.
    """
    # Retrieve all user accounts and order them by joined_at in
    # descending order so the newest accounts appear first.
    return Database.fetch_all("SELECT * FROM users ORDER BY joined_at DESC")


def get_users_at_location(location_id):
    """Return guards and admins assigned to a specific location."""
    # Retrieve users whose location_id matches the selected parking location
    return Database.fetch_all(
        "SELECT * FROM users WHERE location_id = ?", (str(location_id),)
    )


def set_reset_token(email, token, expires_at):
    """
    Save a password reset token against the user's email.
    expires_at is a UTC datetime — token is rejected after this time.
    """
    # Save the password-reset token and its expiration time for the
    # user associated with the supplied email address.
    Database.execute(
        "UPDATE users SET reset_token = ?, reset_expires_at = ? WHERE email = ?",
        (token, expires_at.isoformat(), email),
    )


def get_user_by_reset_token(token):
    """
    Return the user row for a given reset token,
    but only if the token has not expired yet.
    Returns None if not found or expired.
    """
    # Search for a user account that contains the supplied reset token.
    user = Database.fetch_one("SELECT * FROM users WHERE reset_token = ?", (token,))
    # If no account has that token, the token is invalid.
    if not user:
        return None
    # Retrieve the stored expiration time for the reset token.
    expires_at_raw = user["reset_expires_at"]
    # If no expiration time exists, the reset request is considered invalid.
    if not expires_at_raw:
        return None
    # Convert the stored ISO-formatted expiration time back into
    # a Python datetime object so it can be compared with the current time.
    expires_at = datetime.fromisoformat(expires_at_raw)
    # If the datetime contains timezone information, remove it so
    # the comparison uses compatible datetime objects.
    if expires_at.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=None)
        # Get the current UTC time and compare it with the token's expiration.
        # If the current time is later than the expiration time, the token has expired and cannot be used.
    if datetime.now(timezone.utc).replace(tzinfo=None) > expires_at:
        return None
    # The token exists and has not expired, so return the user record.
    return user


def update_password(user_id, new_password):
    """
    Hash and save a new password, then clear the reset token
    so the same link cannot be used again.
    """
    hashed = hash_password(new_password)
    # Replace the user's old password with the new password hash.
    # At the same time, clear reset_token and reset_expires_at
    Database.execute(
        "UPDATE users SET password = ?, reset_token = NULL, reset_expires_at = NULL "
        "WHERE id = ?",
        (hashed, int(user_id)),
    )


def get_user_by_email(email):
    """Return a user row by email address, or None."""
    return Database.fetch_one(
        "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
    )
 # fetch_one() returns one matching user row or None if no match exists.


def get_user_by_id(user_id):
    """Return a raw user row by id, or None."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    return Database.fetch_one("SELECT * FROM users WHERE id = ?", (uid,))


def delete_user(user_id):
    """Permanently delete a user account.
    Used when a guard is dismissed — removes their login access.
    Returns True if deleted, False if the user was not found.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    existing = Database.fetch_one("SELECT id FROM users WHERE id = ?", (uid,))
    if not existing:
        return False
    Database.execute("DELETE FROM users WHERE id = ?", (uid,))
    return True
# Validate the user ID before performing the deletion.
    # First confirm that the user exists.

def get_driver_by_plate(plate_number):
    """
    Find a registered driver who has previously parked with this plate.
    Used by the guard check-in so sessions get linked to the driver's account.
    Returns the user row or None if no match found.
    """
    session = Database.fetch_one(
        """
        SELECT * FROM sessions
        WHERE plate_number = ? AND driver_id IS NOT NULL
        ORDER BY entry_time DESC LIMIT 1
        """,
        (plate_number.upper().strip(),),
    )
    if not session:
        return None
    try:
        return Database.fetch_one(
            "SELECT * FROM users WHERE id = ?", (int(session["driver_id"]),)
        )
    except (TypeError, ValueError):
        return None
 # Find the most recent parking session for this plate that is linked
    # to a registered driver account.

def update_admin_profile(user_id, name, email, phone, national_id, business_permit, location_id=None):
    """
    Update an admin's profile details.
    location_id is optional — pass it to reassign the admin to a different
    location (used by super admin's Edit Admin page); omit it to leave the
    admin's location unchanged (used by the admin's own profile page).
    Returns True if updated, False if the new email is taken by someone else.
    """
    # Check that the new email is not already assigned to another user.
    existing = Database.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
    if existing and str(existing["id"]) != str(user_id):
        return False

    if location_id is not None:
        Database.execute(
            """
            UPDATE users SET name = ?, email = ?, phone = ?, national_id = ?,
                             business_permit = ?, location_id = ?
            WHERE id = ?
            """,
            (name, email, phone or None, national_id or None,
             business_permit or None, str(location_id), int(user_id)),
        )
    else:
        Database.execute(
            """
            UPDATE users SET name = ?, email = ?, phone = ?, national_id = ?,
                             business_permit = ?
            WHERE id = ?
            """,
            (name, email, phone or None, national_id or None,
             business_permit or None, int(user_id)),
        )
    return True


def update_user_profile(user_id, name, email):
    """
    Update a driver's name and email.
    Returns True if updated, False if the new email is already taken by someone else.
    """
    existing = Database.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
    if existing and str(existing["id"]) != str(user_id):
        return False

    Database.execute(
        "UPDATE users SET name = ?, email = ? WHERE id = ?",
        (name, email, int(user_id)),
    )
    return True
# Check that the new email is not already used by another account.
    # Update the driver's name and email in the users table.

def update_guard_details(user_id, name, email, phone, national_id):
    """
    Update a guard's editable details (used by the admin's Edit Guard page).
    Returns True if updated, False if the new email is already taken by someone else.
    """
    existing = Database.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
    if existing and str(existing["id"]) != str(user_id):
        return False

    Database.execute(
        "UPDATE users SET name = ?, email = ?, phone = ?, national_id = ? WHERE id = ?",
        (name, email, phone or None, national_id or None, int(user_id)),
    )
    return True
 # Check that the new email is not already assigned to another user.
    # Update the guard's editable personal details.