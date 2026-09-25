"""
Congestion / utilisation report queries.

Called by the super-admin congestion view.
Returns current slot occupancy rates across all locations, split into
"flagged" (>= 80% occupied) and "healthy" locations.
"""
from __future__ import annotations

from models.location import get_all_locations
from models.slot import count_slots_by_status


def build_congestion_report() -> dict:
    locations = get_all_locations()
    # Create an empty list to store locations whose occupancy is
    # 80% or higher and therefore need to be flagged as congested.
    flagged = []
    # Create an empty list to store locations whose occupancy is
    # below 80% and are therefore considered healthy.
    healthy = []

    for loc in locations:
        summary = count_slots_by_status(str(loc["id"]))
        # Exclude disabled slots from the total — they are not usable bays.
        # Including them would make a congested lot with closed bays look less full.
        total = summary.get("available", 0) + summary.get("occupied", 0) + summary.get("reserved", 0)
        occupied = summary.get("occupied", 0) + summary.get("reserved", 0)
        pct = int((occupied / total * 100)) if total > 0 else 0

        loc["slot_summary"] = summary
        loc["occupancy_pct"] = pct
        loc["usable_slots"] = total

        # A location with 80% or more of its usable slots occupied or
        # reserved is considered congested and is added to the flagged list.
        if pct >= 80:
            flagged.append(loc)
        else:
            healthy.append(loc)

    # Sort flagged locations — most congested first
    # key=lambda l: l["occupancy_pct"] tells Python to use each
    # location's occupancy percentage for sorting.
    # reverse=True means the order is descending, so the location with
    # the highest occupancy appears first.
    flagged.sort(key=lambda l: l["occupancy_pct"], reverse=True)

    return {"flagged": flagged, "healthy": healthy}
