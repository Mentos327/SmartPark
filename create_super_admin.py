"""
Create or update the super-admin account.

Run this during initial setup or to reset a forgotten super-admin password:
    python create_super_admin.py

Prompts for name, email, and password. Updates the account if the email
already exists, or creates a new one.
"""
from database.db import init_db, Database
from utils.security import hash_password

init_db()
# Initialize the database before performing any account operation.
# This ensures that the required database tables and structure exist
# before the script attempts to read or modify the users table.
print()
print("  Create / Update Super Admin Account")
print()

name = input("Full name     : ").strip()
email = input("Email address : ").strip().lower()
password = input("Password      : ").strip()

if not name or not email or not password:
    print()
    print("ERROR: Name, email, and password are all required.")
    raise SystemExit(1)
# Stop the program immediately.
    # SystemExit(1) indicates that the program ended because of an error.

if len(password) < 6:
    print()
    print("ERROR: Password must be at least 6 characters.")
    raise SystemExit(1)
# Stop the program immediately.
    # SystemExit(1) indicates that the program ended because of an error.

hashed = hash_password(password)
# Pass the plain-text password to the application's password-hashing # function.
# The returned value is the hashed password that will be stored
# in the database instead of storing the original password directly.

existing = Database.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
# Search the users table for an account with the supplied email address
# The "?" is a parameter placeholder. The actual email is supplied
# separately through the tuple (email,).
# fetch_one() retrieves at most one matching database record.

if existing:
    # Check whether a user account with this email already exists.
    Database.execute(
        """
        UPDATE users SET name = ?, password = ?, role = 'super_admin',
                         location_id = NULL, is_active = 1
        WHERE id = ?
        """,
        (name, hashed, existing["id"]),
    )
    # If the account exists, update that existing user's information.
    # The account is explicitly changed to the super_admin role.
    # location_id is set to NULL because the Super Admin is not
    # restricted to one specific parking location.
    # is_active = 1 ensures that the account is enabled.
    print()
    print(f"  Account updated — {email} is now a Super Admin.")
else:
    Database.execute(
        """
        INSERT INTO users (name, email, password, role, location_id, is_active, joined_at)
        VALUES (?, ?, ?, 'super_admin', NULL, 1, datetime('now'))
        """,
        (name, email, hashed),
    )
    print()
    print(f"  Super Admin account created for {email}.")
    # Tell the administrator that the existing account has been
    # successfully converted or updated as a Super Admin.

print()
print("  You can now log in at / with those credentials.")
print()
