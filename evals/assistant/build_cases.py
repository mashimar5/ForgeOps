"""
Builds the eval cases for the operations assistant (src/assistant.py).

Each case is one question in a fresh session. The facts a correct answer
must state are computed here from FactoryService -- the same code behind
the MCP tools -- so they stay right when the serving data is rebuilt.

    must_say      statements a correct answer makes (checked by the judge)
    may_say       statements it may also make (shown to the judge, not graded)
    must_not_say  claims a correct answer avoids
    key_values    Ids, names or small counts that must appear in the answer
                  verbatim (checked programmatically, as whole tokens; commas
                  ignored). Decimals that may fairly be rounded are left to
                  the judge.

Writes evals/assistant/cases.jsonl and a review copy, cases.md. Run from
the project root after build_serving_data.py:

    .venv/bin/python evals/assistant/build_cases.py
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from factory_service import FactoryService, NotFound  # noqa: E402


s = FactoryService()
NOW = s.last_hour
cutoff = s.meta["model"]["training_cutoff_hour"]


def hours(tick):
    return float(tick) / 10


def row_of(part_id):
    return s._row_of.get_loc(part_id)


cases = []


def case(id, tags, question, must_say, must_not_say=(), key_values=(), why=""):
    # "May ..." items are optional: the judge sees them but doesn't grade them
    cases.append(
        {
            "id": id,
            "tags": tags,
            "question": question,
            "must_say": [item for item in must_say if not item.startswith("May ")],
            "may_say": [item for item in must_say if item.startswith("May ")],
            "must_not_say": list(must_not_say),
            "key_values": [str(v) for v in key_values],
            "why": why,
        }
    )


NO_PROBABILITY = "Calls a risk score a probability or chance of failure"


# ============================================================
# INSPECTION QUEUE
# ============================================================

queue = s.inspection_queue(hours=24, limit=20)
top = queue["items"]

case(
    "inspect-now", ["inspection"],
    "Which parts should we inspect now?",
    [
        f"Answers as of the end of the data (hour {NOW:.1f}) and says that is what \"now\" means",
        f"Lists the riskiest recently finished parts, led by {top[0]['part_id']}, {top[1]['part_id']} and {top[2]['part_id']}",
        "Says risk scores rank parts and are not probabilities",
    ],
    [NO_PROBABILITY, "Lists the same part twice"],
    [top[0]["part_id"], top[1]["part_id"], top[2]["part_id"]],
    "The question from the assistant's first live run.",
)

case(
    "inspect-top5", ["inspection"],
    "Give me the five riskiest parts that finished in the last 24 hours, with their scores.",
    [
        "Lists, in order: " + ", ".join(f"{i['part_id']} ({i['risk_score']:.2f})" for i in top[:5]),
        f"Says {queue['parts_finished_in_window']:,} parts finished in that window",
    ],
    [NO_PROBABILITY],
    [i["part_id"] for i in top[:5]],
    "Exact ranking and scores from the queue.",
)

week = s.inspection_queue(at_hour=16000, hours=168, limit=5)

case(
    "inspect-week-16000", ["inspection"],
    "As of hour 16000, which parts from the past week should quality look at first?",
    [
        "Answers as of hour 16000, for parts that finished in the 168 hours before it",
        "Leads with " + ", ".join(str(i["part_id"]) for i in week["items"][:3]),
        f"Says {week['parts_finished_in_window']:,} parts finished in that week",
    ],
    [NO_PROBABILITY, "Uses data from after hour 16000"],
    [i["part_id"] for i in week["items"][:3]],
    "A time-travel question: the at_hour and hours arguments must both be set.",
)

early = s.inspection_queue(at_hour=12000, hours=24, limit=5)

case(
    "inspect-before-model", ["inspection"],
    "Which parts should we inspect as of hour 12000?",
    [
        "Says no parts can be ranked at hour 12000: the model only scores parts that finished "
        f"after hour {cutoff:.1f}, because it trained on the earlier ones",
        f"May say {early['parts_finished_in_window']:,} parts finished in the 24 hours before",
    ],
    ["Lists parts as high-risk or gives risk scores for hour 12000", NO_PROBABILITY],
    [],
    "The tool returns an empty list; the answer must explain why, not fill the gap.",
)


# ============================================================
# PARTS
# ============================================================

pair = s.part(280944)

case(
    "part-status", ["part"],
    "What's the status of part 280944?",
    [
        f"Finished at hour {pair['finished_hour']:.1f} (entered at {pair['entered_hour']:.1f}, on {pair['entry_line']})",
        "Passed final QC",
        f"Has a risk score of {pair['risk']['risk_score']:.2f}, in the top 1%",
        "Has a twin record, 280945 (identical measurements and timestamps, most likely a repeat test)",
    ],
    [NO_PROBABILITY],
    [280945],
    "A twin pair from the first live run; the part view now explains twins.",
)

wait_row = next(
    r for r in range(len(s._part_id))
    if s._scorable[r] and s._is_part[r] and s._end[r] - s._start[r] > 1680
)
wait_id = int(s._part_id[wait_row])
midway = round((hours(s._start[wait_row]) + hours(s._end[wait_row])) / 2, 1)
in_prod = s.part(wait_id, midway)

case(
    "part-midway", ["part"],
    f"As of hour {midway}, where is part {wait_id} and what has it gone through?",
    [
        f"Still in production at hour {midway} (entered at {in_prod['entered_hour']:.1f}, about "
        f"{in_prod['hours_in_production']:.0f} hours on the line)",
        f"Has visited {len(in_prod['route_so_far'])} stations so far, the latest "
        f"{in_prod['route_so_far'][-1]['station']}",
        "Is not flagged by the batch-mate alert",
        "Has no risk score yet because it hasn't reached its last station",
    ],
    ["States when it finished, or its QC result", "Lists stations it visited after the asked hour"],
    [in_prod["route_so_far"][-1]["station"]],
    "Only the stations visited by the asked hour may appear.",
)

fail_rows = [r for r in range(len(s._part_id)) if s._scorable[r] and s._is_part[r] and s._response[r] == 1]
fail_row = max(fail_rows, key=lambda r: s._risk_score[r])
fail_id = int(s._part_id[fail_row])
pending_at = round(hours(s._end[fail_row]) + 0.5, 1)
pending = s.part(fail_id, pending_at)
assert pending["qc_result"] is None

case(
    "part-qc-pending", ["part"],
    f"As of hour {pending_at}, did part {fail_id} pass final QC?",
    [
        f"Says its QC result isn't reported yet: it reached its last station at hour "
        f"{pending['finished_hour']:.1f}, and results are reported 1 hour after",
        f"May give its risk score ({pending['risk']['risk_score']:.2f})",
    ],
    ["Says it passed or failed", NO_PROBABILITY],
    [],
    "The part did fail, but that wasn't known yet at the asked hour.",
)

case(
    "part-unknown", ["part"],
    "Tell me about part 3.",
    ["Says part 3 isn't known (the tools have no record of it)"],
    ["Gives any details about part 3"],
    [],
    "Unknown Id: the tool raises an error the assistant has to relay.",
)

future_id = top[0]["part_id"]
future = s._row_of.get_loc(future_id)
try:
    s.part(future_id, 16000)
    raise AssertionError("expected the part to be unknown at hour 16000")
except NotFound:
    pass

case(
    "part-future", ["part"],
    f"As of hour 16000, what do we know about part {future_id}?",
    [f"Says part {future_id} isn't known at hour 16000 (it hadn't entered production by then)"],
    [
        f"Gives its entry hour ({hours(s._start[future]):.1f}), route, risk score or QC result",
        "Uses data from after hour 16000",
    ],
    [],
    "The part exists later; asking without at_hour would leak the future.",
)


# ============================================================
# RISK SCORES
# ============================================================

why = s.part_risk(future_id, top=5)
strongest = why["explanation"]["top_contributions"][0]

case(
    "risk-why", ["risk"],
    f"Why is part {future_id} flagged as high risk?",
    [
        f"Risk score {why['risk_score']:.2f}, in the top 1% (percentile {why['risk_percentile']})",
        f"The biggest push toward failure is measurement {strongest['feature']} at station "
        f"{strongest['station']} (value {strongest['value']})",
        "Missing measurements at L3_S33 also push the score up (the part skipped them)",
        "These are contributions to the model's score, not proven causes",
    ],
    [NO_PROBABILITY, "Says what an anonymized measurement physically is (e.g. temperature, torque)"],
    [strongest["feature"]],
    "SHAP explanation; the measurement names are anonymized.",
)

second = top[1]

case(
    "risk-probability", ["risk"],
    f"What's the probability that part {second['part_id']} fails?",
    [
        "Says the model gives a risk score for ranking, not a probability",
        f"Gives the score ({second['risk_score']:.2f}) and that it is in the top 1% "
        f"(percentile {second['risk_percentile']})",
    ],
    ["States a probability or percentage chance that it fails"],
    [],
    "A direct request for the thing the scores are not.",
)

trained_row = next(r for r in range(len(s._part_id)) if not s._scorable[r] and s._is_part[r])
trained_id = int(s._part_id[trained_row])

case(
    "risk-trained-part", ["risk"],
    f"What's the risk score for part {trained_id}?",
    [
        f"Says there's no honest score: the model trained on part {trained_id} (it finished at hour "
        f"{hours(s._end[trained_row]):.1f}, before the training cutoff at hour {cutoff:.1f})",
    ],
    ["Gives a risk score for it"],
    [],
    "Parts the model trained on get a reason instead of a score.",
)


# ============================================================
# BATCH-MATE ALERTS
# ============================================================

case(
    "alerts-now", ["alerts"],
    "Are there any batch-mate alerts right now?",
    [f"Says there are none: no parts are in production at the end of the data (hour {NOW:.1f})"],
    ["Names flagged parts"],
    [],
    "The empty-alert case from the first live run.",
)

alerts = s.batch_alerts(at_hour=15000, limit=5)

case(
    "alerts-15000", ["alerts"],
    "Which parts in production at hour 15000 are flagged by the batch-mate alert?",
    [
        f"{alerts['flagged_parts']} of the {alerts['parts_in_production']:,} parts in production are flagged",
        "Names the most recent flags, led by " + ", ".join(str(i["part_id"]) for i in alerts["items"][:3]),
        "In forward tests flagged parts failed at about 2.6 times the average rate",
    ],
    ["Says flagged parts will fail or are defective"],
    [alerts["flagged_parts"], alerts["items"][0]["part_id"]],
    "Alerts as of a past hour with plenty of production.",
)

case(
    "alerts-trust", ["alerts"],
    "How much should we trust the batch-mate alert?",
    [
        "In forward tests flagged parts failed at about 2.6 times the average rate",
        "It flags about 1.7% of production and catches about 4.4% of failures, about 4 days before final QC",
        "Its lead time depends on L1-entry campaigns",
    ],
    ["Overstates it (e.g. says it catches most failures)"],
    ["2.6"],
    "The forward-test numbers live in the tool's note.",
)

flagged = alerts["items"][0]
flagged_view = s.part(flagged["part_id"], 15000)

case(
    "alerts-will-fail", ["alerts"],
    f"As of hour 15000, will part {flagged['part_id']} fail?",
    [
        f"Can't say yet: it's still in production ({flagged_view['hours_in_production']:.0f} hours in), "
        "so it has no risk score",
        f"It is flagged by the batch-mate alert (a batch-mate's failure became known at hour "
        f"{flagged['first_failure_known_hour']:.1f})",
        "Flagged parts failed at about 2.6 times the average rate in forward tests",
    ],
    ["Predicts a definite outcome", "Gives a probability"],
    [],
    "An in-production part: the alert is the only early signal.",
)


# ============================================================
# LINE STATUS
# ============================================================

line = s.line_status()

case(
    "line-now", ["line"],
    "Is the line running hot right now?",
    [
        f"No: the QC failure rate over the last 72 hours is {line['qc_failure_rate_last_72h_pct']}% vs "
        f"{line['qc_failure_rate_history_pct']}% historically (ratio {line['ratio_to_history']}), so no alert",
        "Production is entering on L0 only (no L1 campaign)",
        "The line monitor is an indicator, not reliable day to day",
    ],
    [],
    [],
    "Line monitor at the end of the data.",
)

line_7500 = s.line_status(7500)

case(
    "line-7500", ["line"],
    "What was happening on the line around hour 7500?",
    [
        f"An L1 campaign: {line_7500['l1_share_last_7_days_pct']:.0f}% of the last week's entries came in on L1",
        f"The line monitor was alerting: the 72-hour QC failure rate was {line_7500['ratio_to_history']} "
        "times the historical rate",
        "The monitor is an indicator, not a prediction",
    ],
    [],
    [],
    "Past line state; weeks 42-47 ran L1 only.",
)


# ============================================================
# STATIONS
# ============================================================

stations = [x for x in s.all_stations()["stations"] if x["failure_rate_pct"] is not None]
worst = max(stations, key=lambda x: x["failure_rate_pct"])

case(
    "station-highest", ["stations"],
    "Which station has the highest failure rate?",
    [
        f"{worst['station']}: {worst['failure_rate_pct']:.1f}% of QC results among parts that visited it, "
        f"{worst['risk_lift']} times the overall rate",
        "This is an association, not a cause",
    ],
    ["Says the station causes the failures"],
    [worst["station"]],
    "Needs list_stations; the answer is a station plus caveat.",
)

case(
    "station-cause", ["stations"],
    "Is station S32 causing our failures?",
    [
        f"Parts that visit L3_S32 fail at {worst['failure_rate_pct']:.1f}%, {worst['risk_lift']} times the overall "
        "rate",
        "That is an association; the data can't show S32 causes the failures",
    ],
    ["Says S32 causes the failures"],
    [],
    "Causal question the evidence can't answer.",
)


# ============================================================
# FACTORY SUMMARY AND MODEL
# ============================================================

summary = s.summary()

case(
    "summary-now", ["summary"],
    "How many parts have we made, and what's our failure rate?",
    [
        f"As of the end of the data: {summary['parts_finished']:,} parts finished",
        f"Failure rate {summary['qc_failure_rate_pct']:.2f}% (of {summary['qc_results_known']:,} QC results known)",
    ],
    [],
    [],
    "Counts and the overall rate.",
)

card = summary["model"]

case(
    "model-quality", ["summary"],
    "How good is the risk model?",
    [
        f"Tested forward in time: {card['forward_lift_mean']}x lift over random ranking on average "
        f"({card['forward_lift_range'][0]}-{card['forward_lift_range'][1]}x across 4 test periods)",
        f"Inspecting the top 1% caught about {card['forward_top_1pct_recall_mean_pct']}% of failures",
        "Scores rank parts; they are not probabilities",
    ],
    [NO_PROBABILITY, "Quotes accuracy or other numbers the tools didn't give"],
    [card["forward_lift_mean"]],
    "The model card, not the model's own guesses about itself.",
)


# ============================================================
# OUT OF SCOPE
# ============================================================

case(
    "scope-date", ["scope"],
    "On what date did part 280944 finish?",
    [
        "Says the data has no calendar dates (it is anonymized)",
        f"Gives the production hour instead: {pair['finished_hour']:.1f}",
    ],
    ["Gives a calendar date"],
    [],
    "The tools only know production hours.",
)

case(
    "scope-fix", ["scope"],
    "What should we change on the line to cut failures in half?",
    [
        "Says the tools can't establish causes or predict the effect of changes",
        "May point to associations worth investigating (e.g. L3_S32) as starting points",
    ],
    ["Recommends specific process changes as if the data supported them", "Promises an effect size"],
    [],
    "An advice question beyond what the evidence supports.",
)

case(
    "scope-cost", ["scope"],
    "What did last week's failures cost us in scrap?",
    ["Says the data has no cost information, so it can't answer"],
    ["Gives a cost figure"],
    [],
    "Data that doesn't exist.",
)


# ============================================================
# HARD CASES: COUNTING AND MATH OVER TOOL OUTPUTS
#
# The baseline's misses were details the assistant worked out
# itself from long tool outputs (a ranking, counts from a list).
# These cases ask for exactly that: counts, filters, averages and
# comparisons over the 20-part queue, 52 stations and 100 alerts,
# plus multi-step part questions. Tagged "hard" so they can be
# scored apart from the cases above.
# ============================================================

def hard(id, category, *args, **kwargs):
    case(id, [category, "hard"], *args, **kwargs)


# --- the inspection queue (20 parts by default) ---

l1_parts = [i["part_id"] for i in top if i["entry_line"] == "L1"]
hard(
    "count-l1-in-queue", "inspection",
    "Of the 20 riskiest parts that finished in the last 24 hours, how many entered on line L1, and which are they?",
    [f"{len(l1_parts)} of the 20 entered on L1: " + ", ".join(map(str, l1_parts))],
    [NO_PROBABILITY],
    l1_parts,
    "A count and a filter over the queue.",
)

top10 = [i["risk_score"] for i in top[:10]]
hard(
    "mean-score-top10", "inspection",
    "What's the average risk score of the 10 riskiest parts that finished in the last 24 hours?",
    [f"About {sum(top10) / 10:.3f} (the mean of ten scores from {min(top10)} to {max(top10)})"],
    [NO_PROBABILITY, "Presents the average as the share of these parts that will fail"],
    [],
    "Arithmetic over the queue.",
)

drops = sorted(
    ((top[k]["risk_score"] - top[k + 1]["risk_score"], k) for k in range(len(top) - 1)), reverse=True
)
assert drops[0][0] > 2 * drops[1][0], "the biggest drop should be unambiguous"
k = drops[0][1]
hard(
    "score-gap", "inspection",
    "Among the 20 riskiest parts from the last 24 hours, where is the biggest drop in risk score from one part to the next?",
    [
        f"Between the {k + 1}th part ({top[k]['part_id']}, {top[k]['risk_score']:.3f}) and the {k + 2}th "
        f"({top[k + 1]['part_id']}, {top[k + 1]['risk_score']:.3f}), a drop of about {drops[0][0]:.2f}"
    ],
    [NO_PROBABILITY],
    [top[k]["part_id"], top[k + 1]["part_id"]],
    "Scanning a ranked list for a gap.",
)

latest = max(top[:5], key=lambda i: i["finished_hour"])
assert sum(i["finished_hour"] == latest["finished_hour"] for i in top[:5]) == 1
hard(
    "latest-of-top5", "inspection",
    "Which of the 5 riskiest parts from the last 24 hours finished most recently, and at what hour?",
    [f"{latest['part_id']}, at hour {latest['finished_hour']}"],
    [],
    [latest["part_id"]],
    "A comparison across the top of the queue.",
)

week30 = s.inspection_queue(at_hour=16000, hours=168, limit=30)["items"]
assert all(i["finished_hour"] != 16000 - 24 for i in week30)
last_day = [i["part_id"] for i in week30 if i["finished_hour"] > 16000 - 24]
hard(
    "week-last-day-16000", "inspection",
    "As of hour 16000, of the 30 riskiest parts that finished in the past week, which finished in the last 24 hours?",
    [f"{len(last_day)} of them: " + ", ".join(map(str, last_day))],
    ["Uses data from after hour 16000"],
    last_day,
    "Needs a non-default limit and a time filter.",
)


# --- stations (52 rows) ---

measured = [x for x in stations if x["failure_rate_pct"] is not None]
cutoff_pct = 0.7
above = sorted((x for x in measured if x["failure_rate_pct"] > cutoff_pct), key=lambda x: -x["failure_rate_pct"])
near_miss = max((x for x in measured if x["failure_rate_pct"] <= cutoff_pct), key=lambda x: x["failure_rate_pct"])
hard(
    "stations-above-0.7", "stations",
    "Which stations have a QC failure rate above 0.7%?",
    [
        f"Exactly {len(above)}: " + ", ".join(f"{x['station']} ({x['failure_rate_pct']:.2f}%)" for x in above),
    ],
    [
        f"Includes a station at or below 0.7% (e.g. {near_miss['station']} at {near_miss['failure_rate_pct']}%)",
        "Says these stations cause the failures",
    ],
    [x["station"] for x in above],
    f"A filter over all stations, with {near_miss['station']} just below the line.",
)

l3 = [x for x in measured if x["line"] == "L3"]
lifted = [x["station"] for x in l3 if x["risk_lift"] > 1.0]
# Tiny stations (1 or 15 parts, no failures) tie at a lift of 0.0, so the
# minimum is asked among stations with a real sample
sizeable = [x for x in l3 if x["parts_visited"] >= 1000]
weakest = min(sizeable, key=lambda x: x["risk_lift"])
assert sum(x["risk_lift"] == weakest["risk_lift"] for x in sizeable) == 1
tiny = [x for x in l3 if x["parts_visited"] < 1000]
hard(
    "l3-lift", "stations",
    "How many stations on line L3 have a risk lift above 1.0? And among L3 stations with at least 1,000 parts, "
    "which has the lowest lift?",
    [
        f"{len(lifted)} of the {len(l3)} L3 stations: " + ", ".join(lifted),
        f"Lowest among stations with 1,000+ parts: {weakest['station']}, with a lift of {weakest['risk_lift']}",
        "May note the tiny stations below 1,000 parts: "
        + ", ".join(f"{x['station']} ({x['parts_visited']} part{'' if x['parts_visited'] == 1 else 's'}, lift {x['risk_lift']})" for x in tiny),
    ],
    ["Says a station causes or prevents failures"],
    [weakest["station"]],
    "A count, then a minimum with a second condition.",
)

l2 = sorted((x for x in measured if x["line"] == "L2"), key=lambda x: -x["failure_rate_pct"])
hard(
    "l2-ranking", "stations",
    "Rank the L2 stations by failure rate.",
    [", then ".join(f"{x['station']} ({x['failure_rate_pct']:.3f}%)" for x in l2)],
    ["Says a station causes the failures"],
    [x["station"] for x in l2],
    "Sorting a small subset correctly.",
)

busiest, runner_up = sorted(stations, key=lambda x: -x["parts_visited"])[:2]
hard(
    "most-visited", "stations",
    "Which station do the most parts pass through, and how many?",
    [
        f"{busiest['station']}, with {busiest['parts_visited']:,} parts",
        f"May note it is just ahead of {runner_up['station']} ({runner_up['parts_visited']:,})",
    ],
    [],
    [busiest["station"], f"{busiest['parts_visited']:,}"],
    f"A maximum over all stations; the top two are only {busiest['parts_visited'] - runner_up['parts_visited']:,} apart.",
)

l1_stations = [x for x in measured if x["line"] == "L1"]
hard(
    "line-l1-rate", "stations",
    "What's the failure rate on line L1?",
    [
        "Says the tools give failure rates per station, not per line",
        "Gives L1's stations: " + ", ".join(f"{x['station']} {x['failure_rate_pct']:.2f}%" for x in l1_stations),
    ],
    ["States one failure rate for line L1 made by averaging or adding station rates"],
    [],
    "Asks for a number the tools don't give; combining station rates would be wrong.",
)


# --- batch-mate alerts (140 flagged at hour 15000; the tool lists 100 at most) ---

all_flags = s.batch_alerts(at_hour=15000, limit=100)
listed = all_flags["items"]
unlisted = all_flags["flagged_parts"] - len(listed)
assert unlisted > 0

# The unlisted parts can still be worked out: they were flagged no later than
# the last listed part, and alert lists from earlier hours show them
everything = s.batch_alerts(at_hour=15000, limit=all_flags["flagged_parts"])["items"]
hidden = everything[len(listed):]
last_listed_flag = listed[-1]["first_failure_known_hour"]
assert len(hidden) == unlisted and all(i["first_failure_known_hour"] <= last_listed_flag for i in hidden)

l1_flags = [i["part_id"] for i in listed if i["entry_line"] == "L1"]
assert not any(i["entry_line"] == "L1" for i in hidden)
hard(
    "alerts-l1-count", "alerts",
    "At hour 15000, how many of the parts flagged by the batch-mate alert entered on line L1?",
    [
        f"Among the {len(listed)} most recently flagged parts the tool can list, {len(l1_flags)} entered on L1 "
        f"({', '.join(map(str, l1_flags))})",
        f"The other {unlisted} of the {all_flags['flagged_parts']} flagged parts aren't in that list. Either says "
        f"the full count isn't known, or works it out correctly: none of the {unlisted} entered on L1 (alert "
        f"lists from earlier hours show them), so {len(l1_flags)} of {all_flags['flagged_parts']} in all",
    ],
    [f"Gives a count for all {all_flags['flagged_parts']} flagged parts without noting that {unlisted} weren't listed"],
    l1_flags,
    "A count over the longest list a tool returns, which still isn't the whole set; "
    "the rest can be found from earlier hours.",
)

waited = sum(i["hours_in_production"] > 1000 for i in listed)
assert 15000 - last_listed_flag > 1000
hard(
    "alerts-long-wait", "alerts",
    "At hour 15000, how many flagged parts had been in production for more than 1,000 hours?",
    [
        f"{waited} of the {len(listed)} flagged parts the tool can list",
        f"The other {unlisted} flagged parts aren't listed",
        f"May work out that the other {unlisted} were flagged by hour {last_listed_flag}, so they had all been "
        f"in production for more than 1,000 hours: {waited + unlisted} of {all_flags['flagged_parts']} in all",
    ],
    [f"Gives a count for all {all_flags['flagged_parts']} flagged parts without noting that {unlisted} weren't listed"],
    [waited],
    "A threshold count over the alert list.",
)

hard(
    "alerts-share", "alerts",
    "What share of the parts in production at hour 15000 was flagged by the batch-mate alert?",
    [
        f"{all_flags['flagged_parts']} of {all_flags['parts_in_production']:,} parts, about "
        f"{all_flags['flagged_parts'] / all_flags['parts_in_production'] * 100:.1f}%"
    ],
    ["Says flagged parts will fail"],
    [all_flags["flagged_parts"], f"{all_flags['parts_in_production']:,}"],
    "A ratio of two numbers from one tool call.",
)


# --- counts across time ---

before, after = s.summary(17000), s.summary(17100)
hard(
    "finished-window", "summary",
    "How many parts finished between hour 17000 and hour 17100?",
    [
        f"{after['parts_finished'] - before['parts_finished']:,} ({before['parts_finished']:,} had finished by hour "
        f"17000, {after['parts_finished']:,} by hour 17100)"
    ],
    [],
    [f"{after['parts_finished'] - before['parts_finished']:,}"],
    "A difference between two as-of answers.",
)

then, later = s.summary(15000), s.summary(16000)
change = later["parts_in_production"] - then["parts_in_production"]
hard(
    "production-change", "summary",
    "How did the number of parts in production change from hour 15000 to hour 16000?",
    [f"From {then['parts_in_production']:,} to {later['parts_in_production']:,}, {'up' if change > 0 else 'down'} {abs(change):,}"],
    [],
    [f"{then['parts_in_production']:,}", f"{later['parts_in_production']:,}", f"{abs(change):,}"],
    "Two as-of answers and their difference.",
)

entries = s.line_status(7500)["parts_entered_last_7_days"]
hard(
    "entries-week-7500", "line",
    "In the week before hour 7500, how many parts entered production on each entry line?",
    [f"{entries['L1']:,} on L1 and {entries['L0']:,} on L0"],
    [],
    [f"{entries['L1']:,}"],
    "Counts from the line status, including a zero.",
)

line_7300, line_7700 = s.line_status(7300), s.line_status(7700)


def monitor(line):
    return (
        f"{line['qc_failure_rate_last_72h_pct']}% vs {line['qc_failure_rate_history_pct']}% historically "
        f"({line['ratio_to_history']}x, {'an alert' if line['alert'] else 'no alert'})"
    )


hard(
    "line-change", "line",
    "How did the line's 72-hour QC failure rate change between hour 7300 and hour 7700?",
    [f"Hour 7300: {monitor(line_7300)}", f"Hour 7700: {monitor(line_7700)}"],
    ["Treats the line monitor as a prediction"],
    [],
    "Two as-of answers compared.",
)


# --- multi-step part questions ---

route = s.part(future_id)
first_l3 = next(step for step in route["route_so_far"] if step["station"].startswith("L3"))
total = route["finished_hour"] - route["entered_hour"]
hard(
    "part-time-split", "part",
    f"How long was part {future_id} in production, and how much of that time came before it reached line 3?",
    [
        f"{total:.1f} hours in total (hour {route['entered_hour']} to {route['finished_hour']})",
        f"{first_l3['hours_after_entry']} hours passed before it reached L3 ({first_l3['station']} at hour "
        f"{first_l3['hour']}), so about {total - first_l3['hours_after_entry']:.1f} hours on L3",
    ],
    [],
    [],
    "Arithmetic over a part's route.",
)

other = s.part(second["part_id"])
assert other["entered_hour"] == route["entered_hour"] and other["finished_hour"] == route["finished_hour"]
hard(
    "compare-parts", "part",
    f"Compare parts {future_id} and {second['part_id']}: which entered production first, which finished first, "
    "and which has the higher risk score?",
    [
        f"Both entered at hour {route['entered_hour']} and finished at hour {route['finished_hour']}, so neither was first",
        f"{future_id} has the higher score ({route['risk']['risk_score']:.2f} vs {other['risk']['risk_score']:.2f})",
    ],
    ["Says one entered or finished before the other", NO_PROBABILITY],
    [],
    "A comparison where the honest answer is a tie.",
)

contributions = s.part_risk(future_id, top=10)["explanation"]["top_contributions"]
skipped = [c for c in contributions if c["station"] == "L3_S33" and c["value"] is None]
hard(
    "shap-sum", "risk",
    f"In total, how much do the missing L3_S33 measurements add to part {future_id}'s risk, looking at its top 10 "
    "contributions?",
    [f"{len(skipped)} missing L3_S33 measurements add about +{sum(c['contribution'] for c in skipped):.2f} log-odds in total"],
    ["Converts the log-odds into a probability of failure", "Says what an anonymized measurement physically is"],
    [],
    "A sum over part of a SHAP explanation.",
)


# --- a number the data can't give ---

hard(
    "forecast-failures", "scope",
    "How many failures should we expect next week?",
    [
        "Says the tools can't forecast failures",
        "May give recent or historical failure rates as context",
    ],
    ["Gives a predicted number of failures for next week"],
    [],
    "Tempts a derived forecast from rates.",
)


# ============================================================
# WRITE
# ============================================================

assert len({c["id"] for c in cases}) == len(cases)

with open(HERE / "cases.jsonl", "w") as f:
    for c in cases:
        f.write(json.dumps(c) + "\n")

lines = [
    "# Assistant eval cases",
    "",
    f"{len(cases)} single questions, each asked in a fresh session of `src/assistant.py`. "
    "Generated by `evals/assistant/build_cases.py` from the serving data; \"now\" is the end "
    f"of the data (hour {NOW:.1f}).",
    "",
    "| id | category | question |",
    "|---|---|---|",
    *[f"| {c['id']} | {c['tags'][0]} | {c['question']} |" for c in cases],
    "",
]

for c in cases:
    lines += [
        f"## {c['id']} ({c['tags'][0]})",
        "",
        "````text",
        c["question"],
        "````",
        "",
        f"*Why it's here:* {c['why']}",
        "",
        "**A correct answer says:**",
        "",
        *[f"- {item}" for item in c["must_say"]],
        "",
    ]
    if c["may_say"]:
        lines += ["**It may also (not graded):**", "", *[f"- {item}" for item in c["may_say"]], ""]
    if c["must_not_say"]:
        lines += ["**It must not:**", "", *[f"- {item}" for item in c["must_not_say"]], ""]
    if c["key_values"]:
        lines += ["**Must appear verbatim:** " + ", ".join(f"`{v}`" for v in c["key_values"]), ""]

(HERE / "cases.md").write_text("\n".join(lines))

print(f"Wrote {len(cases)} cases to evals/assistant/cases.jsonl and cases.md")
