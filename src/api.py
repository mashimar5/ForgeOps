"""
API STEP 3 -- the HTTP API (first version)

A thin FastAPI layer over factory_service.py. Every endpoint answers "as
of" a production hour and only uses what was known then.

Build the serving data once, then run from the project root:

    .venv/bin/python src/build_serving_data.py
    .venv/bin/uvicorn api:app --app-dir src

Interactive docs: http://127.0.0.1:8000/docs
"""

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from pydantic import BaseModel

from factory_service import FactoryService, NotFound


# ============================================================
# RESPONSE SCHEMAS
# ============================================================

class ModelCard(BaseModel):
    description: str
    evaluation: str
    training_parts: int
    training_cutoff_hour: float
    scorable_parts: int
    forward_lift_mean: float
    forward_lift_range: list[float]
    forward_top_1pct_recall_mean_pct: float
    forward_top_1pct_recall_range_pct: list[float]


class Summary(BaseModel):
    at_hour: float
    data_first_hour: float
    data_last_hour: float
    parts_entered: int
    parts_in_production: int
    parts_finished: int
    qc_results_known: int
    qc_failure_rate_pct: float | None
    model: ModelCard


class LineStatus(BaseModel):
    at_hour: float
    qc_results_last_72h: int
    qc_failure_rate_last_72h_pct: float | None
    qc_failure_rate_history_pct: float | None
    ratio_to_history: float | None
    alert: bool
    parts_entered_last_7_days: dict[str, int]
    l1_share_last_7_days_pct: float | None
    campaign: str
    parts_in_production: int
    note: str


class RouteStep(BaseModel):
    station: str
    hour: float
    hours_after_entry: float


class BatchMates(BaseModel):
    flagged: bool
    batch_size: int
    batch_mates_failed_known: int
    batch_mates_passed_known: int
    first_failure_known_hour: float | None


class RiskSummary(BaseModel):
    available: bool
    reason: str | None = None
    risk_score: float | None = None
    risk_percentile: float | None = None
    top_1_percent: bool | None = None


class Part(BaseModel):
    part_id: int
    at_hour: float
    status: Literal["in production", "finished"]
    entry_line: str
    entered_hour: float
    finished_hour: float | None
    hours_in_production: float
    route_so_far: list[RouteStep]
    qc_result: Literal["passed", "failed"] | None
    batch_mates: BatchMates | None
    twin_part_ids: list[int] | None
    risk: RiskSummary


class Contribution(BaseModel):
    feature: str
    station: str
    value: float | None
    contribution: float


class Explanation(BaseModel):
    base_log_odds: float
    log_odds: float
    top_contributions: list[Contribution]
    note: str


class PartRisk(RiskSummary):
    part_id: int
    at_hour: float
    explanation: Explanation | None
    note: str


class QueueItem(BaseModel):
    part_id: int
    twin_part_ids: list[int]
    entry_line: str
    finished_hour: float
    risk_score: float
    risk_percentile: float | None
    top_1_percent: bool | None


class InspectionQueue(BaseModel):
    at_hour: float
    window_hours: float
    parts_finished_in_window: int
    parts_scored_in_window: int
    parts_covered: int
    items: list[QueueItem]
    note: str


class BatchAlert(BaseModel):
    part_id: int
    entry_line: str
    entered_hour: float
    hours_in_production: float
    first_failure_known_hour: float
    hours_since_flag: float
    batch_size: int
    stations_visited_so_far: int
    last_station_so_far: str | None


class BatchAlerts(BaseModel):
    at_hour: float
    parts_in_production: int
    flagged_parts: int
    items: list[BatchAlert]
    note: str


class StationMetrics(BaseModel):
    station: str
    line: str
    station_number: int
    numeric_features: int
    parts_visited: int
    qc_results_known: int
    failure_rate_pct: float | None
    risk_lift: float | None
    median_hours_after_entry: float | None
    median_hours_until_last_station: float | None


class Station(StationMetrics):
    at_hour: float


class Stations(BaseModel):
    at_hour: float
    stations: list[StationMetrics]


# ============================================================
# APP
# ============================================================

@asynccontextmanager
async def lifespan(app):
    app.state.service = FactoryService()
    yield


app = FastAPI(
    title="ForgeOps API",
    version="0.1.0",
    description=(
        "Evidence about the Bosch production line, answered **as of** a production hour "
        "(hours since the first timestamp in the data; the data is anonymized, so there are "
        "no calendar dates). Each answer only uses what was known at that hour: stations a "
        "part had visited and QC results already reported, 1 hour after a part's last "
        "station. The risk model only scores parts it never trained on. Risk scores rank "
        "parts for inspection; they are not calibrated probabilities."
    ),
    lifespan=lifespan,
)


def get_service(request: Request) -> FactoryService:
    return request.app.state.service


Service = Annotated[FactoryService, Depends(get_service)]

AtHour = Annotated[
    float | None,
    Query(ge=0, description="Production hour to answer as of. Defaults to the end of the data."),
]

PartId = Annotated[int, Path(description="Part Id from the Bosch data")]


def not_found(error):
    return HTTPException(status_code=404, detail=str(error))


# ============================================================
# ROUTES
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/summary", response_model=Summary, summary="Factory summary and model card")
def summary(service: Service, at_hour: AtHour = None):
    return service.summary(at_hour)


@app.get("/line/status", response_model=LineStatus, summary="Line monitor and current entry-line campaign")
def line_status(service: Service, at_hour: AtHour = None):
    return service.line_status(at_hour)


@app.get("/parts/{part_id}", response_model=Part, summary="A part's route, status and risk so far")
def part(service: Service, part_id: PartId, at_hour: AtHour = None):
    try:
        return service.part(part_id, at_hour)
    except NotFound as error:
        raise not_found(error)


@app.get("/parts/{part_id}/risk", response_model=PartRisk, summary="A part's risk score with its SHAP explanation")
def part_risk(
    service: Service,
    part_id: PartId,
    at_hour: AtHour = None,
    top: Annotated[int, Query(ge=1, le=50, description="How many contributions to return")] = 10,
):
    try:
        return service.part_risk(part_id, at_hour, top)
    except NotFound as error:
        raise not_found(error)


@app.get(
    "/inspection-queue",
    response_model=InspectionQueue,
    summary="Parts that just finished, riskiest first",
)
def inspection_queue(
    service: Service,
    at_hour: AtHour = None,
    hours: Annotated[float, Query(gt=0, le=336, description="Look back this many hours")] = 24,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
):
    return service.inspection_queue(at_hour, hours, limit)


@app.get(
    "/alerts/batch-mates",
    response_model=BatchAlerts,
    summary="Parts in production whose batch-mate already failed final QC",
)
def batch_alerts(
    service: Service,
    at_hour: AtHour = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    return service.batch_alerts(at_hour, limit)


@app.get("/stations", response_model=Stations, summary="Metrics for every station")
def stations(service: Service, at_hour: AtHour = None):
    return service.all_stations(at_hour)


@app.get("/stations/{station_id}", response_model=Station, summary="Metrics for one station")
def station(
    service: Service,
    station_id: Annotated[str, Path(description="Station, e.g. 'L3_S32' or 'S32'")],
    at_hour: AtHour = None,
):
    try:
        return service.station(station_id, at_hour)
    except NotFound as error:
        raise not_found(error)
