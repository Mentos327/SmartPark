"""
Central configuration for smart.park.

All settings are read from environment variables (loaded from .env on startup).
Import and use as: from config import Config
"""
import os
import secrets
from pathlib import Path


# Minimal .env loader
# Reads KEY=VALUE lines from a .env file into os.environ without
# overwriting anything already set in the real environment.

BASE_DIR = Path(__file__).resolve().parent
# __file__ represents the current Python file.
# Path(__file__) converts that file path into a Path object.
# .resolve() converts it into an absolute path.
# .parent gets the folder containing this Python file
# BASE_DIR therefore represents the main directory where this
# configuration file is located.


def _load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        # Check whether the .env file exists.
        # If the file does not exist, return immediately instead of trying
        # to read a file that is not available.
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        # Read the entire .env file using UTF-8 encoding.
        # splitlines() separates the file into individual lines so that
        # each configuration setting can be processed separately
        line = raw_line.strip()
        # Remove unnecessary whitespace from the beginning and end
        # of the current line.
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # partition("=") separates the line into three parts:
        # - everything before the first "="
        # - the "=" separator itself
        # - everything after the first "="
        # The underscore "_" is used for the separator because the
        # program does not need to use the "=" returned by partition().
        key = key.strip()
        # Remove unnecessary spaces around the configuration key.
        value = value.strip().strip('"').strip("'")
        # strip('"') removes surrounding double-quote characters.
        # strip("'") removes surrounding single-quote characters.
        os.environ.setdefault(key, value)
        # Add the key and value to the environment variables.
        # setdefault() means an existing environment variable is not
        # overwritten by the value from the .env file.
        # This allows an environment variable supplied by the operating
        # system or server environment to take priority.


_load_dotenv()
# Call the function so that the .env configuration is loaded before
# the Config class reads its settings.


class Config:
    """Central settings for smart.park. All values come from environment variables."""


    # Core / sessions

    SECRET_KEY = os.environ.get("SECRET_KEY")
    # Read the application's SECRET_KEY from the environment.
    # A secret key is normally used by the web application for security
    # purposes such as protecting session-related data.
    if not SECRET_KEY:
        raise RuntimeError(
            # Stop application startup by raising RuntimeError.
            # The error message tells the developer what configuration is
            # missing and gives a command that can generate a secure key.
            "SECRET_KEY is not set. Add it to your .env file.\n"
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )


    # Database (sqlite3)
    # Define the location of the SQLite database.
    # os.environ.get() first checks whether DATABASE_PATH has been
    # provided as an environment variable.
    # If it has not been provided, the default location is:
    # BASE_DIR / instance / smart_parking.db
    DATABASE_PATH = os.environ.get(
        "DATABASE_PATH",
        str(BASE_DIR / "instance" / "smart_parking.db"),
    )


    # HTTP server
    # HOST determines the network address on which the application's
    # HTTP server listens.
    # If HOST is not defined in the environment, 127.0.0.1 is used.
    # 127.0.0.1 refers to the local machine.
    HOST = os.environ.get("HOST", "127.0.0.1")
    PORT = int(os.environ.get("PORT", "5000"))
    # PORT determines the network port used by the HTTP server.
    # os.environ.get() returns text, so int() converts the value into
    # an integer before the application uses it as a port number.
    # If PORT is not defined, port 5000 is used.
    DEBUG = os.environ.get("DEBUG", "true").lower() == "true"
    # DEBUG controls whether the application runs in debug mode.



    # Email (smtplib — see services/email.py)
    # Specify Gmail's SMTP server.

    MAIL_SERVER = "smtp.gmail.com"
    # SMTP is the protocol used for sending email.
    MAIL_PORT = 587
    # Port 587 is the SMTP submission port commonly used with TLS.
    MAIL_USE_TLS = True
    # Enable TLS encryption for the email connection.
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    # Read the Gmail account username from the environment.
    # An empty string is used if MAIL_USERNAME has not been configured.
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    # Keeping it in an environment variable prevents the password from
    # being hard-coded directly into the source code.
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_USERNAME", "noreply@smartparking.com")
    # Define the default sender address for application emails.
    # If MAIL_USERNAME exists, it is used as the sender.
    # Otherwise, the system uses the specified fallback address.


    # M-Pesa — global settings only. Per-location credentials (till
    # number, passkey, consumer key/secret) live on the location row.
    # Store the URL that Safaricom uses to send payment callback
    # notifications back to the application.
    # This value is normally configured in the environment because it
    # can change depending on the deployment environment.
    MPESA_CALLBACK_URL = os.environ.get("MPESA_CALLBACK_URL", "")
    MPESA_ENV = os.environ.get("MPESA_ENV", "sandbox")
    # Determine whether the application uses Safaricom's sandbox
    # environment or the live production environment.
    MPESA_CALLBACK_SECRET = os.environ.get("MPESA_CALLBACK_SECRET", "")
    # Optional secret used to help verify that an incoming M-Pesa
    # callback is authorised.

    # Enable or disable M-Pesa simulation mode.
    # If the value from the environment is "true", this becomes True.
    # Otherwise it becomes False.
    MPESA_SIMULATION = os.environ.get("MPESA_SIMULATION", "true").lower() == "true"


    MPESA_REQUIRE_PAYMENT_CONFIRMATION = os.environ.get(
        "MPESA_REQUIRE_PAYMENT_CONFIRMATION", "true"
    ).lower() == "true"
    # Determine whether an M-Pesa checkout must wait for Safaricom's
    # callback before the parking session can be completed.
    # When True, the application expects confirmation from Safaricom.
    # When False, the guard's confirmation can complete the checkout
    # without waiting for the callback.
    # The default is True because real payment confirmation should
    # normally come from the payment provider.



def generate_secret_key() -> str:
    """Helper for first-time setup: `python -c "from config import generate_secret_key as g; print(g())"`"""
    return secrets.token_hex(32)
# secrets.token_hex(32) generates 32 random bytes and represents
 # them as hexadecimal characters.
 # The generated value can be used as the application's SECRET_KEY
 # during first-time configuration.
