/**
 * The ATLAS shell: header, navigation, and the active page.
 *
 * No router dependency — the dashboard has seven flat pages and a `useState`
 * tab is less machinery than react-router for the same result. The hash is
 * kept in sync so a reload lands on the same page.
 */

import { useEffect, useState } from "react";
import { Header } from "./components/Header";
import { api } from "./lib/api";
import { useEventSocket, usePoll } from "./lib/hooks";
import { Agents } from "./pages/Agents";
import { CommandCenter } from "./pages/CommandCenter";
import { Journal } from "./pages/Journal";
import { Portfolio } from "./pages/Portfolio";
import { RiskLab } from "./pages/RiskLab";
import { Scanner } from "./pages/Scanner";
import { Strategies } from "./pages/Strategies";
import { System } from "./pages/System";

const PAGES = [
  { id: "command", label: "Command Center" },
  { id: "scanner", label: "Scanner" },
  { id: "agents", label: "Agents" },
  { id: "strategies", label: "Strategies" },
  { id: "portfolio", label: "Portfolio" },
  { id: "risk", label: "Risk" },
  { id: "journal", label: "Journal" },
  { id: "system", label: "System" },
] as const;

type PageId = (typeof PAGES)[number]["id"];

function initialPage(): PageId {
  const hash = window.location.hash.replace("#", "");
  return PAGES.some((page) => page.id === hash) ? (hash as PageId) : "command";
}

export function App() {
  const [page, setPage] = useState<PageId>(initialPage);
  const socket = useEventSocket();

  // The status poll is the fallback; the socket snapshot keeps it fresh in
  // between, so the header's lights react immediately.
  const status = usePoll(api.status, 6000);
  const current = socket.snapshot ?? status.data;

  // Keep the hash in sync with the active tab...
  useEffect(() => {
    if (window.location.hash.replace("#", "") !== page) {
      window.location.hash = page;
    }
  }, [page]);

  // ...and follow the hash when it changes from outside: the browser's Back
  // and Forward buttons, and links pasted straight to "#scanner". Without
  // this listener the URL updates but the view does not, which makes Back
  // appear broken.
  useEffect(() => {
    const onHashChange = () => setPage(initialPage());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return (
    <div className="app">
      <Header
        status={current}
        socketConnected={socket.connected}
        onChanged={status.refresh}
      />

      <nav className="nav">
        {PAGES.map((item) => (
          <button
            key={item.id}
            className={page === item.id ? "active" : ""}
            onClick={() => setPage(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <main className="main">
        {status.error && !current ? (
          <div className="banner danger">
            <strong>BACKEND UNREACHABLE</strong>
            <span>{status.error}</span>
            <span className="spacer" />
            <span className="faint">
              Start it with: cd trading/backend &amp;&amp; uvicorn app.main:app
            </span>
          </div>
        ) : null}

        {page === "command" ? (
          <CommandCenter status={current} events={socket.events} />
        ) : null}
        {page === "scanner" ? <Scanner /> : null}
        {page === "agents" ? <Agents /> : null}
        {page === "strategies" ? <Strategies /> : null}
        {page === "portfolio" ? <Portfolio /> : null}
        {page === "risk" ? <RiskLab /> : null}
        {page === "journal" ? <Journal /> : null}
        {page === "system" ? (
          <System
            status={current}
            events={socket.events}
            socketConnected={socket.connected}
          />
        ) : null}
      </main>

      <footer className="footer">
        ATLAS — personal research and paper-trading system. Not financial
        advice. Past performance does not guarantee future results.
      </footer>
    </div>
  );
}
