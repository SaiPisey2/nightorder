import { Session, api } from "../api";
import { timeAgo, usePoll } from "../hooks";

export default function Dashboard({ session, openRun }: { session: Session; openRun: (id: string) => void }) {
  const health = usePoll<any>(() => api.get("/health/components"), [], 5000);
  const runs = usePoll<any[]>(() => api.get(`/projects/${session.project}/runs?limit=15`, session.key), [session.project], 4000);
  const gates = usePoll<any[]>(() => api.get(`/projects/${session.project}/gates?status=pending`, session.key), [session.project], 4000);
  const incidents = usePoll<any[]>(() => api.get(`/projects/${session.project}/incidents?limit=8`, session.key), [session.project], 8000);

  return (
    <div className="grid">
      <div className="card">
        <h3>Component health {health.data && <span className="muted">v{health.data.version}</span>}</h3>
        {health.error && <div className="error-banner">{health.error}</div>}
        <div className="health-grid">
          {health.data &&
            Object.entries(health.data.components as Record<string, any>).map(([name, c]) => (
              <div className="health-card" key={name}>
                <div className="name">{name.replace("_", " ")}</div>
                <span className={`pill ${c.ok ? "ok" : "bad"}`}>{c.ok ? "healthy" : "down"}</span>
                <div className="detail">{c.detail}</div>
              </div>
            ))}
        </div>
      </div>

      <div className="row">
        <div className="card" style={{ flex: 2, minWidth: 480 }}>
          <h3>Recent runs</h3>
          {runs.data?.length === 0 && <div className="muted">No runs yet — start one from Pipelines.</div>}
          <table>
            <thead><tr><th>Pipeline</th><th>Status</th><th>Started</th><th>Run id</th></tr></thead>
            <tbody>
              {runs.data?.map((r) => (
                <tr key={r.run_id} className="clickable" onClick={() => openRun(r.run_id)}>
                  <td>{r.pipeline} <span className="muted">v{r.spec_version}</span></td>
                  <td><span className={`pill ${r.status}`}>{r.status}</span></td>
                  <td className="muted">{timeAgo(r.started_at || r.created_at)}</td>
                  <td className="muted" style={{ fontFamily: "var(--mono)", fontSize: 11 }}>{r.run_id.slice(0, 8)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div style={{ flex: 1, minWidth: 320 }} className="grid">
          <div className="card">
            <h3>Pending gates ({gates.data?.length ?? "…"})</h3>
            {gates.data?.length === 0 && <div className="muted">Nothing waiting on a human.</div>}
            {gates.data?.slice(0, 5).map((g) => (
              <div key={g.gate_id} style={{ marginBottom: 8 }}>
                <span className={`pill warn`}>{g.type}</span>{" "}
                <a style={{ cursor: "pointer", color: "var(--accent)" }} onClick={() => openRun(g.run_id)}>
                  {g.step_id}
                </a>
                <div className="muted" style={{ fontSize: 12 }}>{g.prompt.slice(0, 90)}</div>
              </div>
            ))}
          </div>
          <div className="card">
            <h3>Recent incidents</h3>
            {incidents.data?.length === 0 && <div className="muted">No incidents.</div>}
            {incidents.data?.map((i) => (
              <div key={i.id} style={{ marginBottom: 8 }}>
                <span className={`pill ${i.status}`}>{i.status}</span>{" "}
                <a style={{ cursor: "pointer", color: "var(--accent)" }} onClick={() => openRun(i.run_id)}>{i.step_id}</a>
                <span className="muted" style={{ fontSize: 12 }}> {timeAgo(i.created_at)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
