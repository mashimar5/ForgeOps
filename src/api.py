"""
API STEP 3 -- the HTTP API (first version)

A thin FastAPI layer over factory_service.py. Every endpoint answers "as
of" a production hour and only uses what was known then.

Build the serving data once, then run from the project root:

    .venv/bin/python src/build_serving_data.py
    .venv/bin/uvicorn api:app --app-dir src

Interactive docs: http://127.0.0.1:8000/docs
Dashboard, once built (dashboard/README.md): http://127.0.0.1:8000/dashboard/

The AI analyst endpoints (/analyst/...) call the Claude API with the
credentials of the shell that starts the server (`ant auth login`, or
ANTHROPIC_API_KEY); each question is billed to that account.
"""

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import assistant
import mcp_server
from analyst import Analyst, Busy, Mismatch
from factory_service import FactoryService, NotFound
from line_map import TwinRuns
from production_data import PROJECT_ROOT

DASHBOARD_DIR = PROJECT_ROOT / "dashboard" / "dist"


# ============================================================
# RESPONSE SCHEMAS
# ============================================================

class ModelCard(BaseModel):
    description: str
    evaluation: str
    metric_definitions: str
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
    note: str


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


class Week(BaseModel):
    week: int
    start_hour: float
    hours_covered: float
    qc_results: int
    qc_failures: int
    qc_failure_rate_pct: float | None
    parts_entered: dict[str, int]


class LineHistory(BaseModel):
    at_hour: float
    weeks: list[Week]
    note: str


class PlantLine(BaseModel):
    code: str
    name: str
    behaviour: str


class PlantStation(BaseModel):
    station: str
    line: str
    cell: str | None
    op: str
    label: str


class PlantProduct(BaseModel):
    id: str
    name: str
    route: str


class Plant(BaseModel):
    theme: str
    lines: list[PlantLine]
    stations: list[PlantStation]
    products: list[PlantProduct]
    note: str


class ProductMetrics(BaseModel):
    id: str
    name: str
    route: str
    parts_entered: int
    parts_in_production: int
    parts_finished: int
    qc_results_known: int
    qc_failure_rate_pct: float | None
    median_hours_in_production: float | None


class Products(BaseModel):
    at_hour: float
    products: list[ProductMetrics]
    note: str


class MapStation(BaseModel):
    station: str
    line: str
    parts: int


class LineMap(BaseModel):
    source: str
    at_hour: float
    in_production: int
    stations: list[MapStation]
    waiting_for_line3: dict[str, int]
    line3_serving: str
    line3_started_last_hour: dict[str, int]
    entered_last_hour: dict[str, int]
    finished_last_hour: int
    qc_reported_last_24h: int
    qc_failed_last_24h: int
    note: str


class TwinScenario(BaseModel):
    id: str
    label: str
    description: str
    first_hour: float
    last_hour: float


class TwinScenarios(BaseModel):
    scenarios: list[TwinScenario]
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
    product: str
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
    product: str
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
    items: list[QueueItem]
    note: str


class BatchAlert(BaseModel):
    part_id: int
    product: str
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


class AnalystStatus(BaseModel):
    available: bool
    reason: str | None
    model: str
    effort: str
    max_tool_rounds: int
    tools: list[str]
    price_per_mtok: dict[str, float]
    note: str


class AnalystQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    at_hour: float | None = Field(
        None, ge=0, description="The hour to answer as of; it stays fixed for the conversation. Defaults to the end of the data."
    )
    conversation_id: str | None = Field(None, description="Continue this conversation; omit to start a new one.")


# ============================================================
# APP
# ============================================================

@asynccontextmanager
async def lifespan(app):
    app.state.service = FactoryService()
    app.state.twin = TwinRuns()

    # The AI analyst: the assistant's MCP tools, connected in-process to the data loaded above
    mcp_server.use_service(app.state.service)
    async with assistant.connect(mcp_server.server) as (tools, system):
        app.state.analyst = Analyst(tools, system, app.state.service.last_hour)
        yield


