"""
Kenya number plate validation and normalisation.

Functions:
  normalise_plate(plate)  — strip spaces, uppercase: "kaa 123b" -> "KAA123B"
  is_valid_plate(plate)   — True if the plate matches a recognised Kenya format
"""
import re

# Primary Kenyan civilian plate: 3 uppercase letters, space/hyphen optional, 3 digits, 1 letter
# e.g. KCA 123A, KBZ 001C, KDG 999Z
_PATTERN_STANDARD = re.compile(r'^[A-Z]{3}\s?[0-9]{3}[A-Z]$')

# Government plates: GK prefix
# Format: GK prefix + optional letter + 3–4 digits + optional letter
_PATTERN_GOVT = re.compile(r'^GK\s?[A-Z]?\s?[0-9]{3,4}[A-Z]?$')

# Diplomatic plates: CD prefix followed by numbers
# Format: CD prefix + 3–4 digits + optional letter
_PATTERN_DIPLOMATIC = re.compile(r'^CD\s?[0-9]{3,4}\s?[A-Z]?$')

# Private/county short plates (some older plates): 2 letters + digits
# Format: 2 letters + optional space + 3–4 digits + optional letter
_PATTERN_LEGACY = re.compile(r'^[A-Z]{2}\s?[0-9]{3,4}[A-Z]?$')


def normalise_plate(plate: str) -> str:
    """
    Clean up a plate number entered by a guard:
    - Strip leading/trailing whitespace
    - Uppercase
    - Collapse multiple spaces to one
    """
    plate = plate.strip().upper()
    plate = re.sub(r'\s+', ' ', plate)
    # Use the regular expression module to find one or more
    # consecutive whitespace characters and replace them with
    # a single space. This keeps spacing consistent.
    # \s means whitespace and + means one or more occurrences.
    plate = plate.replace('-', ' ')
    # Replace any hyphen (-) in the plate number with a space
    # so that plates entered with hyphens follow the same format
    # as plates entered with spaces.
    return plate


def is_valid_plate(plate: str) -> bool:
    """
    Return True if the plate matches any recognised Kenyan format.
    Guards see a warning (not a hard block) for unrecognised plates
    so they can still check in vehicles with unusual plates.
    """
    plate = normalise_plate(plate)
    # First normalise the plate so that differences in capitalisation,
    # unnecessary spaces, or hyphens do not interfere with validation.
    return bool(
        # Convert the result of the pattern checks into a Boolean value.
        # The plate is considered valid if it matches at least one of
        # the recognised Kenyan plate patterns.
        _PATTERN_STANDARD.match(plate) or
        # Check whether the plate matches the standard Kenyan
        # vehicle registration format.
        _PATTERN_GOVT.match(plate) or
        # If the standard pattern does not match, check whether
        # the plate matches the recognised government vehicle format.
        _PATTERN_DIPLOMATIC.match(plate) or
        _PATTERN_LEGACY.match(plate)
    )

