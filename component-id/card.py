"""Component card: field definitions and stub generation (Phase 1).

The card is the exchange format between the service and its clients (phone app).
It is class-dependent: depending on the identified component class, different fields
are populated (e.g. wert/toleranz/ringe for resistors, erwartung_diode for diodes).
In Phase 1, build_stub_card() returns fixed placeholders to make the pipeline testable
end-to-end before Claude is connected (Phase 2).
"""

from __future__ import annotations

# Documentation of all possible fields (not every field is set for every class).
CARD_FIELDS = (
    "klasse",          # e.g. "Widerstand THT" | "Diode" | "IC" | ...
    "logo",            # detected manufacturer logo/symbol (determined first) | "keins erkennbar"
    "hersteller",
    "bezeichnung",
    "gehaeuse",
    "funktion",
    "wert",            # e.g. "4.7 kΩ" (resistors/capacitors)
    "toleranz",        # e.g. "±5 %"
    "ringanzahl",      # 4 | 5 | 6 (THT resistor) | null
    "ringe",           # ["gelb","violett","rot","gold"] | null
    "erwartung_ohm",   # expected Ohm reading on the DMM7510
    "erwartung_diode", # expected behavior in diode mode
    "datenblatt_url",
    "konfidenz",       # 0.0 .. 1.0
    "hinweis",         # at most one short sentence about the component (no measurement reference)
    "silhouette",      # polygon [[x,y],...] normalized 0..1 tightly around the component | null
)


def empty_card() -> dict:
    """Card with all fields set to neutral defaults."""
    card = {f: None for f in CARD_FIELDS}
    card["konfidenz"] = 0.0
    return card


def build_stub_card(image_bytes: bytes, dmm: dict | None) -> dict:
    """Phase 1 placeholder: populates the card with recognizable stub values.

    Proves that request -> identify() -> response flows end-to-end. Replaced in Phase 2
    by the result built from the Claude response.
    """
    card = empty_card()
    card.update(
        klasse="STUB",
        bezeichnung="Phase-1-Stub -- no real identification yet",
        funktion="Placeholder; Claude integration follows in Phase 2",
        konfidenz=0.0,
        hinweis=(
            f"Phase-1-Stub. Received: image={len(image_bytes)} bytes, "
            f"DMM context={'yes' if dmm else 'no'}."
        ),
    )
    # Debug echo so you can see what arrived during testing (removable in Phase 2).
    card["_debug"] = {"image_bytes": len(image_bytes), "dmm_received": dmm}
    return card
