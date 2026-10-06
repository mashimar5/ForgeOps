"""
Tests for the HTTP API, run against the real serving data. Build it first:

    .venv/bin/python src/build_serving_data.py
    .venv/bin/python -m pytest tests

Most tests check the API's main promise: an answer "as of" an hour only
uses what was known at that hour.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory_service import SERVING_DIR  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (SERVING_DIR / "parts.parquet").exists(),
    reason="Serving data not built; run src/build_serving_data.py first",
)

from fastapi.testclient import TestClient  # noqa: E402

from api import app  # noqa: E402


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def service(client):
    return client.app.state.service


def hours(tick):
    return tick / 10


@pytest.fixture(scope="module")
def riskiest_failure(service):
    """The scorable failure with the highest risk score."""

    rows = np.flatnonzero(service._scorable & (service._response == 1))
    row = rows[np.argmax(service._risk_score[rows])]

    return {
        "id": int(service._part_id[row]),
        "start": hours(service._start[row]),
        "end": hours(service._end[row]),
    }


@pytest.fixture(scope="module")
def training_part(service):
    """A part the model trained on."""

    row = np.flatnonzero(~service._scorable & (service._start < np.iinfo(np.int64).max))[0]

    return {"id": int(service._part_id[row]), "end": hours(service._end[row])}


@pytest.fixture(scope="module")
def long_wait_part(service):
    """A scorable part that spent more than a week on the line."""

    rows = np.flatnonzero(service._scorable & (service._end - service._start > 1680))
    row = rows[0]

    return {
        "id": int(service._part_id[row]),
        "start": hours(service._start[row]),
        "end": hours(service._end[row]),
    }


# ============================================================
# NOTHING FROM THE FUTURE
# ============================================================

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_summary_only_counts_reported_results(client):
    at_start = client.get("/summary", params={"at_hour": 0}).json()
    midway = client.get("/summary", params={"at_hour": 8000}).json()
    at_end = client.get("/summary").json()

    assert at_start["qc_results_known"] == 0
    assert 0 < midway["qc_results_known"] < at_end["qc_results_known"]
    assert at_end["parts_in_production"] == 0
    assert at_end["qc_failure_rate_pct"] == pytest.approx(0.58, abs=0.01)


def test_part_is_unknown_before_it_enters(client, riskiest_failure):
    response = client.get(f"/parts/{riskiest_failure['id']}", params={"at_hour": riskiest_failure["start"] - 1})

    assert response.status_code == 404


def test_unknown_part(client):
    assert client.get("/parts/3").status_code == 404


def test_route_only_shows_stations_already_visited(client, long_wait_part):
    midway = (long_wait_part["start"] + long_wait_part["end"]) / 2
    part = client.get(f"/parts/{long_wait_part['id']}", params={"at_hour": midway}).json()

    assert part["status"] == "in production"
    assert part["finished_hour"] is None
    assert part["qc_result"] is None
    assert part["batch_mates"] is not None
    assert all(step["hour"] <= midway for step in part["route_so_far"])
    assert part["risk"]["available"] is False


def test_qc_result_hidden_until_reported(client, riskiest_failure):
    """QC results are reported 1 hour after a part's last station."""

    url = f"/parts/{riskiest_failure['id']}"
    end = riskiest_failure["end"]

    assert client.get(url, params={"at_hour": end}).json()["qc_result"] is None
    assert client.get(url, params={"at_hour": end + 1.0}).json()["qc_result"] is None
    assert client.get(url, params={"at_hour": end + 1.1}).json()["qc_result"] == "failed"


def test_batch_alerts_only_flag_parts_still_in_production(client):
    at = 15000
    alerts = client.get("/alerts/batch-mates", params={"at_hour": at, "limit": 5}).json()

    assert alerts["flagged_parts"] > 0

    for item in alerts["items"]:
        assert item["entered_hour"] <= at
        assert item["first_failure_known_hour"] < at

        part = client.get(f"/parts/{item['part_id']}", params={"at_hour": at}).json()
        assert part["status"] == "in production"
        assert part["batch_mates"]["flagged"] is True


