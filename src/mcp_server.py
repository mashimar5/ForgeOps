"""
AI-ASSISTANT TOOLS -- an MCP server over the factory evidence

Exposes the evidence layer (factory_service.py) as MCP tools, so any MCP
client -- Claude Code, Claude Desktop, or assistant.py -- can answer
operations questions with grounded, time-aware evidence. Every tool answers
"as of" a production hour and only uses what was known at that hour.

The tools are read-only. Build the serving data first:

    .venv/bin/python src/build_serving_data.py

Then register the server with an MCP client, e.g. Claude Code (run from
the project root):

    claude mcp add forgeops -- "$PWD/.venv/bin/python" "$PWD/src/mcp_server.py"

or run it directly over stdio:

    .venv/bin/python src/mcp_server.py
"""

from typing import Annotated, Any

from pydantic import Field
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from factory_service import FactoryService, NotFound


INSTRUCTIONS = """\
Evidence about a Bosch production line: anonymized Kaggle data covering about two \
years and 1.18M parts, each with measurements from up to 52 stations on 4 lines and \
a pass/fail result from final quality control (about 0.58% fail).

Time: every tool takes an optional at_hour, the production hour to answer as of \
(hours since the first timestamp, 0 to about 17185; the data has no calendar dates). \
Omit it to ask about the end of the data, which stands in for "now". Week w starts \
at hour 168 * w. Answers only use what was known at that hour: stations a part had \
already visited, and QC results reported 1 hour after a part's last station.

Reading the evidence:
- Risk scores rank finished parts for final-QC inspection; they are not probabilities. \
The model only scores parts that finished after hour 13565.6, because it trained on \
the earlier ones. In forward tests, inspecting its top 1% caught about 13% of failures.
- Batch-mate alerts flag parts that failed at about 2.6x the average rate in forward \
tests, about 4 days before their own final QC.
- The line monitor indicates long high-failure stretches, with a lag; it is not \
reliable day to day.
- Station failure rates are associations, not causes.
- About 2% of parts have more than one record (twin records: identical measurements \
and timestamps, most likely repeat tests of the part). A repeat record only appears \
once the part's QC result is reported. Part counts and lists count each part once; QC \
result counts and failure rates include every record.
- Measurement names are anonymized (L3_S32_F3850 = line 3, station 32, feature 3850). \
Don't guess what they physically measure.
"""

