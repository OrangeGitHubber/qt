import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useMemo, useState } from "react";
import { AboutInfo, getAbout, getChangelogMarkdown, getRoadmapMarkdown } from "../api";

type DocTab = "changelog" | "roadmap";

// Base for rewriting relative doc links (e.g. how-it-works.md) to GitHub so
// they resolve in the deployed app. Updated once the /api/about info loads.
let repoBase = "https://github.com/OrangeGitHubber/qt";

// Post-sanitize: make relative doc links point at GitHub and open every link
// in a new tab. Registered once; DOMPurify hooks are global.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    const href = node.getAttribute("href") || "";
    const isRelative = href && !/^https?:\/\//i.test(href) && !href.startsWith("#");
    if (isRelative) {
      node.setAttribute("href", `${repoBase}/blob/main/docs/${href.replace(/^\.?\//, "")}`);
    }
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noreferrer");
  }
});

// Render trusted in-repo markdown to sanitized HTML. Content is our own docs,
// but we sanitize anyway (defense in depth). Never throws — a render failure
// must not take down the page.
function renderMarkdown(md: string): string {
  try {
    const raw = marked.parse(md, { async: false }) as string;
    return DOMPurify.sanitize(raw);
  } catch {
    return "<p>Couldn't render this document.</p>";
  }
}

function Markdown({ source }: { source: string | null }) {
  const html = useMemo(() => (source ? renderMarkdown(source) : ""), [source]);
  if (source === null) return <p className="hint">Loading…</p>;
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}

export default function About() {
  const [info, setInfo] = useState<AboutInfo | null>(null);
  const [changelog, setChangelog] = useState<string | null>(null);
  const [roadmap, setRoadmap] = useState<string | null>(null);
  const [tab, setTab] = useState<DocTab>("changelog");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getAbout()
      .then((i) => {
        setInfo(i);
        if (i.repo_url) repoBase = i.repo_url;
      })
      .catch((e: Error) => setError(e.message));
    getChangelogMarkdown()
      .then((r) => setChangelog(r.markdown))
      .catch(() => setChangelog("_Changelog unavailable._"));
    getRoadmapMarkdown()
      .then((r) => setRoadmap(r.markdown))
      .catch(() => setRoadmap("_Roadmap unavailable._"));
  }, []);

  return (
    <>
      <div className="toolbar">
        <h2>About QT</h2>
      </div>

      <div className="card about-identity">
        <div>
          <div className="about-name">{info?.name ?? "QT Auto-Trader"}</div>
          <div className="hint">
            A self-hosted, paper-first momentum auto-trader. Built deliberately because it
            eventually touches real money.
          </div>
        </div>
        <dl className="about-meta">
          <div>
            <dt>Version</dt>
            <dd>{info ? `v${info.version}` : "…"}</dd>
          </div>
          <div>
            <dt>Build</dt>
            <dd title="The exact commit this container was built from">
              <code>{info?.git_sha ?? "…"}</code>
            </dd>
          </div>
          <div>
            <dt>Built</dt>
            <dd>{info?.build_date ? info.build_date : "local / dev"}</dd>
          </div>
          <div>
            <dt>License</dt>
            <dd>{info?.license ?? "GPLv3"}</dd>
          </div>
          <div>
            <dt>Source</dt>
            <dd>
              {info ? (
                <a href={info.repo_url} target="_blank" rel="noreferrer">
                  GitHub
                </a>
              ) : (
                "…"
              )}
            </dd>
          </div>
        </dl>
      </div>

      {error && <div className="card error">Couldn't load build info: {error}</div>}

      <nav className="tabs subtabs">
        <button className={tab === "changelog" ? "tab active" : "tab"} onClick={() => setTab("changelog")}>
          What&apos;s changed
        </button>
        <button className={tab === "roadmap" ? "tab active" : "tab"} onClick={() => setTab("roadmap")}>
          Roadmap
        </button>
      </nav>

      <div className="card">
        {tab === "changelog" ? <Markdown source={changelog} /> : <Markdown source={roadmap} />}
      </div>
    </>
  );
}