app = FastAPI(
    title="ForgeOps API",
    version="0.4.0",
    description=(
        "Evidence about the Bosch production line, answered **as of** a production hour "
        "(hours since the first timestamp in the data; the data is anonymized, so there are "
        "no calendar dates). Each answer only uses what was known at that hour: stations a "
        "part had visited and QC results already reported, 1 hour after a part's last "
        "station. Twin records (identical measurements and timestamps, most likely repeat "
        "tests of one part) appear only once the part's QC result is reported. The risk "
        "model only scores parts it never trained on. Risk scores rank parts for "
        "inspection; they are not calibrated probabilities. The AI analyst (/analyst/ask) answers "
        "questions with Claude from the same evidence, and each question is billed to the Claude API "
        "account the server is signed in with."
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


def with_product(service, item):
    """Add the illustrative product name (plant_names.py). The API only: the assistant's tools keep the codes."""

    return {**item, "product": service.product_name(item["part_id"])}


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


@app.get("/line/history", response_model=LineHistory, summary="Week by week: QC results reported and parts entered")
def line_history(service: Service, at_hour: AtHour = None):
    return service.line_history(at_hour)


@app.get("/plant", response_model=Plant, summary="Illustrative names for the lines, stations and products")
def plant(service: Service):
    return service.plant()


@app.get("/products", response_model=Products, summary="Parts and QC results by product (route family)")
def products(service: Service, at_hour: AtHour = None):
    return service.products(at_hour)


@app.get("/line/map", response_model=LineMap, summary="Where the parts in production are: each station and the queue for line 3")
def line_map(service: Service, at_hour: AtHour = None):
    return service.line_map(at_hour)


TWIN_NOTE = (
    "Saved runs of the digital twin (src/digital_twin.py): a flow simulation calibrated on the real "
    "line, replaying weeks 62-98. Simulated parts, not real ones; it reproduces how parts move and how "
    "often they fail, not why. Write them with src/twin_scenarios.py."
)


@app.get("/twin/scenarios", response_model=TwinScenarios, summary="The digital twin's saved runs")
def twin_scenarios(request: Request):
    return {"scenarios": request.app.state.twin.scenarios, "note": TWIN_NOTE}


@app.get("/twin/map/{scenario}", response_model=LineMap, summary="Line map of a digital-twin run")
def twin_map(
    request: Request,
    service: Service,
    scenario: Annotated[str, Path(description="A run id from /twin/scenarios")],
    at_hour: AtHour = None,
):
    info, result = request.app.state.twin.snapshot(scenario, at_hour, service.stations)
    if info is None:
        raise HTTPException(status_code=404, detail=f"No saved twin run {scenario!r}; see /twin/scenarios.")
    if not info["first_hour"] <= result["at_hour"] <= info["last_hour"]:
        raise HTTPException(
            status_code=404,
            detail=f"Twin run {scenario!r} covers hours {info['first_hour']:.0f}-{info['last_hour']:.0f}.",
        )
    return {"source": f"twin:{scenario}", **result}


@app.get("/parts/{part_id}", response_model=Part, summary="A part's route, status and risk so far")
def part(service: Service, part_id: PartId, at_hour: AtHour = None):
    try:
        return with_product(service, service.part(part_id, at_hour))
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
    queue = service.inspection_queue(at_hour, hours, limit)
    queue["items"] = [with_product(service, item) for item in queue["items"]]
    return queue


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
    alerts = service.batch_alerts(at_hour, limit)
    alerts["items"] = [with_product(service, item) for item in alerts["items"]]
    return alerts


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


# ============================================================
# AI ANALYST
#
# The assistant (assistant.py) behind the dashboard's AI Analyst page; see
# analyst.py. Each question calls the Claude API and is billed to the
# account the server is signed in with.
# ============================================================

@app.get("/analyst/status", response_model=AnalystStatus, summary="Whether the AI analyst can answer, and how")
async def analyst_status(request: Request):
    return await request.app.state.analyst.status()


@app.post(
    "/analyst/ask",
    summary="Ask the AI analyst; the answer streams back as server-sent events",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "Events: start, text (as it is written), tool_call, tool_result, then done (the answer, "
                "token usage, estimated cost) or error. Each question is billed to the Claude API "
                "account the server is signed in with."
            ),
        },
        404: {"description": "The conversation has ended"},
        409: {"description": "Busy, or a follow-up as of a different hour"},
    },
)
async def analyst_ask(request: Request, body: AnalystQuestion):
    analyst = request.app.state.analyst
    try:
        conversation = analyst.open(body.conversation_id, body.at_hour)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except (Busy, Mismatch) as error:
        raise HTTPException(status_code=409, detail=str(error))

    return StreamingResponse(
        analyst.events(conversation, body.question.strip()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# DASHBOARD
#
# The built dashboard (dashboard/, `npm run build`) is served from the
# same origin as the API; in development Vite's dev server proxies to it.
# ============================================================

if DASHBOARD_DIR.is_dir():
    app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
