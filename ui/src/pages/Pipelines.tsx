import { useEffect, useState } from "react";
import { Session, api } from "../api";
import { timeAgo, usePoll } from "../hooks";

const TEMPLATE = `apiVersion: bosun/v1
kind: Pipeline
name: my-pipeline
project: PROJECT
description: What this pipeline does.

parameters:
  - name: yearmonth
    resolver: activity
    activity: now_yearmonth

steps:
  - id: first-step
    executor: script
    config: {command: ["echo", "hello {{params.yearmonth}}"]}
`;

export default function Pipelines({ session, openRun }: { session: Session; openRun: (id: string) => void }) {
  const specs = usePoll<any[]>(() => api.get(`/projects/${session.project}/specs`, session.key), [session.project], 10000);
  const runs = usePoll<any[]>(() => api.get(`/projects/${session.project}/runs?limit=30`, session.key), [session.project], 4000);
  const catalog = usePoll<any>(() => api.get("/catalog"), [], 60000);

  const [editorOpen, setEditorOpen] = useState(false);
  const [yaml, setYaml] = useState(TEMPLATE.replace("PROJECT", session.project));
  const [validation, setValidation] = useState<any>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [paramsFor, setParamsFor] = useState<string | null>(null);
  const [paramsJson, setParamsJson] = useState("{}");
  const [specView, setSpecView] = useState<any>(null);

  // Draft handed over from the Assistant ("open in spec editor")
  useEffect(() => {
    const draft = localStorage.getItem("bosun.editor-draft");
    if (draft) {
      localStorage.removeItem("bosun.editor-draft");
      setYaml(draft);
      setEditorOpen(true);
      setMessage("Draft loaded from the Assistant — validate, then register.");
    }
  }, []);

  // latest version per pipeline name
  const latest: Record<string, number> = {};
  specs.data?.forEach((s) => { latest[s.name] = Math.max(latest[s.name] || 0, s.version); });

  async function validate() {
    setError(null); setMessage(null);
    try {
      setValidation(await api.postYaml(`/projects/${session.project}/specs/validate`, yaml, session.key));
    } catch (e: any) { setError(e.message); }
  }

  async function register() {
    setError(null); setMessage(null);
    try {
      const r = await api.postYaml(`/projects/${session.project}/specs`, yaml, session.key);
      setMessage(`Registered ${r.name} v${r.version}`);
      setValidation(null);
      specs.reload();
    } catch (e: any) { setError(e.message); }
  }

  async function startRun(pipeline: string) {
    setError(null); setMessage(null);
    try {
      const params = JSON.parse(paramsJson || "{}");
      const r = await api.post(`/projects/${session.project}/pipelines/${pipeline}/runs`, { params }, session.key);
      setParamsFor(null);
      openRun(r.run_id);
    } catch (e: any) { setError(e.message); }
  }

  async function viewSpec(name: string) {
    setSpecView(await api.get(`/projects/${session.project}/specs/${name}`, session.key));
  }

  return (
    <div className="grid">
      {error && <div className="error-banner">{error}</div>}
      {message && <div className="notice">{message}</div>}

      <div className="row">
        <div className="card" style={{ flex: 1, minWidth: 420 }}>
          <h3>
            Pipelines
            <button className="btn small" style={{ float: "right" }} onClick={() => setEditorOpen(!editorOpen)}>
              {editorOpen ? "close editor" : "+ new / edit spec"}
            </button>
          </h3>
          {Object.keys(latest).length === 0 && <div className="muted">No pipelines registered yet.</div>}
          <table>
            <thead><tr><th>Name</th><th>Latest</th><th /></tr></thead>
            <tbody>
              {Object.entries(latest).map(([name, v]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td className="muted">v{v}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button className="btn ghost small" onClick={() => viewSpec(name)}>view</button>{" "}
                    <button className="btn small good" onClick={() => { setParamsFor(name); setParamsJson("{}"); }}>▶ run</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {paramsFor && (
            <div style={{ marginTop: 12 }}>
              <label>Parameter overrides for <b>{paramsFor}</b> (JSON, optional)</label>
              <textarea className="code" style={{ minHeight: 80 }} value={paramsJson} onChange={(e) => setParamsJson(e.target.value)} />
              <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
                <button className="btn good" onClick={() => startRun(paramsFor)}>Start run</button>
                <button className="btn ghost" onClick={() => setParamsFor(null)}>Cancel</button>
              </div>
            </div>
          )}

          {specView && (
            <div style={{ marginTop: 12 }}>
              <h3>{specView.name} v{specView.version} <button className="btn ghost small" style={{ float: "right" }} onClick={() => setSpecView(null)}>close</button></h3>
              <pre className="code">{JSON.stringify(specView.spec, null, 2)}</pre>
              <button className="btn ghost small" style={{ marginTop: 8 }}
                onClick={() => { setYaml(JSON.stringify(specView.spec, null, 2)); setEditorOpen(true); }}>
                load into editor (as JSON — YAML also accepted)
              </button>
            </div>
          )}
        </div>

        <div className="card" style={{ flex: 1, minWidth: 420 }}>
          <h3>Runs</h3>
          <table>
            <thead><tr><th>Pipeline</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>
              {runs.data?.map((r) => (
                <tr key={r.run_id} className="clickable" onClick={() => openRun(r.run_id)}>
                  <td>{r.pipeline} <span className="muted">v{r.spec_version}</span></td>
                  <td><span className={`pill ${r.status}`}>{r.status}</span></td>
                  <td className="muted">{timeAgo(r.started_at || r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {editorOpen && (
        <div className="card">
          <h3>Spec editor <span className="muted">(YAML or JSON — validated before registration)</span></h3>
          <textarea className="code" value={yaml} onChange={(e) => setYaml(e.target.value)} />
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button className="btn ghost" onClick={validate}>Validate</button>
            <button className="btn" onClick={register} disabled={validation != null && !validation.valid}>Register new version</button>
          </div>
          {validation && (
            validation.valid
              ? <div className="notice" style={{ marginTop: 10 }}>Valid ✓ pipeline: {validation.pipeline}</div>
              : <div className="error-banner" style={{ marginTop: 10 }}>{validation.errors.join(" · ")}</div>
          )}
          {catalog.data && (
            <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
              Available — executors: {catalog.data.step_executors?.join(", ")} · checks: {catalog.data.preflight_checks?.join(", ")} · resolvers: {catalog.data.param_resolvers?.join(", ")} · channels: {catalog.data.gate_channels?.join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
