import { useState } from "react";
import { Session, api, loadSession, saveSession } from "./api";
import Dashboard from "./pages/Dashboard";
import Pipelines from "./pages/Pipelines";
import RunDetail from "./pages/RunDetail";
import Knowledge from "./pages/Knowledge";
import Ops from "./pages/Ops";
import Chat from "./pages/Chat";
import { usePoll } from "./hooks";

type Page =
  | { name: "dashboard" }
  | { name: "pipelines" }
  | { name: "run"; runId: string }
  | { name: "knowledge" }
  | { name: "ops" }
  | { name: "chat" };

function Login({ onDone }: { onDone: (s: Session) => void }) {
  const projects = usePoll<any[]>(() => api.get("/projects"), [], 15000);
  const [project, setProject] = useState("");
  const [newProject, setNewProject] = useState("");
  const [actor, setActor] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function enter(id: string) {
    setError(null);
    try {
      await api.get(`/projects/${id}/specs`);
      onDone({ project: id, key: "", actor: actor || "ui-user" });
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function create() {
    setError(null);
    try {
      await api.post("/projects", { id: newProject, display_name: newProject });
      await enter(newProject);
    } catch (e: any) {
      setError(e.message);
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login">
        <h3>Nightorder <span className="muted">— open platform, pick a project</span></h3>
        {error && <div className="error-banner">{error}</div>}
        <label>Your name (recorded on gate approvals / audit trail)</label>
        <input value={actor} onChange={(e) => setActor(e.target.value)} placeholder="you@example.com" />
        <label>Projects</label>
        {projects.data?.length === 0 && <div className="muted">No projects yet — create one below.</div>}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
          {projects.data?.map((p) => (
            <button key={p.id} className={`btn ${project === p.id ? "" : "ghost"}`}
                    onClick={() => setProject(p.id)}>{p.id}</button>
          ))}
        </div>
        <button className="btn" style={{ width: "100%" }} disabled={!project} onClick={() => enter(project)}>
          Enter {project || "…"}
        </button>
        <label style={{ marginTop: 18 }}>Or create a new project</label>
        <div style={{ display: "flex", gap: 8 }}>
          <input value={newProject} onChange={(e) => setNewProject(e.target.value)} placeholder="batch" />
          <button className="btn ghost" disabled={!newProject} onClick={create}>Create</button>
        </div>
      </div>
    </div>
  );
}

function ProjectSwitcher({ session, onSwitch }: { session: Session; onSwitch: (s: Session) => void }) {
  const projects = usePoll<any[]>(() => api.get("/projects"), [], 30000);
  const [creating, setCreating] = useState(false);
  const [newId, setNewId] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function create() {
    setError(null);
    try {
      await api.post("/projects", { id: newId, display_name: newId });
      setCreating(false);
      setNewId("");
      onSwitch({ ...session, project: newId });
    } catch (e: any) {
      setError(e.message?.slice(0, 80));
    }
  }

  if (creating) {
    return (
      <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input autoFocus style={{ width: 120, padding: "5px 8px" }} placeholder="project id"
               value={newId} onChange={(e) => setNewId(e.target.value.toLowerCase().replace(/[^a-z0-9-_]/g, ""))}
               onKeyDown={(e) => { if (e.key === "Enter" && newId) create(); if (e.key === "Escape") setCreating(false); }} />
        <button className="btn small" disabled={!newId} onClick={create}>create</button>
        <button className="btn ghost small" onClick={() => { setCreating(false); setError(null); }}>✕</button>
        {error && <span style={{ color: "var(--bad)", fontSize: 11 }}>{error}</span>}
      </span>
    );
  }

  return (
    <select
      style={{ width: "auto", padding: "5px 8px", fontWeight: 600 }}
      value={session.project}
      onChange={(e) => {
        if (e.target.value === "__create__") setCreating(true);
        else onSwitch({ ...session, project: e.target.value });
      }}
      title="Switch project — every view is scoped to one project"
    >
      {(projects.data || [{ id: session.project }]).map((p: any) => (
        <option key={p.id} value={p.id}>{p.id}</option>
      ))}
      <option value="__create__">➕ new project…</option>
    </select>
  );
}

function HealthDot({ session }: { session: Session }) {
  const { data } = usePoll<any>(() => api.get("/health/components"), [], 5000);
  const ok = data?.ok;
  return (
    <span className={`pill ${ok == null ? "unknown" : ok ? "ok" : "bad"}`}>
      {ok == null ? "…" : ok ? "all systems ok" : "degraded"}
    </span>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(loadSession());
  const [page, setPage] = useState<Page>({ name: "dashboard" });

  if (!session) {
    return (
      <Login
        onDone={(s) => {
          saveSession(s);
          setSession(s);
        }}
      />
    );
  }

  const openRun = (runId: string) => setPage({ name: "run", runId });
  const nav = (name: "dashboard" | "pipelines" | "knowledge" | "ops" | "chat") => setPage({ name } as Page);

  return (
    <>
      <div className="topbar">
        <div className="brand">night<span>order</span></div>
        <div className="nav">
          <button className={page.name === "dashboard" ? "active" : ""} onClick={() => nav("dashboard")}>Dashboard</button>
          <button className={page.name === "pipelines" || page.name === "run" ? "active" : ""} onClick={() => nav("pipelines")}>Pipelines</button>
          <button className={page.name === "knowledge" ? "active" : ""} onClick={() => nav("knowledge")}>Knowledge</button>
          <button className={page.name === "ops" ? "active" : ""} onClick={() => nav("ops")}>Gates & Ops</button>
          <button className={page.name === "chat" ? "active" : ""} onClick={() => nav("chat")}>✦ Assistant</button>
        </div>
        <div className="right">
          <HealthDot session={session} />
          <ProjectSwitcher session={session} onSwitch={(s) => { saveSession(s); setSession(s); setPage({ name: "dashboard" }); }} />
          <button className="btn ghost small" onClick={() => { saveSession(null); setSession(null); }}>exit</button>
        </div>
      </div>
      <div className="main">
        {page.name === "dashboard" && <Dashboard session={session} openRun={openRun} />}
        {page.name === "pipelines" && <Pipelines session={session} openRun={openRun} />}
        {page.name === "run" && <RunDetail session={session} runId={page.runId} back={() => nav("pipelines")} />}
        {page.name === "knowledge" && <Knowledge session={session} />}
        {page.name === "ops" && <Ops session={session} openRun={openRun} />}
        {page.name === "chat" && <Chat session={session} goPipelines={() => nav("pipelines")} />}
      </div>
    </>
  );
}
