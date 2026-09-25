"""
Password and PIN hashing for smart.park.

Uses PBKDF2-SHA256 (Python built-in hashlib) with 260,000 iterations
and a per-password salt. The full hash is stored as one string:
  pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

Functions:
  hash_password(plain)          — hash a password or PIN for storage
  verify_password(stored, cand) — constant-time comparison; returns bool
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
# The algorithm name is stored as a constant so the same algorithm can
# be identified when a password hash is later verified.
_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 260_000  # OWASP-recommended minimum for PBKDF2-SHA256 (2023+)
# This is the number of PBKDF2 iterations performed when deriving the
# password hash.# A high number of iterations intentionally makes password hashing more
# # computationally expensive. This makes large-scale password guessing
# # attacks more difficult.
_SALT_BYTES = 16
# This specifies the number of random bytes used to create the salt.
# A salt is random data added to the password before the password is
# processed by the key-derivation function.# Each password hash gets its own salt.


def hash_password(plain_password: str) -> str:
    """Hash a password or PIN for storage using PBKDF2-SHA256."""
    salt = secrets.token_hex(_SALT_BYTES)
    # Generate a random salt using Python's cryptographically secure
    # secrets module.
    # token_hex(16) generates 16 random bytes and represents them as
    # hexadecimal characters.
    # The salt ensures that identical passwords do not automatically
    # produce identical stored hashes.

    # PBKDF2 derives a cryptographic digest from the password and salt.
    # hashlib.pbkdf2_hmac() performs the key-derivation operation using:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        #SHA-256 is used as the underlying hash function.
        plain_password.encode("utf-8"),
        #converts the password from a Python string into bytes because the cryptographic function
        #operates on bytes.
        salt.encode("utf-8"),
        #-> converts the salt into bytes.
        _ITERATIONS,
        #specifies how many times the derivation process is performed.
    ).hex()
    # .hex() converts the resulting binary digest into a hexadecimal
    # string so it can be stored as text.
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest}"
    # Store all information required to verify the password later.
    # The $ character separates the four pieces of information:
    # # The original password itself is NOT included.


def verify_password(stored_hash: str, candidate_password: str) -> bool:
    """Verify a password or PIN against a stored hash.
    Returns False for malformed or missing hashes rather than raising.
    """
    if not stored_hash:
        # If there is no stored hash, verification cannot be performed.
        return False
    try:
        # Attempt to split the stored hash into its four expected components:
        algorithm, iterations_str, salt, expected_digest = stored_hash.split("$")
    except ValueError:
        # If the stored value does not contain exactly the expected number
        # of components, split() will not be able to assign the values
        return False
    if algorithm != _ALGORITHM:
        # Confirm that the stored hash uses the algorithm expected by this
        # password module.
        # This prevents the function from blindly processing a hash that
        # uses an unsupported or unexpected algorithm.
        return False

    actual_digest = hashlib.pbkdf2_hmac(
        # Recalculate the password digest using the candidate password.
        "sha256",
        candidate_password.encode("utf-8"),
        # - the password supplied during login
        salt.encode("utf-8"),
        # - the salt stored with the original hash
        int(iterations_str),
        # - the iteration count stored with the original hash
    ).hex()
    # Constant-time comparison prevents timing attacks from leaking how
    # many leading characters of the hash matched.
    return hmac.compare_digest(actual_digest, expected_digest)
    # Compare the newly calculated digest with the digest stored in
    # the database.
    # hmac.compare_digest() performs a constant-time comparison.
    # This is preferable to a normal string comparison for security-
    # sensitive values because it reduces the risk of timing information
    # revealing how much of the two values matched.
    # The result is a Boolean:
    # True  -> the password is correct.
    # False -> the password is incorrect.