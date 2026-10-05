import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    classification_report,
    average_precision_score,
    matthews_corrcoef
)

# Start with a manageable sample
df = pd.read_csv("train_numeric.csv", nrows=10000)

# Dictionary:
# (line, station) -> list of feature columns belonging to that station
station_columns = {}

for col in df.columns:
    if col.startswith("L"):
        line, station, feature = col.split("_")

        key = (line, station)

        if key not in station_columns:
            station_columns[key] = []

        station_columns[key].append(col)


# Build station summary
station_summary = []

for (line, station), cols in station_columns.items():

    # True if this part has at least one measurement at this station
    visited = df[cols].notna().any(axis=1)

    parts_visited = visited.sum()

    # Among parts that visited this station,
    # count how many eventually failed QC
    failed = ((df["Response"] == 1) & visited).sum()

    # Avoid dividing by zero
    if parts_visited > 0:
        failure_rate = failed / parts_visited * 100
    else:
        failure_rate = 0

    station_summary.append({
        "Line": line,
        "Station": station,
        "Features": len(cols),
        "Parts Visited": parts_visited,
        "Failed": failed,
        "Failure Rate (%)": failure_rate
    })

baseline_failure_rate = df["Response"].mean() * 100

print("Baseline failure rate:", baseline_failure_rate)

station_summary = pd.DataFrame(station_summary)
station_summary["Risk Lift"] = (
    station_summary["Failure Rate (%)"] / baseline_failure_rate
)

# print(
#     station_summary.sort_values(
#         "Risk Lift",
#         ascending=False
#     )[
#         [
#             "Line",
#             "Station",
#             "Parts Visited",
#             "Failed",
#             "Failure Rate (%)",
#             "Risk Lift"
#         ]
#     ].head(15)
# )

reliable_stations = station_summary[
    station_summary["Parts Visited"] >= 500
]

visit_matrix = pd.DataFrame(index=df.index)

for (line, station), cols in station_columns.items():
    visit_matrix[f"{line}_{station}"] = df[cols].notna().any(axis=1)

visit_matrix["Id"] = df["Id"]
visit_matrix["Response"] = df["Response"]

print(visit_matrix.head())

print(
    reliable_stations.sort_values(
        "Failure Rate (%)",
        ascending=False
    ).reset_index(drop=True)
)

station_cols = [
    col for col in visit_matrix.columns
    if col.startswith("L")
]

def get_route(row):
    return tuple(
        station
        for station in station_cols
        if row[station]
    )

visit_matrix["Route"] = visit_matrix.apply(
    get_route,
    axis=1
)

print(
    visit_matrix[
        ["Id", "Route", "Response"]
    ].head()
)

route_summary = (
    visit_matrix
    .groupby("Route")
    .agg(
        Parts=("Id", "count"),
        Failed=("Response", "sum"),
        Failure_Rate=("Response", "mean")
    )
    .reset_index()
)

route_summary["Failure_Rate"] *= 100

route_summary = route_summary.sort_values(
    "Parts",
    ascending=False
).reset_index(drop=True)

print(route_summary.head(20))


s26_cols = station_columns[("L2", "S26")]

print(s26_cols)

s26_data = df[
    ["Response"] + s26_cols
]

comparison = (
    s26_data
    .groupby("Response")
    .mean()
    .T
)

print(comparison)


station_cols = [
    col for col in visit_matrix.columns
    if col.startswith("L")
]

X = visit_matrix[station_cols].astype(int)
y = visit_matrix["Response"]

X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.2,
    random_state=42,
    stratify=y
)

model = LogisticRegression(
    class_weight="balanced",
    max_iter=1000
)

model.fit(X_train, y_train)

probabilities = model.predict_proba(X_test)[:, 1]

print(
    "PR-AUC:",
    average_precision_score(
        y_test,
        probabilities
    )
)

importance = pd.DataFrame({
    "Station": station_cols,
    "Coefficient": model.coef_[0]
})

importance = importance.sort_values(
    "Coefficient",
    ascending=False
)

print(importance)

feature = "L2_S26_F3077"

print(
    df.groupby("Response")[feature].describe()
)

comparison = (
    df[
        df[s26_cols]
        .notna()
        .any(axis=1)
    ]
    .groupby("Response")[s26_cols]
    .agg(["mean", "median", "std", "count"])
)

print(comparison)


missing_comparison = []

for col in s26_cols:

    passed = df[df["Response"] == 0][col]
    failed = df[df["Response"] == 1][col]

    missing_comparison.append({
        "Feature": col,
        "Pass Present %": passed.notna().mean() * 100,
        "Fail Present %": failed.notna().mean() * 100
    })

missing_comparison = pd.DataFrame(
    missing_comparison
)

print(missing_comparison)