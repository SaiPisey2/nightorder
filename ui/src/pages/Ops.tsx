import { useState } from "react";
import { Session, api } from "../api";
import { timeAgo, usePoll } from "../hooks";

function DangerZone({ session }: { session: Session }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function nuke() {
    setBusy(true); setError(null);
    try {
      await api.del(`/projects/${session.project}?confirm=${session.project}`, session.key);
      localStorage.removeItem("nightorder.session");
      localStorage.removeItem(`nightorder.chat.${session.project}`);
      location.reload(); // back to project picker
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ borderColor: "var(--bad)" }}>
      <h3 style={{ color: "var(--bad)" }}>Danger zone</h3>
      <div style={{ fontSize: 13, marginBottom: 8 }}>
        Delete project <b>{session.project}</b> — removes all pipelines, runs, step history,
        gates, events, incidents, knowledge (incl. embeddings) and playbooks, and terminates
        any running workflows. <b>Irreversible.</b>
      </div>
      {error && <div className="error-banner">{error}</div>}
      <div style={{ display: "flex", gap: 8, maxWidth: 480 }}>
        <input placeholder={`type "${session.project}" to confirm`} value={typed}
               onChange={(e) => setTyped(e.target.value)} />
        <button className="btn danger" disabled={typed !== session.project || busy} onClick={nuke}>
          {busy ? "deleting…" : "Delete project"}
        </button>
      </div>
    </div>
  );
}

export default function Ops({ session, openRun }: { session: Session; openRun: (id: string) => void }) {
  const gates = usePoll<any[]>(() => api.get(`/projects/${session.project}/gates?status=pending`, session.key), [session.project], 3000);
  const agents = usePoll<any[]>(() => api.get("/agents"), [], 15000);
  const playbooks = usePoll<any[]>(() => api.get(`/projects/${session.project}/playbooks`, session.key), [session.project], 15000);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pb, setPb] = useState({ name: "", description: "", image: "", command: '["sh","-c","echo fix $PARAM_TARGET"]', allowed_params: '{"target":"what to fix"}', approved_by: session.actor });

  async function resolve(gateId: string, decision: "approve" | "reject") {
    setBusy(gateId); setError(null);
    try {
      await api.post(`/gates/${gateId}/resolve`, { decision, actor: session.actor }, session.key);
      gates.reload();
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  }

  async function addPlaybook() {
    setError(null); setMessage(null);
    try {
      const r = await api.post(`/projects/${session.project}/playbooks`, {
        name: pb.name, description: pb.description, image: pb.image,
        command: JSON.parse(pb.command), allowed_params: JSON.parse(pb.allowed_params),
        approved_by: pb.approved_by,
      }, session.key);
      setMessage(`Playbook ${r.name} v${r.version} registered.`);
      playbooks.reload();
    } catch (e: any) { setError(e.message); }
  }

  return (
    <div className="grid">
      {error && <div className="error-banner">{error}</div>}
      {message && <div className="notice">{message}</div>}

      <div className="card">
        <h3>Pending gates — all pipelines in {session.project}</h3>
        {gates.data?.length === 0 && <div className="muted">Nothing waiting on a human. 🎉</div>}
        {gates.data?.map((g) => (
          <div key={g.gate_id} style={{ marginBottom: 14, paddingBottom: 12, borderBottom: "1px solid var(--border)" }}>
            <span className="pill warn">{g.type}</span> <b>{g.step_id}</b>{" "}
            <a style={{ cursor: "pointer", color: "var(--accent)" }} onClick={() => openRun(g.run_id)}>open run →</a>
            <div style={{ margin: "6px 0" }}>{g.prompt}</div>
            <button className="btn good small" disabled={busy === g.gate_id} onClick={() => resolve(g.gate_id, "approve")}>Approve</button>{" "}
            <button className="btn danger small" disabled={busy === g.gate_id} onClick={() => resolve(g.gate_id, "reject")}>Reject</button>
            <span className="muted" style={{ marginLeft: 10, fontSize: 12 }}>requested {timeAgo(g.created_at)} · expires {timeAgo(g.expires_at)}</span>
          </div>
        ))}
      </div>

      <div className="row">
        <div className="card" style={{ flex: 1, minWidth: 380 }}>
          <h3>AI agent registry</h3>
          <table>
            <thead><tr><th>Agent</th><th>Capabilities</th><th>Approval</th><th>Status</th></tr></thead>
            <tbody>
              {agents.data?.map((a) => (
                <tr key={a.id}>
                  <td>{a.name}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{Object.keys(a.capabilities).join(", ")}</td>
                  <td>{a.requires_approval ? "required" : "—"}</td>
                  <td><span className={`pill ${a.active ? "ok" : "pending"}`}>{a.active ? "active" : "off"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card" style={{ flex: 1, minWidth: 380 }}>
          <h3>Playbooks <span className="muted">(pre-approved remediations — only these can execute)</span></h3>
          {playbooks.data?.length === 0 && <div className="muted">None registered.</div>}
          {playbooks.data?.map((p) => (
            <div key={`${p.name}-${p.version}`} style={{ marginBottom: 8, fontSize: 13 }}>
              <b>{p.name}</b> v{p.version} <span className="muted">approved by {p.approved_by}</span>
              <div className="muted" style={{ fontSize: 12, fontFamily: "var(--mono)" }}>{p.image}</div>
            </div>
          ))}
          <details style={{ marginTop: 10 }}>
            <summary style={{ cursor: "pointer", color: "var(--accent)" }}>+ register playbook</summary>
            <label>Name</label><input value={pb.name} onChange={(e) => setPb({ ...pb, name: e.target.value })} />
            <label>Description</label><input value={pb.description} onChange={(e) => setPb({ ...pb, description: e.target.value })} />
            <label>Image (immutable, allowed registry)</label><input value={pb.image} onChange={(e) => setPb({ ...pb, image: e.target.value })} />
            <label>Command (JSON array — fixed; params only via env)</label>
            <input value={pb.command} onChange={(e) => setPb({ ...pb, command: e.target.value })} style={{ fontFamily: "var(--mono)" }} />
            <label>Allowed params (JSON)</label>
            <input value={pb.allowed_params} onChange={(e) => setPb({ ...pb, allowed_params: e.target.value })} style={{ fontFamily: "var(--mono)" }} />
            <label>Approved by</label><input value={pb.approved_by} onChange={(e) => setPb({ ...pb, approved_by: e.target.value })} />
            <button className="btn small" style={{ marginTop: 10 }} onClick={addPlaybook} disabled={!pb.name || !pb.image}>Register</button>
          </details>
        </div>
      </div>

      <DangerZone session={session} />
    </div>
  );
}
