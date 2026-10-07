/** JOURNAL — every lesson and trade review, searchable. */

import { useState } from "react";
import { api } from "../lib/api";
import { dateTime } from "../lib/format";
import { usePoll } from "../lib/hooks";
import { Badge, Empty, ErrorNote, Panel } from "../components/common";

export function Journal() {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");

  const entries = usePoll(() => api.journal(60, query || undefined), 15000, [query]);
  const explanations = usePoll(() => api.explanations(20), 15000);

  return (
    <>
      <Panel
        title="Trading Journal"
        actions={
          <div className="row">
            <input
              className="field"
              style={{ width: 220 }}
              placeholder="search title and body…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") setQuery(search);
              }}
            />
            <button className="btn sm primary" onClick={() => setQuery(search)}>
              Search
            </button>
            {query ? (
              <button
                className="btn sm"
                onClick={() => {
                  setSearch("");
                  setQuery("");
                }}
              >
                Clear
              </button>
            ) : null}
          </div>
        }
        scroll
      >
        {entries.data && entries.data.entries.length > 0 ? (
          entries.data.entries.map((entry) => (
            <div
              key={entry.id}
              style={{
                marginBottom: 12,
                paddingBottom: 10,
                borderBottom: "1px solid var(--border)",
              }}
            >
              <div className="row">
                <strong className="mono" style={{ fontSize: 12.5 }}>
                  {entry.title || entry.entry_type}
                </strong>
                {entry.symbol ? <Badge>{entry.symbol}</Badge> : null}
                {entry.outcome ? (
                  <Badge tone={entry.outcome === "win" ? "ok" : "error"}>
                    {entry.outcome}
                  </Badge>
                ) : null}
                <span className="spacer" />
                <span className="faint mono" style={{ fontSize: 10 }}>
                  {dateTime(entry.created_at)}
                </span>
              </div>
              {entry.body ? (
                <pre className="block" style={{ marginTop: 6 }}>
                  {entry.body}
                </pre>
              ) : null}
              {entry.lessons.length > 0 ? (
                <ul className="hint" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {entry.lessons.map((lesson, index) => (
                    <li key={index}>{lesson}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          ))
        ) : (
          <Empty>
            {query
              ? `Nothing matches "${query}".`
              : "The journal fills as ATLAS runs: every risk verdict, signal and fill is explained and stored here."}
          </Empty>
        )}
      </Panel>

      <ErrorNote error={entries.error} />

      <div style={{ marginTop: "var(--gap)" }}>
        <Panel title="Mentor — Recent Teaching Notes" scroll>
          {explanations.data && explanations.data.explanations.length > 0 ? (
            explanations.data.explanations.map((note, index) => (
              <div
                key={`${note.timestamp}-${index}`}
                style={{
                  marginBottom: 12,
                  paddingBottom: 10,
                  borderBottom: "1px solid var(--border)",
                }}
              >
                <div className="row">
                  <strong className="mono" style={{ fontSize: 12.5 }}>
                    {note.title}
                  </strong>
                  <Badge>{note.category}</Badge>
                  <span className="spacer" />
                  <span className="faint mono" style={{ fontSize: 10 }}>
                    {dateTime(note.timestamp)}
                  </span>
                </div>
                <pre className="block" style={{ marginTop: 6 }}>
                  {note.body}
                </pre>
                {note.lessons.length > 0 ? (
                  <ul className="hint" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                    {note.lessons.map((lesson, lessonIndex) => (
                      <li key={lessonIndex}>{lesson}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ))
          ) : (
            <Empty>No teaching notes yet.</Empty>
          )}
        </Panel>
      </div>
    </>
  );
}
