
import { useEffect, useState } from "react";
import "./App.css";
import { ApiError, describeApiError } from "./api/client";
import { useAuth } from "./auth/useAuth";
import { ConfigurationError, LoadingScreen, SignInScreen } from "./auth/screens";

type PipelineRun = {
  run_id?: string;
  status?: string;
  stage?: string;
  days_back?: number;
  created_at?: string;
  error?: string;
};

type ApiStatus = "checking" | "connected" | "error";

function isRunsResponse(data: unknown): data is { runs: PipelineRun[] } {
  return (
    typeof data === "object" &&
    data !== null &&
    Array.isArray((data as { runs?: unknown }).runs)
  );
}

function formatCreated(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function statusClass(status?: string) {
  if (status === "succeeded") return "success";
  if (status === "failed") return "danger";
  if (status === "partial_failure" || status === "interrupted") return "warning";
  return "neutral";
}

const apiStatusLabel: Record<ApiStatus, string> = {
  checking: "Connecting to API",
  connected: "API connected — demo mode",
  error: "API offline — demo mode",
};

type Stage = {
  name: string;
  detail: string;
  status: "complete" | "blocked" | "pending";
};

const initialStages: Stage[] = [
  { name: "Bronze", detail: "Ingest EirGrid JSON", status: "complete" },
  { name: "Silver", detail: "Validate and transform Parquet", status: "complete" },
  { name: "Gold", detail: "Aggregate daily demand", status: "complete" },
  { name: "Glue", detail: "Register analytics partition", status: "blocked" },
];

type QualityStatus = "complete" | "partial" | "unknown";

type DemandRow = {
  date: string;
  region: string;
  average_demand_mw: number;
  minimum_demand_mw: number;
  maximum_demand_mw: number;
  observations: number;
  expected_observations?: number | null;
  is_complete?: boolean | null;
  quality_status?: QualityStatus;
  quality_note?: string;
};

const qualityDisplay: Record<QualityStatus, { symbol: string; label: string }> = {
  complete: { symbol: "✓", label: "Complete" },
  partial: { symbol: "◐", label: "Partial" },
  unknown: { symbol: "?", label: "Unknown" },
};

function qualityOf(row: DemandRow): QualityStatus {
  return row.quality_status === "complete" || row.quality_status === "partial"
    ? row.quality_status
    : "unknown";
}

function observationText(row: DemandRow) {
  return typeof row.expected_observations === "number"
    ? `${row.observations} of ${row.expected_observations} intervals`
    : `${row.observations} observations (expected count unknown)`;
}

type DemandData = { source: string; days: DemandRow[] };

function isDemandData(data: unknown): data is DemandData {
  if (typeof data !== "object" || data === null) return false;
  const { source, days } = data as Partial<DemandData>;
  return (
    typeof source === "string" &&
    Array.isArray(days) &&
    days.every(
      (row) =>
        typeof row?.date === "string" &&
        typeof row.region === "string" &&
        typeof row.average_demand_mw === "number" &&
        typeof row.minimum_demand_mw === "number" &&
        typeof row.maximum_demand_mw === "number" &&
        typeof row.observations === "number",
    )
  );
}

const mwFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

function shortDate(value: string) {
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function Dashboard() {
  const { api, state, signOut } = useAuth();
  const user = state.status === "signedIn" ? state.user : null;
  const [stages, setStages] = useState(initialStages);
  const [message, setMessage] = useState(
    "Demo view — connect the backend to control real executions.",
  );
  const [running, setRunning] = useState(false);

  const runDemo = () => {
    setRunning(true);
    setMessage(
      "Demo only: no pipeline was started. Pipeline execution is disabled.",
    );
    setStages((current) =>
      current.map((stage) => ({
        ...stage,
        status: "pending",
      })),
    );
    window.setTimeout(() => {
      setStages(initialStages);
      setRunning(false);
      setMessage(
        "Demo complete. Real execution controls will be enabled after API integration.",
      );
    }, 1200);
  };

  const [runs, setRuns] = useState<PipelineRun[]>([]);
  const [demandData, setDemandData] = useState<DemandData | null>(null);
  const [demandStatus, setDemandStatus] = useState<ApiStatus>("checking");
  const [demandError, setDemandError] = useState("");
  const [demandAttempt, setDemandAttempt] = useState(0);

  // Read-only GET of Gold-layer analytics; aborted on unmount.
  useEffect(() => {
    const controller = new AbortController();

    if (!api) return;
    api
      .getJson("/api/analytics/daily-demand", { signal: controller.signal })
      .then((data) => {
        if (!isDemandData(data)) throw new ApiError("invalid_response");
        setDemandData(data);
        setDemandError("");
        setDemandStatus("connected");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setDemandData(null);
        setDemandError(describeApiError(error));
        setDemandStatus("error");
      });

    return () => controller.abort();
  }, [api, demandAttempt]);

  // Chart and summary use the region of the most recent Gold row.
  const allRows = demandData?.days ?? [];
  const region = allRows.length ? allRows[allRows.length - 1].region : "";
  const demandRows = allRows.filter((row) => row.region === region).slice(-14);
  const latestDay = demandRows.length
    ? demandRows[demandRows.length - 1]
    : undefined;
  const maxDemand = Math.max(
    1,
    ...demandRows.map((row) => row.average_demand_mw),
  );
  const latestRun = runs[0];
  const [apiStatus, setApiStatus] = useState<ApiStatus>("checking");
  const [apiError, setApiError] = useState("");
  const [attempt, setAttempt] = useState(0);

  // Read-only GET; the abort signal prevents state updates after unmount.
  useEffect(() => {
    const controller = new AbortController();

    if (!api) return;
    api
      .getJson("/api/runs", { signal: controller.signal })
      .then((data) => {
        if (!isRunsResponse(data)) throw new ApiError("invalid_response");
        setRuns(data.runs);
        setApiError("");
        setApiStatus("connected");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setApiError(describeApiError(error));
        setApiStatus("error");
      });

    return () => controller.abort();
  }, [api, attempt]);

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">IE</div>
          <div>
            <strong>Energy Analytics</strong>
            <span>DATA PLATFORM</span>
          </div>
        </div>

        <div className="nav-label">WORKSPACE</div>
        <a className="nav-item active" href="#overview">▦ <span>Overview</span></a>
        <a className="nav-item" href="#pipeline">⌘ <span>Pipeline control</span></a>
        <a className="nav-item" href="#analytics">⌁ <span>Demand analytics</span></a>
        <a className="nav-item" href="#history">◷ <span>Run history</span></a>

        <div className="sidebar-bottom">
          <div className="environment-label">ENVIRONMENT</div>
          <div className="environment">
            <span className="environment-dot" />
            AWS · eu-west-1
          </div>
          <p>Irish Energy Analytics<br />Portfolio project</p>
        </div>
      </aside>

      <section className="main-content" id="overview">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> Overview</div>
          <div className="topbar-pills">
            <div className={`api-pill ${apiStatus}`} role="status">
              <span /> {apiStatusLabel[apiStatus]}
            </div>
            <div className="demo-pill"><span /> DEMONSTRATION MODE</div>
            <div className="user-chip">
              <span>
                {user?.username} · {user?.role ?? "no role"}
              </span>
              <button className="retry-button" onClick={signOut}>
                Sign out
              </button>
            </div>
          </div>
        </header>

        <div className="page-heading">
          <div>
            <p className="eyebrow">DATA OPERATIONS</p>
            <h1>Energy data overview</h1>
            <p className="subtitle">
              Monitor ingestion, transformation and daily demand analytics.
            </p>
          </div>
          {/* Visibility is a UI hint only; the API enforces roles and execution is disabled. */}
          {user?.role === "operator" ? (
            <button className="primary-button" onClick={runDemo} disabled={running}>
              {running ? "Simulating…" : "▶  Run pipeline"}
            </button>
          ) : (
            <span className="role-note">Read-only access</span>
          )}
        </div>

        <div className="notice">
          <span>ⓘ</span>
          <div>
            <strong>Demo dashboard</strong>
            <p>{message} Demand analytics come from the S3 Gold layer when available; the pipeline stages below are illustrative.</p>
          </div>
        </div>

        <div className="metrics-grid">
          <Metric
            label="LATEST API RUN"
            value={latestRun?.status ?? (apiStatus === "connected" ? "None recorded" : "Unavailable")}
            foot={latestRun ? `Stage: ${latestRun.stage ?? "unknown"}` : "Run history is held in API memory"}
            icon="◷"
          />
          <Metric
            label="LATEST DAY OBSERVATIONS"
            value={latestDay ? observationText(latestDay).replace(" intervals", "").replace(" of ", " / ") : "Unavailable"}
            foot={latestDay ? `${latestDay.date} · ${latestDay.region} · ${qualityDisplay[qualityOf(latestDay)].label}` : "No Gold data loaded"}
            icon="▤"
          />
          <Metric
            label="DAILY AVG. DEMAND"
            value={latestDay ? `${mwFormat.format(latestDay.average_demand_mw)} MW` : "Unavailable"}
            foot={latestDay ? `${latestDay.date} · ${latestDay.region}` : "No Gold data loaded"}
            icon="⌁"
          />
          <Metric
            label="PEAK DEMAND"
            value={latestDay ? `${mwFormat.format(latestDay.maximum_demand_mw)} MW` : "Unavailable"}
            foot={latestDay ? `Minimum ${mwFormat.format(latestDay.minimum_demand_mw)} MW` : "No Gold data loaded"}
            icon="▱"
          />
        </div>

        <div className="content-grid">
          <section className="panel pipeline-panel" id="pipeline">
            <div className="panel-heading">
              <div>
                <h2>Pipeline execution</h2>
                <p>Bronze → Silver → Gold → Catalogue</p>
              </div>
              <span className="status-badge warning">ILLUSTRATIVE</span>
            </div>

            <div className="stage-list">
              {stages.map((stage, index) => (
                <div className="stage" key={stage.name}>
                  <div className={`stage-number ${stage.status}`}>
                    {stage.status === "complete" ? "✓" : index + 1}
                  </div>
                  <div className="stage-copy">
                    <strong>{stage.name}</strong>
                    <span>{stage.detail}</span>
                  </div>
                  <span className={`stage-status ${stage.status}`}>
                    {stage.status === "complete"
                      ? "Complete"
                      : stage.status === "blocked"
                        ? "Blocked"
                        : "Pending"}
                  </span>
                </div>
              ))}
            </div>

            <div className="panel-footer">
              <span>Simulated stages — not live execution status.</span>
              <a href="#history">View history ↗</a>
            </div>
          </section>

          <section className="panel analytics-panel" id="analytics">
            <div className="panel-heading">
              <div>
                <h2>Electricity demand</h2>
                <p>
                  Daily average · MW · S3 Gold layer
                  {region ? ` · ${region}` : ""}
                </p>
              </div>
              <span className="period-label">LATEST 14 DAYS</span>
            </div>

            {demandStatus === "checking" && (
              <p className="table-message">Loading Gold-layer analytics…</p>
            )}
            {demandStatus === "error" && (
              <div className="table-message error" role="alert">
                <span>Real demand data is unavailable: {demandError}</span>
                <button
                  className="retry-button"
                  onClick={() => {
                    setDemandStatus("checking");
                    setDemandAttempt((n) => n + 1);
                  }}
                >
                  Retry
                </button>
              </div>
            )}
            {demandStatus === "connected" && demandRows.length === 0 && (
              <p className="table-message">
                No Gold daily-demand files were found in {demandData?.source}.
              </p>
            )}
            {demandStatus === "connected" && latestDay && (
              <>
                <div className="chart-summary">
                  <strong>{mwFormat.format(latestDay.average_demand_mw)} MW</strong>
                  <span>Latest daily average · {latestDay.date}</span>
                </div>

                <div className="chart">
                  {demandRows.map((point) => {
                    const quality = qualityOf(point);
                    const { symbol, label } = qualityDisplay[quality];
                    return (
                      <div className="chart-column" key={point.date}>
                        <div className="bar-track">
                          <div
                            className={`bar ${quality}`}
                            style={{ height: `${(point.average_demand_mw / maxDemand) * 100}%` }}
                            title={`${point.date}: ${mwFormat.format(point.average_demand_mw)} MW avg (${mwFormat.format(point.minimum_demand_mw)}–${mwFormat.format(point.maximum_demand_mw)} MW) · ${label}: ${observationText(point)}${point.quality_note ? `. ${point.quality_note}` : ""}`}
                          />
                        </div>
                        <span>{shortDate(point.date)}</span>
                        <span
                          className={`quality-badge ${quality}`}
                          title={point.quality_note ?? label}
                        >
                          {symbol} {label}
                        </span>
                      </div>
                    );
                  })}
                </div>
                <div className="quality-legend">
                  <span>✓ Complete: all expected 15-minute intervals present</span>
                  <span>◐ Partial: fewer than expected (day in progress or missing measurements)</span>
                  <span>? Unknown: completeness cannot be established</span>
                </div>
                {latestDay?.quality_note && qualityOf(latestDay) !== "complete" && (
                  <p className="quality-note">
                    {latestDay.date}: {observationText(latestDay)}. {latestDay.quality_note}
                  </p>
                )}
                <div className="chart-note">Source: {demandData?.source}</div>
              </>
            )}
          </section>
        </div>

        <section className="panel history-panel" id="history">
          <div className="panel-heading">
            <div>
              <h2>Recent pipeline runs</h2>
              <p>Execution history and outcomes</p>
            </div>
            <span className="period-label">LATEST</span>
          </div>
          {apiStatus === "checking" && (
            <p className="table-message">Loading run history…</p>
          )}
          {apiStatus === "error" && (
            <div className="table-message error" role="alert">
              <span>Unable to load run history: {apiError}</span>
              <button
                className="retry-button"
                onClick={() => {
                  setApiStatus("checking");
                  setAttempt((n) => n + 1);
                }}
              >
                Retry
              </button>
            </div>
          )}
          {apiStatus === "connected" && runs.length === 0 && (
            <p className="table-message">
              No pipeline runs have been recorded by the API yet.
            </p>
          )}
          {apiStatus === "connected" && runs.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>RUN</th>
                    <th>CREATED</th>
                    <th>STAGE</th>
                    <th>STATUS</th>
                    <th>DETAIL</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run, index) => (
                    <tr key={run.run_id ?? `run-${index}`}>
                      <td className="mono">{run.run_id ?? "—"}</td>
                      <td>{formatCreated(run.created_at)}</td>
                      <td>{run.stage ?? "—"}</td>
                      <td>
                        <span className={`table-status ${statusClass(run.status)}`}>
                          {run.status ?? "unknown"}
                        </span>
                      </td>
                      <td>
                        {run.error ??
                          (run.days_back !== undefined
                            ? `${run.days_back} day(s) back`
                            : "—")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <footer>
          <span>Irish Energy Analytics · Portfolio project</span>
          <span>{apiStatusLabel[apiStatus]}</span>
        </footer>
      </section>
    </main>
  );
}

function Metric({
  label,
  value,
  foot,
  icon,
}: {
  label: string;
  value: string;
  foot: string;
  icon: string;
}) {
  return (
    <section className="metric-card">
      <div className="metric-top">
        <span>{label}</span>
        <span className="metric-icon">{icon}</span>
      </div>
      <strong>{value}</strong>
      <p>{foot}</p>
    </section>
  );
}

function App() {
  const { state, signIn } = useAuth();
  switch (state.status) {
    case "unconfigured":
      return <ConfigurationError problems={state.problems} />;
    case "loading":
      return <LoadingScreen />;
    case "signedOut":
      return <SignInScreen notice={state.notice} onSignIn={signIn} />;
    default:
      return <Dashboard />;
  }
}

export default App;
