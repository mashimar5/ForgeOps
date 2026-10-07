"""
ILLUSTRATIVE PLANT NAMES -- a display layer over the anonymized codes

The Bosch data is anonymized: nothing says what the parts are or what any
station does. To make the flow easier to follow, the dashboard and the API
describe the plant as a factory for electronic control units (ECUs). The
names fit how each line behaves in the data, not what it really does:

    L0  24 stations, parts clear it in about 20 minutes  -> circuit-board line
    L1  2 stations, sub-steps spread over days           -> potting & cure line
    L2  3 stations, about 30% of parts                   -> connector sub-assembly
    L3  23 stations in about 20 minutes, two paths       -> final assembly, cells A and B

Stations get operation numbers in order (OP10, OP20, ...), as real routings
do, rather than invented functions. Products are route families: a part's
entry line, whether it goes through L2, and its line-3 cell. In a real plant
the product is known when the order is released, so it is shown from the
moment a part enters; in this data it is read from the part's whole route.

The real codes stay alongside every name. The AI assistant's tools and its
eval use the codes only.
"""

import numpy as np

from production_data import line_of, station_number

NOTE = (
    "Illustrative names: the Bosch data is anonymized, so the plant is described as an ECU factory "
    "whose lines behave like the real ones (a fast circuit-board line, a long potting & cure line, "
    "an optional connector sub-assembly, final assembly in two cells). Station names are operation "
    "numbers in production order. Products are route families; a real plant knows a part's product "
    "from its order, so it is shown from entry, though here it comes from the part's whole route."
)

LINES = {
    "L0": {"name": "Circuit-board line", "behaviour": "24 stations; parts clear it in about 20 minutes"},
    "L1": {"name": "Potting & cure line", "behaviour": "2 stations whose sub-steps span days"},
    "L2": {"name": "Connector sub-assembly", "behaviour": "3 stations; about 30% of parts go through it"},
    "L3": {"name": "Final assembly", "behaviour": "23 stations in about 20 minutes, in two cells"},
}

# Line 3's two paths (analyze_dates.py): cell A is S29-S38, cell B S39-S51
CELLS = {"A": (29, 38), "B": (39, 51)}
FIRST_STATION = {"L0": 0, "L1": 24, "L2": 26}

PRODUCTS = [
    {"id": "standard", "name": "Standard ECU", "route": "Circuit-board line, final assembly cell A",
     "match": ("L0", False, "S29-S38")},
    {"id": "potted-connector", "name": "Potted ECU + connector", "route": "Potting & cure line, connector sub-assembly, cell A",
     "match": ("L1", True, "S29-S38")},
    {"id": "connector", "name": "ECU + connector", "route": "Circuit-board line, connector sub-assembly, cell A",
     "match": ("L0", True, "S29-S38")},
    {"id": "compact", "name": "Compact ECU", "route": "Circuit-board line, final assembly cell B",
     "match": ("L0", False, "S39-S51")},
    {"id": "potted", "name": "Potted ECU", "route": "Potting & cure line, final assembly cell A",
     "match": ("L1", False, "S29-S38")},
    {"id": "special", "name": "Special variant", "route": "Any other route (about 1% of parts)", "match": None},
]
SPECIAL = len(PRODUCTS) - 1


def cell_of(station):
    number = station_number(station)
    return next((cell for cell, (lo, hi) in CELLS.items() if lo <= number <= hi), None)


def station_label(station):
    """'L3_S32' -> {'line': 'Final assembly', 'cell': 'A', 'op': 'OP40', 'label': 'Final assembly cell A · OP40'}"""

    line = line_of(station)
    number = station_number(station)
    cell = cell_of(station)
    # Operations count from each line's (or line-3 cell's) first station
    first = CELLS[cell][0] if cell else FIRST_STATION[line]
    op = f"OP{(number - first + 1) * 10}"
    where = LINES[line]["name"] + (f" cell {cell}" if cell else "")
    return {"station": station, "line": LINES[line]["name"], "cell": cell, "op": op, "label": f"{where} · {op}"}


def product_index(entry_line, through_l2, l3_path):
    """Index into PRODUCTS for every part (arrays of equal length)."""

    out = np.full(len(entry_line), SPECIAL, np.int8)
    for i, product in enumerate(PRODUCTS[:SPECIAL]):
        line, l2, path = product["match"]
        out[(entry_line == line) & (through_l2 == l2) & (l3_path == path)] = i
    return out


def plant(stations):
    """The naming layer for clients: lines, stations, products."""

    return {
        "theme": "Electronic control unit (ECU) plant",
        "lines": [{"code": code, **info} for code, info in LINES.items()],
        "stations": [station_label(s) for s in stations],
        "products": [{k: v for k, v in p.items() if k != "match"} for p in PRODUCTS],
        "note": NOTE,
    }
