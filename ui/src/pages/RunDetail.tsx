import { useState } from "react";
import { Session, api } from "../api";
import { duration, timeAgo, usePoll } from "../hooks";
import Dag, { StepInfo } from "./Dag";

export default function RunDetail({ session, runId, back }: { session: Session; runId: string; back: () => void }) {
  const run = usePoll<any>(() => api.get(`/runs/${runId}`, session.key), [runId], 2500);
  const events = usePoll<any[]>(() => api.get(`/runs/${runId}/events`, session.key), [runId], 3000);
  const incidents = usePoll<any[]>(() => api.get(`/projects/${session.project}/incidents?limit=50`, session.key), [runId], 5000);
  const spec = usePoll<any>(
    () => run.data ? api.get(`/projects/${session.project}/specs/${run.data.pipeline}?version=${run.data.spec_version}`, session.key) : Promise.resolve(null),
    [runId, run.data?.pipeline], 30000
  );

  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [troubleshootResult, setTroubleshootResult] = useState<any>(null);

  if (run.error) return <div className="error-banner">{run.error}</div>;
  if (!run.data) return <div className="muted">loading…</div>;
  const r = run.data;

  // build DAG model from spec + live step statuses
  const stepRows: any[] = r.steps || [];
  const parentRows = Object.fromEntries(stepRows.filter((s: any) => !s.unit).map((s: any) => [s.step_id, s]));
  const specSteps: any[] = spec.data?.spec?.steps || [];
  const dagSteps: StepInfo[] = specSteps.map((s: any) => {
    const units = stepRows.filter((row: any) => row.step_id === s.id && row.unit);
    const uniqueUnits = new Set(units.map((u: any) => u.unit));
    const doneUnits = new Set(units.filter((u: any) => u.status === "succeeded").map((u: any) => u.unit));
    return {
      id: s.id,
      depends_on: s.depends_on || [],
      status: parentRows[s.id]?.status || "pending",
      executor: s.executor || "script",
      gate: !!s.gate,
      fanOut: s.fan_out ? { done: doneUnits.size, total: uniqueUnits.size || (s.fan_out ? 0 : 0) } : null,
    };
  });

  const runGates = (r.gates || []) as any[];
  const pendingGates = runGates.filter((g) => g.status === "pending");
  const runIncidents = (incidents.data || []).filter((i) => i.run_id === runId);

  async function resolveGate(gateId: string, decision: "approve" | "reject") {
    setBusy(gateId); setError(null);
    try {
      await api.post(`/gates/${gateId}/resolve`, { decision, actor: session.actor }, session.key);
      run.reload();
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  }

  async function troubleshoot(stepId: string) {
    setBusy(`ts-${stepId}`); setError(null);
    try {
      setTroubleshootResult(await api.post(`/runs/${runId}/steps/${stepId}/troubleshoot`, {}, session.key));
      incidents.reload(); run.reload();
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  }

  const shownSteps = selected ? stepRows.filter((s: any) => s.step_id === selected) : stepRows;

  return (
    <div className="grid">
      <div className="card">
        <h3>
          <a style={{ cursor: "pointer", color: "var(--accent)" }} onClick={back}>← runs</a>{"  "}
          {r.pipeline} <span className="muted">v{r.spec_version}</span>{" "}
          <span className={`pill ${r.status}`}>{r.status}</span>
          <span className="muted" style={{ float: "right", fontWeight: 400 }}>
            run {runId.slice(0, 8)} · started {timeAgo(r.started_at)} {r.completed_at && `· finished ${timeAgo(r.completed_at)}`}
          </span>
        </h3>
        {error && <div className="error-banner">{error}</div>}
        {r.error && <div className="error-banner">{r.error}</div>}
        {dagSteps.length > 0 && <Dag steps={dagSteps} onSelect={(id) => setSelected(id === selected ? null : id)} />}
      </div>

      {pendingGates.length > 0 && (
        <div className="card" style={{ borderColor: "var(--warn)" }}>
          <h3>⏸ Waiting on you — pending gates</h3>
          {pendingGates.map((g) => (
            <div key={g.gate_id} style={{ marginBottom: 12 }}>
              <span className="pill warn">{g.type}</span> <b>{g.step_id}</b>
              <div style={{ margin: "6px 0" }}>{g.prompt}</div>
              <button className="btn good small" disabled={busy === g.gate_id} onClick={() => resolveGate(g.gate_id, "approve")}>Approve</button>{" "}
              <button className="btn danger small" disabled={busy === g.gate_id} onClick={() => resolveGate(g.gate_id, "reject")}>Reject</button>
              <span className="muted" style={{ marginLeft: 10, fontSize: 12 }}>as {session.actor} · expires {timeAgo(g.expires_at)}</span>
            </div>
          ))}
        </div>
      )}

      <div className="row">
        <div className="card" style={{ flex: 2, minWidth: 500 }}>
          <h3>
            Steps {selected && <span className="muted">— filtered to {selected} <a style={{ cursor: "pointer", color: "var(--accent)" }} onClick={() => setSelected(null)}>(clear)</a></span>}
          </h3>
          {shownSteps.map((s: any, i: number) => (
            <details className="step" key={`${s.step_id}-${s.unit}-${s.attempt}-${i}`} open={s.status === "failed"}>
              <summary>
                <span className={`pill ${s.status}`}>{s.status}</span>
                <b>{s.step_id}</b>
                {s.unit && <span className="muted">unit: {s.unit}</span>}
                {s.attempt > 1 && <span className="muted">attempt {s.attempt}</span>}
                <span className="muted" style={{ marginLeft: "auto" }}>
                  {s.executor} · {duration(s.duration_seconds)}
                </span>
              </summary>
              <div className="body">
                <div className="kv">
                  <div className="k">started</div><div>{s.started_at || "—"}</div>
                  <div className="k">completed</div><div>{s.completed_at || "—"}</div>
                  {s.external_ref && (<><div className="k">external ref</div><div style={{ fontFamily: "var(--mono)" }}>{s.external_ref}</div></>)}
                </div>
                {s.error && <pre className="code" style={{ marginTop: 8 }}>{s.error}</pre>}
                {s.status === "failed" && !s.unit && (
                  <button className="btn small" style={{ marginTop: 8 }} disabled={busy === `ts-${s.step_id}`}
                          onClick={() => troubleshoot(s.step_id)}>
                    {busy === `ts-${s.step_id}` ? "analyzing… (30-60s)" : "🔍 AI troubleshoot"}
                  </button>
                )}
              </div>
            </details>
          ))}
        </div>

        <div style={{ flex: 1, minWidth: 360 }} className="grid">
          <div className="card">
            <h3>Parameters (with provenance)</h3>
            <table>
              <tbody>
                {(r.parameters || []).map((p: any) => (
                  <tr key={p.name}>
                    <td style={{ fontFamily: "var(--mono)", fontSize: 12 }}>{p.name}</td>
                    <td style={{ fontFamily: "var(--mono)", fontSize: 12 }}>{JSON.stringify(p.value)}</td>
                    <td className="muted" style={{ fontSize: 11 }}>{p.provenance}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card">
            <h3>Gate history</h3>
            {runGates.length === 0 && <div className="muted">No gates in this run.</div>}
            {runGates.map((g) => (
              <div key={g.gate_id} style={{ marginBottom: 6, fontSize: 13 }}>
                <span className={`pill ${g.status}`}>{g.status}</span> {g.type} · {g.step_id}
                {g.decided_by && <span className="muted"> by {g.decided_by}</span>}
              </div>
            ))}
          </div>

          <div className="card">
            <h3>Event stream</h3>
            <div className="feed">
              {(events.data || []).slice().reverse().map((e: any) => (
                <div className="ev" key={e.event_id}>
                  <span className="type">{e.type}</span>
                  <span className="muted">{e.step_id}</span>
                  <span className="muted" style={{ marginLeft: "auto" }}>{timeAgo(e.at)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {(troubleshootResult || runIncidents.length > 0) && (
        <IncidentPanel session={session} runId={runId} incidents={runIncidents}
                       fresh={troubleshootResult} onChanged={() => { incidents.reload(); run.reload(); }} />
      )}
    </div>
  );
}

function IncidentPanel({ session, incidents, fresh, onChanged }:
  { session: Session; runId: string; incidents: any[]; fresh: any; onChanged: () => void }) {
  const [detail, setDetail] = useState<any>(fresh);
  const [playbooks, setPlaybooks] = useState<any[] | null>(null);
  const [playbook, setPlaybook] = useState("");
  const [paramsJson, setParamsJson] = useState("{}");
  const [outcome, setOutcome] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const incidentId = detail?.incident_id || detail?.id;

  async function open(id: string) {
    setDetail(await api.get(`/incidents/${id}`, session.key));
    setMessage(null); setError(null);
  }

  async function loadPlaybooks() {
    setPlaybooks(await api.get(`/projects/${session.project}/playbooks`, session.key));
  }

  async function execute() {
    setError(null); setMessage(null);
    try {
      const r = await api.post(`/incidents/${incidentId}/execute`,
        { playbook, params: JSON.parse(paramsJson || "{}") }, session.key);
      setMessage(`Sandbox execution started: ${r.playbook} (workflow ${r.workflow_id})`);
      onChanged();
    } catch (e: any) { setError(e.message); }
  }

  async function reportOutcome(success: boolean) {
    setError(null); setMessage(null);
    try {
      await api.post(`/incidents/${incidentId}/outcome`, { outcome, success }, session.key);
      setMessage(success ? "Outcome captured — becomes searchable knowledge." : "Outcome recorded.");
      onChanged();
    } catch (e: any) { setError(e.message); }
  }

  return (
    <div className="card">
      <h3>Incidents & AI analysis</h3>
      {error && <div className="error-banner">{error}</div>}
      {message && <div className="notice">{message}</div>}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        {incidents.map((i) => (
          <button key={i.id} className="btn ghost small" onClick={() => open(i.id)}>
            {i.step_id} · <span className={`pill ${i.status}`}>{i.status}</span>
          </button>
        ))}
      </div>
      {detail && (
        <div className="row">
          <div style={{ flex: 1, minWidth: 380 }}>
            <h3>Root cause analysis</h3>
            <pre className="code">{detail.rca || "—"}</pre>
            <h3 style={{ marginTop: 12 }}>Remediation proposal <span className="muted">(advisory — approval + playbook required)</span></h3>
            <pre className="code">{detail.remediation_proposal || "—"}</pre>
          </div>
          <div style={{ flex: 1, minWidth: 340 }}>
            {(detail.similar_incidents?.hits || detail.similar_incidents || []).length > 0 && (
              <>
                <h3>Similar past incidents</h3>
                {(detail.similar_incidents?.hits || detail.similar_incidents || []).map((h: any, i: number) => (
                  <div key={i} style={{ marginBottom: 8, fontSize: 13 }}>
                    <b>{h.title}</b> <span className="muted">({h.kind}, score {h.score})</span>
                    <div className="muted" style={{ fontSize: 12 }}>{h.snippet?.slice(0, 150)}</div>
                  </div>
                ))}
              </>
            )}
            <h3 style={{ marginTop: 12 }}>Execute pre-approved playbook</h3>
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
              Requires the remediation gate approved first (see gates above).
            </div>
            {!playbooks
              ? <button className="btn ghost small" onClick={loadPlaybooks}>load playbooks</button>
              : (
                <>
                  <select value={playbook} onChange={(e) => setPlaybook(e.target.value)}>
                    <option value="">— select playbook —</option>
                    {playbooks.map((p) => <option key={p.name} value={p.name}>{p.name} v{p.version}</option>)}
                  </select>
                  <label>params (JSON, allow-listed only)</label>
                  <textarea className="code" style={{ minHeight: 60 }} value={paramsJson} onChange={(e) => setParamsJson(e.target.value)} />
                  <button className="btn small" style={{ marginTop: 8 }} disabled={!playbook} onClick={execute}>Execute in sandbox</button>
                </>
              )}
            <h3 style={{ marginTop: 14 }}>Close the loop (learning)</h3>
            <textarea style={{ minHeight: 50 }} placeholder="What actually fixed it?" value={outcome} onChange={(e) => setOutcome(e.target.value)} />
            <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
              <button className="btn good small" disabled={!outcome} onClick={() => reportOutcome(true)}>Fixed — save as knowledge</button>
              <button className="btn ghost small" disabled={!outcome} onClick={() => reportOutcome(false)}>Didn't work</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
