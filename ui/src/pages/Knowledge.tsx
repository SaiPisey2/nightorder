import { useState } from "react";
import { Session, api } from "../api";
import { timeAgo, usePoll } from "../hooks";

export default function Knowledge({ session }: { session: Session }) {
  const list = usePoll<any[]>(() => api.get(`/projects/${session.project}/knowledge`, session.key), [session.project], 6000);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<any[] | null>(null);
  const [form, setForm] = useState({ kind: "runbook", title: "", content: "", source: "" });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  async function search() {
    setError(null);
    try {
      setResults(await api.get(`/projects/${session.project}/knowledge/search?q=${encodeURIComponent(query)}&limit=8`, session.key));
    } catch (e: any) { setError(e.message); }
  }

  async function add() {
    setError(null); setMessage(null);
    try {
      await api.post(`/projects/${session.project}/knowledge`, form, session.key);
      setMessage("Added — relay indexes it into Qdrant within seconds.");
      setForm({ ...form, title: "", content: "", source: "" });
      list.reload();
    } catch (e: any) { setError(e.message); }
  }

  async function openDetail(id: string) {
    setError(null); setConfirmDelete(false);
    try {
      setDetail(await api.get(`/projects/${session.project}/knowledge/${id}/detail`, session.key));
    } catch (e: any) { setError(e.message); }
  }

  async function remove(id: string) {
    setError(null); setMessage(null);
    try {
      const body = await api.del(`/projects/${session.project}/knowledge/${id}`, session.key);
      setMessage(body.qdrant?.startsWith("warning") ? `Deleted. ${body.qdrant}` : "Deleted (row + embedding).");
      setDetail(null); setConfirmDelete(false);
      list.reload(); setResults(null);
    } catch (e: any) { setError(e.message); }
  }

  return (
    <div className="row">
      <div className="card" style={{ flex: 2, minWidth: 480 }}>
        <h3>Knowledge base <span className="muted">({list.data?.length ?? "…"} records — semantic search via Qdrant)</span></h3>
        {error && <div className="error-banner">{error}</div>}
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <input placeholder="semantic search: e.g. 'no space left on device dataset'" value={query}
                 onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} />
          <button className="btn" onClick={search} disabled={!query}>Search</button>
          {results && <button className="btn ghost" onClick={() => setResults(null)}>clear</button>}
        </div>

        {detail ? (
          <div>
            <h3>
              {detail.title}{" "}
              <span className="pill pending">{detail.kind}</span>{" "}
              {detail.indexed ? <span className="pill ok">indexed</span> : <span className="pill warn">queued</span>}
              <button className="btn ghost small" style={{ float: "right" }} onClick={() => setDetail(null)}>close</button>
            </h3>
            {detail.source && <div className="muted" style={{ marginBottom: 8 }}>source: {detail.source} · added {timeAgo(detail.created_at)}</div>}
            <pre className="code" style={{ maxHeight: 500 }}>{detail.content}</pre>
            <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
              {!confirmDelete
                ? <button className="btn danger small" onClick={() => setConfirmDelete(true)}>Delete record</button>
                : (
                  <>
                    <span className="muted" style={{ alignSelf: "center" }}>Removes the record and its embedding — sure?</span>
                    <button className="btn danger small" onClick={() => remove(detail.id)}>Yes, delete</button>
                    <button className="btn ghost small" onClick={() => setConfirmDelete(false)}>Cancel</button>
                  </>
                )}
            </div>
          </div>
        ) : results ? (
          <>
            <div className="muted" style={{ marginBottom: 8 }}>Top matches (by embedding similarity) — click to open:</div>
            {results.length === 0 && <div className="muted">No matches.</div>}
            {results.map((h) => (
              <div key={h.id} className="card clickable" style={{ marginBottom: 8, background: "var(--panel2)", cursor: "pointer" }}
                   onClick={() => openDetail(h.id)}>
                <b>{h.title}</b> <span className="pill pending">{h.kind}</span> <span className="muted">score {h.score}</span>
                <div style={{ fontSize: 13, marginTop: 4 }}>{h.snippet}</div>
                {h.source && <div className="muted" style={{ fontSize: 12 }}>source: {h.source}</div>}
              </div>
            ))}
          </>
        ) : (
          <table>
            <thead><tr><th>Title</th><th>Kind</th><th>Indexed</th><th>Added</th></tr></thead>
            <tbody>
              {list.data?.map((k) => (
                <tr key={k.id} className="clickable" onClick={() => openDetail(k.id)}>
                  <td>{k.title}<div className="muted" style={{ fontSize: 12 }}>{k.content_preview.slice(0, 120)}…</div></td>
                  <td><span className="pill pending">{k.kind}</span></td>
                  <td>{k.indexed ? <span className="pill ok">indexed</span> : <span className="pill warn">queued</span>}</td>
                  <td className="muted">{timeAgo(k.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card" style={{ flex: 1, minWidth: 340 }}>
        <h3>Add knowledge</h3>
        {message && <div className="notice">{message}</div>}
        <label>Kind</label>
        <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          <option value="runbook">runbook</option>
          <option value="incident">incident</option>
          <option value="fix">fix</option>
          <option value="doc">doc</option>
        </select>
        <label>Title</label>
        <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })}
               placeholder="fetch_results fails silently when the volume is undersized" />
        <label>Content</label>
        <textarea style={{ minHeight: 140 }} value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })}
                  placeholder="Symptoms, root cause, fix, verification…" />
        <label>Source (ticket / wiki url, optional)</label>
        <input value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} placeholder="INC-1024" />
        <button className="btn" style={{ marginTop: 12 }} onClick={add} disabled={!form.title || !form.content}>Add record</button>
      </div>
    </div>
  );
}
