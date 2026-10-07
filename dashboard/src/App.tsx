import type { Summary } from "./api";
import { AsOfBar } from "./components/AsOfBar";
import { ApiDown } from "./components/Status";
import { toHash, useApi, useRoute, useTheme, type Page, type ThemeChoice } from "./hooks";
import { Overview } from "./pages/Overview";
import { PartTrace } from "./pages/PartTrace";
import { StationsPage } from "./pages/Stations";
import { ColorsContext, PALETTE } from "./theme";

const TABS: { page: Page; label: string }[] = [
  { page: "overview", label: "Overview" },
  { page: "stations", label: "Stations" },
  { page: "parts", label: "Part trace" },
];

export default function App() {
  const [route, navigate] = useRoute();
  const theme = useTheme();

  // As of the end of the data: the time range and the model's training cutoff
  const end = useApi<Summary>("/summary");

  const lastHour = end.data?.data_last_hour;
  const at = route.at !== null && lastHour !== undefined && route.at >= lastHour ? null : route.at;
  const partLink = (id: number) => toHash({ page: "parts", partId: id, at });

  return (
    <ColorsContext.Provider value={PALETTE[theme.mode]}>
      <div className="app">
        <header className="topbar">
          <a className="brand" href={toHash({ page: "overview", partId: null, at })}>
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <rect x="1" y="9" width="3.5" height="6" rx="1" fill={PALETTE[theme.mode].lines.L0} />
              <rect x="6.25" y="5" width="3.5" height="10" rx="1" fill={PALETTE[theme.mode].lines.L1} />
              <rect x="11.5" y="1" width="3.5" height="14" rx="1" fill={PALETTE[theme.mode].lines.L2} />
            </svg>
            ForgeOps
          </a>
          <nav aria-label="Pages">
            {TABS.map((tab) => (
              <a
                key={tab.page}
                href={toHash({ page: tab.page, partId: tab.page === "parts" ? route.partId : null, at })}
                aria-current={route.page === tab.page ? "page" : undefined}
              >
                {tab.label}
              </a>
            ))}
          </nav>
          <label className="theme">
            <span className="visually-hidden">Theme</span>
            <select value={theme.choice} onChange={(event) => theme.setChoice(event.target.value as ThemeChoice)}>
              <option value="system">System theme</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
          </label>
        </header>

        {end.error ? (
          <ApiDown error={end.error} />
        ) : !end.data ? (
          <p className="muted connecting">Connecting to the API…</p>
        ) : (
          <>
            <AsOfBar
              at={at}
              lastHour={end.data.data_last_hour}
              trainingCutoff={end.data.model.training_cutoff_hour}
              onChange={(next) => navigate({ at: next }, { replace: true })}
            />
            <main>
              {route.page === "overview" && <Overview at={at} partLink={partLink} />}
              {route.page === "stations" && <StationsPage at={at} />}
              {route.page === "parts" && (
                <PartTrace
                  partId={route.partId}
                  at={at}
                  partLink={partLink}
                  onOpen={(id) => navigate({ page: "parts", partId: id })}
                />
              )}
            </main>
          </>
        )}

        <footer>
          Bosch Production Line Performance data (Kaggle), anonymized: times are production hours since the first
          timestamp, with no calendar dates. Every view shows only what was known at the selected hour.
        </footer>
      </div>
    </ColorsContext.Provider>
  );
}