server = MCPServer(
    name="forgeops",
    title="ForgeOps factory evidence",
    instructions=INSTRUCTIONS,
    version="0.2.0",
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

AtHour = Annotated[
    float | None,
    Field(
        ge=0,
        description=(
            "Production hour to answer as of (hours since the first timestamp, "
            "0 to about 17185). Omit for the end of the data."
        ),
    ),
]

PartId = Annotated[int, Field(description="Part Id from the Bosch data, e.g. 272133")]


# The service loads ~1.2 GB of serving data, so load it once, at startup
_service = None


def service():
    global _service

    if _service is None:
        _service = FactoryService()

    return _service


def use_service(loaded):
    """Serve an already-loaded FactoryService (the API runs this server in-process)."""

    global _service
    _service = loaded


# ============================================================
# TOOLS
# ============================================================

@server.tool(annotations=READ_ONLY)
def get_factory_summary(at_hour: AtHour = None) -> dict[str, Any]:
    """Overview of the factory at a production hour: parts entered, in production
    and finished, QC results reported so far and their failure rate, plus the risk
    model's card (what it is, how it was evaluated, its forward-in-time accuracy).

    Call this first for general questions ("how is the line doing?", "how good is
    the model?") or to find the data's time range.
    """

    return service().summary(at_hour)


@server.tool(annotations=READ_ONLY)
def get_line_status(at_hour: AtHour = None) -> dict[str, Any]:
    """The line monitor and production campaign at a production hour: the QC failure
    rate over the last 72 hours against its history (with an alert flag), which entry
    line fed production over the last 7 days (L0 only, or an L1 campaign), and how
    many parts are in production.

    Call this for questions about whether the line is running hot, recent failure
    trends, or which campaign is running.
    """

    return service().line_status(at_hour)


@server.tool(annotations=READ_ONLY)
def get_part(part_id: PartId, at_hour: AtHour = None) -> dict[str, Any]:
    """One part's history at a production hour: entry line, route so far (stations
    with hours), status (in production or finished), QC result once reported,
    batch-mate alert status while in production (batch size and batch-mates count
    parts, each once), twin records once the QC result
    is reported (other records with identical measurements and timestamps, most
    likely repeat tests of the same part; the lowest Id is the first test), and
    whether a risk score exists.

    Call this when the user asks about a specific part Id. Fails if the part had
    not entered production by that hour.
    """

    try:
        return service().part(part_id, at_hour)
    except NotFound as error:
        raise ToolError(str(error))


@server.tool(annotations=READ_ONLY)
def explain_part_risk(
    part_id: PartId,
    at_hour: AtHour = None,
    top: Annotated[int, Field(ge=1, le=30, description="How many contributions to return")] = 8,
) -> dict[str, Any]:
    """A finished part's risk score, its percentile among parts already scored, and
    the measurements that pushed the score up or down most (SHAP contributions in
    log-odds: positive pushes toward failure; a missing value means the part skipped
    that station or measurement).

    Call this when the user asks why a part was flagged or how risky it is. If no
    score is available, the reason says why (the model trained on the part, or the
    part hasn't finished).
    """

    try:
        return service().part_risk(part_id, at_hour, top)
    except NotFound as error:
        raise ToolError(str(error))


@server.tool(annotations=READ_ONLY)
def get_inspection_queue(
    at_hour: AtHour = None,
    hours: Annotated[float, Field(gt=0, le=336, description="Look back this many hours")] = 24,
    limit: Annotated[int, Field(ge=1, le=100, description="Most parts to return")] = 20,
) -> dict[str, Any]:
    """Parts that reached their last station in the last `hours`, riskiest first,
    with risk scores and percentiles. Each part is listed once (repeat test records
    are left out).

    Call this for "which parts should we inspect?" or "what are the highest-risk
    parts right now?". Only parts that finished after hour 13565.6 can be scored.
    """

    return service().inspection_queue(at_hour, hours, limit)


@server.tool(annotations=READ_ONLY)
def get_batch_mate_alerts(
    at_hour: AtHour = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Most parts to return")] = 20,
) -> dict[str, Any]:
    """Parts still in production whose entry batch-mate (a part that entered in the
    same 6-minute tick) has already failed final QC, most recent flags first.

    Call this for early-warning questions: "which parts in production are at risk?",
    "any alerts?". In forward tests flagged parts failed at about 2.6x the average
    rate, about 4 days before their own final QC.
    """

    return service().batch_alerts(at_hour, limit)


@server.tool(annotations=READ_ONLY)
def get_station(
    station_id: Annotated[str, Field(description="Station, e.g. 'L3_S32' or 'S32'")],
    at_hour: AtHour = None,
) -> dict[str, Any]:
    """One station's metrics at a production hour: parts visited, failure rate among
    parts with reported QC results, risk lift against the overall rate, median hours
    after entry and until the part's last station, and how many numeric measurements
    it records.

    Call this for questions about a specific station.
    """

    try:
        return service().station(station_id, at_hour)
    except NotFound as error:
        raise ToolError(str(error))


@server.tool(annotations=READ_ONLY)
def list_stations(at_hour: AtHour = None) -> dict[str, Any]:
    """Metrics for all 52 stations at once (the same fields as get_station).

    Call this to compare stations or find the riskiest ones. It is a larger
    response, so prefer get_station for a single station.
    """

    return service().all_stations(at_hour)


if __name__ == "__main__":
    service()  # load the data before the client's first call
    server.run("stdio")
