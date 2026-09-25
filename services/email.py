"""
Outgoing email via smtplib.

All emails are sent through send_email() which logs failures to the
email_failures table so nothing is silently lost.

Available senders:
  send_password_reset_email(to, reset_url)
  send_reservation_confirmation(to, ...)
  send_reservation_needs_approval(to, ...)
  send_reservation_reminder(to, ...)
  send_receipt_email(to, ...)
  send_guard_credentials(to, name, password, location_name)
"""
from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from config import Config
from database.db import Database


def send_email(to: str, subject: str, body_html: str) -> bool:
    """
    Low-level helper. All other functions call this one.
    Returns True if sent, False if it failed.
    Failures are printed to the terminal AND saved to the email_failures
    table so admins can see what was not delivered.
    """
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = Config.MAIL_DEFAULT_SENDER
        msg["To"] = to
        msg.set_content("This email requires an HTML-capable mail client to view.")
        msg.add_alternative(body_html, subtype="html")

        with smtplib.SMTP(Config.MAIL_SERVER, Config.MAIL_PORT, timeout=15) as smtp:
            if Config.MAIL_USE_TLS:
                smtp.starttls()
            if Config.MAIL_USERNAME and Config.MAIL_PASSWORD:
                smtp.login(Config.MAIL_USERNAME, Config.MAIL_PASSWORD)
            smtp.send_message(msg)
        return True

    except Exception as e:
        print(f"[EMAIL ERROR] Could not send to {to}: {e}")
        try:
            Database.execute(
                """
                INSERT INTO email_failures ("to", subject, error, failed_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    to, subject, str(e),
                    datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                ),
            )
        except Exception:
            pass  # Never let email logging crash the app
        return False


def send_reservation_confirmation(driver_email, reservation_data):
    subject = f"Reservation Confirmed — Slot {reservation_data['slot_number']}"

    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: auto;">
      <h2 style="color: #1A3C5E;">Reservation Confirmed</h2>
      <p>Your slot has been reserved. Present your reservation details to the guard on arrival.</p>
      <hr>
      <table style="width:100%;">
        <tr><td><strong>Location:</strong></td><td>{reservation_data['location_name']}</td></tr>
        <tr><td><strong>Slot:</strong></td><td>{reservation_data['slot_number']}</td></tr>
        <tr><td><strong>From:</strong></td><td>{reservation_data['reserved_from']}</td></tr>
        <tr><td><strong>Until:</strong></td><td>{reservation_data['reserved_until']}</td></tr>
      </table>
      <hr>
      <p style="color: #888; font-size: 12px;">
        Your reservation is valid until your end time.
        If you no longer need the slot, please cancel in the app.
      </p>
    </body></html>
    """
    return send_email(driver_email, subject, body)


def send_reservation_needs_approval(driver_email, reservation_data):
    """
    Sent instead of send_reservation_confirmation when create_reservation
    flags the booking as pending_approval (3+ no-shows in the last 30 days).
    The slot is NOT locked yet — a guard/admin has to approve it first —
    so this must not claim the reservation is confirmed.
    """
    subject = f"Reservation Received — Awaiting Approval — Slot {reservation_data['slot_number']}"

    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: auto;">
      <h2 style="color: #1A3C5E;">Reservation Awaiting Approval</h2>
      <p>
        We've received your booking request, but it has not been confirmed yet.
        Because of 3 or more missed reservations in the last 30 days, this
        booking needs to be approved by a guard or admin at the location
        before the slot is held for you.
      </p>
      <hr>
      <table style="width:100%;">
        <tr><td><strong>Location:</strong></td><td>{reservation_data['location_name']}</td></tr>
        <tr><td><strong>Slot:</strong></td><td>{reservation_data['slot_number']}</td></tr>
        <tr><td><strong>From:</strong></td><td>{reservation_data['reserved_from']}</td></tr>
        <tr><td><strong>Until:</strong></td><td>{reservation_data['reserved_until']}</td></tr>
      </table>
      <hr>
      <p style="color: #888; font-size: 12px;">
        You will not receive a confirmation email until this is approved.
        Please arrive only once you have confirmation the slot is held.
      </p>
    </body></html>
    """
    return send_email(driver_email, subject, body)


def send_reservation_reminder(driver_email, reservation_data):
    subject = f"Reminder: Your parking reservation starts soon — Slot {reservation_data['slot_number']}"

    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: auto;">
      <h2 style="color: #1A3C5E;">Reservation Reminder</h2>
      <p>Your parking reservation starts in approximately 2 hours.</p>
      <hr>
      <table style="width:100%;">
        <tr><td><strong>Location:</strong></td><td>{reservation_data['location_name']}</td></tr>
        <tr><td><strong>Slot:</strong></td><td>{reservation_data['slot_number']}</td></tr>
        <tr><td><strong>Starts:</strong></td><td>{reservation_data['reserved_from']}</td></tr>
        <tr><td><strong>Until:</strong></td><td>{reservation_data['reserved_until']}</td></tr>
      </table>
      <hr>
      <p style="color: #888; font-size: 12px;">
        If you cannot make it, please cancel your reservation so the slot
        can be released for other drivers.
      </p>
    </body></html>
    """
    return send_email(driver_email, subject, body)


def send_receipt_email(driver_email, plate, location_name, entry_time, exit_time,
                        duration, fee_kes, payment_method):
    def fmt(t):
        """Format a datetime as EAT (UTC+3) for the receipt; pass strings through unchanged."""
        if t is None:
            return "N/A"
        if isinstance(t, datetime):
            eat = t.replace(tzinfo=None) + timedelta(hours=3)
            return eat.strftime("%d %b %Y, %H:%M") + " EAT"
        return t

    subject = f"Parking Receipt — {plate}"

    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: auto;">
      <h2 style="color: #1A3C5E;">Parking Receipt</h2>
      <hr>
      <table style="width:100%; border-collapse: collapse;">
        <tr>
          <td style="padding: 6px 0;"><strong>Vehicle Plate:</strong></td>
          <td>{plate}</td>
        </tr>
        <tr>
          <td style="padding: 6px 0;"><strong>Location:</strong></td>
          <td>{location_name}</td>
        </tr>
        <tr>
          <td style="padding: 6px 0;"><strong>Entry Time:</strong></td>
          <td>{fmt(entry_time)}</td>
        </tr>
        <tr>
          <td style="padding: 6px 0;"><strong>Exit Time:</strong></td>
          <td>{fmt(exit_time)}</td>
        </tr>
        <tr>
          <td style="padding: 6px 0;"><strong>Duration:</strong></td>
          <td>{duration}</td>
        </tr>
        <tr style="background: #f5f5f5;">
          <td style="padding: 6px 0;"><strong>Amount Paid:</strong></td>
          <td><strong>KES {fee_kes}</strong></td>
        </tr>
        <tr>
          <td style="padding: 6px 0;"><strong>Payment Method:</strong></td>
          <td>{payment_method.upper()}</td>
        </tr>
      </table>
      <hr>
      <p style="color: #888; font-size: 12px;">
        Keep this receipt for your records. Thank you for using Smart Parking.
      </p>
    </body></html>
    """
    return send_email(driver_email, subject, body)


def send_guard_credentials(guard_email, guard_name, password, location_name):
    """Sent to a guard when their admin creates their account, with their
    login email and auto-generated starting password."""
    subject = "Your Smart Parking Guard Account"

    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: auto;">
      <h2 style="color: #1A3C5E;">Welcome, {guard_name}</h2>
      <p>Your guard account has been created for <strong>{location_name}</strong>.</p>
      <p>Use the credentials below to log in to the Smart Parking system.</p>
      <hr>
      <table style="width:100%; border-collapse: collapse;">
        <tr>
          <td style="padding: 8px 0; width: 140px;"><strong>Username (Email):</strong></td>
          <td style="padding: 8px 0;">{guard_email}</td>
        </tr>
        <tr>
          <td style="padding: 8px 0;"><strong>Start Password:</strong></td>
          <td style="padding: 8px 0; font-family: monospace; font-size: 1.05em;">{password}</td>
        </tr>
      </table>
      <hr>
      <p style="color: #555; font-size: 13px;">
        Please log in and change your password after your first sign-in.
      </p>
    </body></html>
    """
    return send_email(guard_email, subject, body)


def send_password_reset_email(to_email, reset_url):
    subject = "Reset Your Smart Parking Password"

    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: auto;">
      <h2 style="color: #1A3C5E;">Password Reset Request</h2>
      <p>We received a request to reset the password for your Smart Parking account.</p>
      <p>Click the button below to set a new password. This link expires in <strong>1 hour</strong>.</p>
      <p style="margin: 30px 0;">
        <a href="{reset_url}"
           style="background:#0d6efd; color:white; padding:12px 24px;
                  border-radius:6px; text-decoration:none; font-weight:bold;">
          Reset My Password
        </a>
      </p>
      <p>Or copy and paste this link into your browser:</p>
      <p style="word-break:break-all; color:#555;">{reset_url}</p>
      <hr>
      <p style="color:#888; font-size:12px;">
        If you did not request a password reset, ignore this email.
        Your password will not change.
      </p>
    </body></html>
    """
    return send_email(to_email, subject, body)