# ============================================================
# RISK SCORES
# ============================================================

def test_no_score_for_parts_the_model_trained_on(client, training_part):
    risk = client.get(f"/parts/{training_part['id']}/risk").json()

    assert risk["available"] is False
    assert "trained on this part" in risk["reason"]
    assert risk["explanation"] is None


def test_risk_explanation_matches_the_score(client, riskiest_failure):
    risk = client.get(f"/parts/{riskiest_failure['id']}/risk", params={"top": 5}).json()

    assert risk["available"] is True
    assert 0 < risk["risk_score"] < 1

    contributions = [c["contribution"] for c in risk["explanation"]["top_contributions"]]
    assert len(contributions) == 5
    assert contributions == sorted(contributions, key=abs, reverse=True)

    # SHAP log-odds add up to the model's own score
    assert 1 / (1 + math.exp(-risk["explanation"]["log_odds"])) == pytest.approx(risk["risk_score"], abs=1e-3)


def test_inspection_queue_is_riskiest_first(client):
    at = 17000
    queue = client.get("/inspection-queue", params={"at_hour": at, "hours": 168, "limit": 20}).json()

    scores = [item["risk_score"] for item in queue["items"]]
    assert len(scores) == 20
    assert scores == sorted(scores, reverse=True)
    assert all(at - 168 < item["finished_hour"] <= at for item in queue["items"])


def test_inspection_queue_is_empty_before_the_model_can_score(client):
    queue = client.get("/inspection-queue", params={"at_hour": 10000}).json()

    assert queue["parts_finished_in_window"] > 0
    assert queue["parts_scored_in_window"] == 0
    assert queue["items"] == []


# ============================================================
# TWIN RECORDS
# ============================================================

def test_twin_records_are_listed_once(client):
    queue = client.get("/inspection-queue", params={"hours": 24, "limit": 50}).json()

    listed = [item["part_id"] for item in queue["items"]]
    twins = [part for item in queue["items"] for part in item["twin_part_ids"]]

    assert not set(listed) & set(twins)
    assert queue["parts_covered"] == len(listed) + len(twins)

    # 280944 and 280945 have identical records (the assistant's first answer listed both)
    pair = next(item for item in queue["items"] if item["part_id"] == 280944)
    assert pair["twin_part_ids"] == [280945]


def test_twins_are_shown_once_the_part_finishes(client, service):
    row = service._row_of.get_loc(280944)
    start, end = hours(service._start[row]), hours(service._end[row])

    finished = client.get("/parts/280944", params={"at_hour": end}).json()
    in_production = client.get("/parts/280944", params={"at_hour": (start + end) / 2}).json()

    assert finished["twin_part_ids"] == [280945]
    assert in_production["status"] == "in production"
    assert in_production["twin_part_ids"] is None


# ============================================================
# LINE AND STATIONS
# ============================================================

def test_line_status_sees_the_l1_campaign(client):
    # Weeks 42-47 ran L1 only (see campaign_analysis.py); hour 7500 is week ~44.6
    status = client.get("/line/status", params={"at_hour": 7500}).json()

    assert status["campaign"] == "L1 campaign"
    assert status["l1_share_last_7_days_pct"] > 90


def test_station_ids(client):
    by_short_id = client.get("/stations/S32").json()
    by_full_id = client.get("/stations/l3_s32").json()

    assert by_short_id["station"] == by_full_id["station"] == "L3_S32"
    assert by_short_id["risk_lift"] > 5

    assert client.get("/stations/S99").status_code == 404


def test_all_stations(client):
    stations = client.get("/stations").json()["stations"]

    assert len(stations) == 52
    assert [s["station_number"] for s in stations] == sorted(s["station_number"] for s in stations)


def test_rejects_negative_hours(client):
    assert client.get("/summary", params={"at_hour": -1}).status_code == 422
