import { useEffect, useMemo, useState } from "react";
import {
  askMemory,
  getActivitySuggestions,
  getApiTimeline,
  getInsights,
} from "./services/api";

const ASK_STARTERS = [
  "What did I do today?",
  "Describe my workflow",
  "What kind of videos do I watch?",
  "What documents did I use for bioinformatics lab?",
  "What was I doing yesterday afternoon?",
];

function formatDateLabel(dateText) {
  if (!dateText) return "Unknown date";
  const today = new Date();
  const todayText = today.toISOString().slice(0, 10);
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);

  if (dateText === todayText) return "Today";
  if (dateText === yesterday) return "Yesterday";

  const parsed = new Date(`${dateText}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return dateText;
  return parsed.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

function formatClock(timestampSeconds) {
  const ts = Number(timestampSeconds);
  if (!Number.isFinite(ts) || ts <= 0) return "--:--";
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function SummaryStrip({ items }) {
  if (!items?.length) return null;
  return (
    <div className="summary-strip">
      {items.map((item) => (
        <article key={`${item.label}-${item.value}`} className="summary-pill">
          <span>{item.label}</span>
          <strong>{item.value}</strong>
        </article>
      ))}
    </div>
  );
}

function SessionDetails({ session }) {
  return (
    <details className="expand-card">
      <summary>
        <div>
          <strong>{session.label || session.application_used || "Activity"}</strong>
          <span>{session.time_window || `${formatClock(session.start_time)} - ${formatClock(session.end_time)}`}</span>
        </div>
        <span>{session.duration || "0m"}</span>
      </summary>
      <div className="expand-body">
        <div className="meta-grid">
          <div>
            <span className="mini-label">Application</span>
            <strong>{session.application_used || "Unknown app"}</strong>
          </div>
          <div>
            <span className="mini-label">Category</span>
            <strong>{session.category || "other"}</strong>
          </div>
          {session.youtube_category ? (
            <div>
              <span className="mini-label">YouTube Category</span>
              <strong>{session.youtube_category}</strong>
            </div>
          ) : null}
          {session.domain ? (
            <div>
              <span className="mini-label">Site</span>
              <strong>{session.domain}</strong>
            </div>
          ) : null}
        </div>
      </div>
    </details>
  );
}

function DocumentCard({ document }) {
  const sessions = document.used_sessions || [];
  return (
    <details className="expand-card">
      <summary>
        <div>
          <strong>{document.file_name || "Untitled document"}</strong>
          <span>
            {document.application_used
              ? `${document.application_used}${document.last_used_window ? ` on ${document.last_used_window}` : ""}`
              : document.last_used || "No recent usage"}
          </span>
        </div>
        <span>{document.last_used || ""}</span>
      </summary>
      <div className="expand-body">
        {document.summary ? (
          <div className="content-block">
            <p className="section-title">Summary</p>
            <p className="body-copy">{document.summary}</p>
          </div>
        ) : null}

        {document.topics?.length ? (
          <div className="content-block">
            <p className="section-title">Topics</p>
            <div className="topic-row">
              {document.topics.map((topic) => (
                <span key={`${document.file_name}-${topic}`} className="topic-chip">
                  {topic}
                </span>
              ))}
            </div>
          </div>
        ) : null}

        {document.key_snippets?.length ? (
          <div className="content-block">
            <p className="section-title">Key Snippets</p>
            <ul className="detail-list">
              {document.key_snippets.map((snippet) => (
                <li key={`${document.file_name}-${snippet.slice(0, 20)}`}>{snippet}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="meta-grid">
          <div>
            <span className="mini-label">Last Used</span>
            <strong>{document.last_used || "Unknown"}</strong>
          </div>
          <div>
            <span className="mini-label">Application</span>
            <strong>{document.application_used || "Unknown"}</strong>
          </div>
        </div>

        {sessions.length ? (
          <div className="content-block">
            <p className="section-title">Related Sessions</p>
            <div className="stack-list">
              {sessions.slice(0, 3).map((session) => (
                <SessionDetails
                  key={`${document.file_name}-${session.session_id || session.start_time}`}
                  session={session}
                />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </details>
  );
}

function AssistantBubble({ payload }) {
  const documents = payload?.related_documents || [];
  const activitySessions = payload?.activity_sessions || [];
  const browserSessions = payload?.browser_sessions || [];
  const workflowInsights = payload?.workflow_analysis?.insights || [];
  const youtubeCategories = payload?.youtube_analysis?.top_categories || [];

  return (
    <article className="chat-bubble assistant-bubble">
      <div className="bubble-meta">
        <span>Assistant</span>
        {payload?.intent ? <span>{payload.intent.replace(/_/g, " ")}</span> : null}
      </div>

      <p className="bubble-text">{payload?.assistant_response || payload?.answer || "I could not find anything yet."}</p>

      <SummaryStrip items={payload?.structured_summary || []} />

      {payload?.short_summary ? (
        <div className="panel-block">
          <p className="section-title">Quick Summary</p>
          <p className="body-copy">{payload.short_summary}</p>
        </div>
      ) : null}

      {workflowInsights.length ? (
        <div className="panel-block">
          <p className="section-title">Workflow Insights</p>
          <ul className="detail-list compact">
            {workflowInsights.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {youtubeCategories.length ? (
        <div className="panel-block">
          <p className="section-title">Watch Pattern</p>
          <div className="stack-list">
            <p className="body-copy">{payload.youtube_analysis.summary}</p>
            <div className="topic-row">
              {youtubeCategories.map((item) => (
                <span key={item.category} className="topic-chip">
                  {item.category} - {item.duration}
                </span>
              ))}
            </div>
          </div>
        </div>
      ) : null}

      {documents.length ? (
        <div className="panel-block">
          <p className="section-title">Related Documents</p>
          <div className="stack-list">
            {documents.map((document) => (
              <DocumentCard
                key={`${document.file_name}-${document.last_used_timestamp || document.last_used || "doc"}`}
                document={document}
              />
            ))}
          </div>
        </div>
      ) : null}

      {activitySessions.length ? (
        <div className="panel-block">
          <p className="section-title">Activity Sessions</p>
          <div className="stack-list">
            {activitySessions.slice(0, 5).map((session) => (
              <SessionDetails key={session.session_id || `${session.label}-${session.start_time}`} session={session} />
            ))}
          </div>
        </div>
      ) : null}

      {browserSessions.length ? (
        <div className="panel-block">
          <p className="section-title">Browser Sessions</p>
          <ul className="detail-list">
            {browserSessions.slice(0, 5).map((session) => (
              <li key={session.session_id || `${session.label}-${session.start_time}`}>
                <strong>{session.label}</strong>
                <span>{session.time_window}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </article>
  );
}

function AskTab({
  question,
  setQuestion,
  onSubmit,
  conversation,
  loading,
}) {
  return (
    <section className="tab-pane ask-pane">
      <header className="ask-hero">
        <div>
          <p className="eyebrow">Ask Tab</p>
          <h2>Unified memory reconstruction</h2>
          <p className="hero-copy">
            Ask about workflow, browsing, documents, or YouTube habits. The assistant merges them into one answer.
          </p>
        </div>
      </header>

      <div className="starter-grid">
        {ASK_STARTERS.map((item) => (
          <button
            key={item}
            className="starter-btn"
            type="button"
            onClick={() => onSubmit(item)}
            disabled={loading}
          >
            {item}
          </button>
        ))}
      </div>

      <div className="chat-log">
        {conversation.length === 0 ? (
          <div className="empty-chat">
            <p>No conversation yet.</p>
            <span>Try one of the prompts above to reconstruct your recent digital activity.</span>
          </div>
        ) : (
          conversation.map((message) => {
            if (message.role === "user") {
              return (
                <article key={message.id} className="chat-bubble user-bubble">
                  <div className="bubble-meta">
                    <span>You</span>
                  </div>
                  <p className="bubble-text">{message.text}</p>
                </article>
              );
            }

            if (message.pending) {
              return (
                <article key={message.id} className="chat-bubble assistant-bubble pending-bubble">
                  <div className="bubble-meta">
                    <span>Assistant</span>
                    <span>thinking</span>
                  </div>
                  <p className="bubble-text">Pulling together sessions, browsing, YouTube, and documents...</p>
                </article>
              );
            }

            return <AssistantBubble key={message.id} payload={message.payload} />;
          })
        )}
      </div>

      <form
        className="ask-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit(question);
        }}
      >
        <input
          className="ask-input"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask your memory assistant..."
        />
        <button className="ask-button" type="submit" disabled={loading || !question.trim()}>
          Ask
        </button>
      </form>
    </section>
  );
}

function TimelineTab({ timelineData, loading }) {
  const days = timelineData?.timeline || [];

  return (
    <section className="tab-pane timeline-pane">
      {loading && <p className="loading-note">Loading timeline...</p>}
      {!loading && days.length === 0 && <p className="empty-note">No activity sessions found yet.</p>}

      {days.map((day) => (
        <article key={day.date} className="timeline-day">
          <h3>{formatDateLabel(day.date)}</h3>

          <ul className="simple-list timeline-list">
            {(day.entries || []).map((entry) => (
              <li key={entry.session_id || `${entry.start_time}-${entry.app_name}`}>
                <div className="timeline-main">
                  <span className="timeline-time">{entry.start_time} - {entry.end_time}</span>
                  <span className="timeline-app">{entry.app_name}</span>
                </div>
                <div className="timeline-meta">
                  <span>{entry.category}</span>
                  <span>{entry.duration}</span>
                  {entry.document_name ? <span>{entry.document_name}</span> : null}
                  {!entry.document_name && entry.domain ? <span>{entry.domain}</span> : null}
                </div>
              </li>
            ))}
          </ul>
        </article>
      ))}
    </section>
  );
}

function InsightsTab({ summaryCards, insights, suggestions, loading }) {
  return (
    <section className="tab-pane insights-pane">
      {loading && <p className="loading-note">Loading insights...</p>}

      {!loading && (
        <>
          <div className="summary-cards">
            {summaryCards.map((card) => (
              <article key={card.title} className="summary-card">
                <p className="summary-label">{card.title}</p>
                <p className="summary-value">{card.duration}</p>
              </article>
            ))}
          </div>

          <div className="insight-block">
            <p className="section-title">Workflow Insights</p>
            <ul className="simple-list">
              {insights.map((item) => (
                <li key={`insight-${item}`}>{item}</li>
              ))}
            </ul>
          </div>

          <div className="insight-block">
            <p className="section-title">Activity Suggestions</p>
            <ul className="simple-list">
              {suggestions.map((item) => (
                <li key={`suggest-${item}`}>{item}</li>
              ))}
            </ul>
          </div>
        </>
      )}
    </section>
  );
}

export default function App() {
  const [activeTab, setActiveTab] = useState("ask");
  const [question, setQuestion] = useState("");
  const [conversation, setConversation] = useState([]);

  const [timelinePayload, setTimelinePayload] = useState(null);
  const [summaryCards, setSummaryCards] = useState([]);
  const [insightsList, setInsightsList] = useState([]);
  const [suggestionsList, setSuggestionsList] = useState([]);

  const [loadingAsk, setLoadingAsk] = useState(false);
  const [loadingTimeline, setLoadingTimeline] = useState(false);
  const [loadingInsights, setLoadingInsights] = useState(false);
  const [error, setError] = useState("");

  const bannerText = useMemo(() => timelinePayload?.banner || "Your Computer Memory", [timelinePayload]);

  async function loadTimeline(days = 30) {
    setLoadingTimeline(true);
    setError("");
    try {
      const payload = await getApiTimeline(days);
      setTimelinePayload(payload || null);
    } catch (err) {
      setError(err.message || "Failed to load timeline");
    } finally {
      setLoadingTimeline(false);
    }
  }

  async function loadInsights(days = 14) {
    setLoadingInsights(true);
    setError("");
    try {
      const [insightPayload, suggestionPayload] = await Promise.all([
        getInsights(days),
        getActivitySuggestions(Math.min(days, 7)),
      ]);
      setSummaryCards(insightPayload?.summary_cards || []);
      setInsightsList(insightPayload?.insights || []);
      setSuggestionsList(suggestionPayload?.suggestions || []);
    } catch (err) {
      setError(err.message || "Failed to load insights");
    } finally {
      setLoadingInsights(false);
    }
  }

  async function submitAsk(rawQuestion) {
    const normalized = (rawQuestion || "").trim();
    if (!normalized) return;

    const userId = `user-${Date.now()}`;
    const pendingId = `assistant-${Date.now()}-pending`;
    setLoadingAsk(true);
    setError("");
    setQuestion("");
    setConversation((prev) => [
      ...prev,
      { id: userId, role: "user", text: normalized },
      { id: pendingId, role: "assistant", pending: true },
    ]);

    try {
      const payload = await askMemory(normalized);
      setConversation((prev) =>
        prev.map((item) =>
          item.id === pendingId
            ? { id: `assistant-${Date.now()}`, role: "assistant", payload: payload || null }
            : item
        )
      );
      setActiveTab("ask");
    } catch (err) {
      const detail = err.message || "Failed to query memory";
      setError(detail);
      setConversation((prev) =>
        prev.map((item) =>
          item.id === pendingId
            ? {
                id: `assistant-${Date.now()}-error`,
                role: "assistant",
                payload: {
                  assistant_response: detail,
                  short_summary: detail,
                  structured_summary: [],
                  related_documents: [],
                  activity_sessions: [],
                  browser_sessions: [],
                  workflow_analysis: { insights: [] },
                  youtube_analysis: { top_categories: [] },
                },
              }
            : item
        )
      );
    } finally {
      setLoadingAsk(false);
    }
  }

  useEffect(() => {
    void loadTimeline(30);
    void loadInsights(14);
  }, []);

  return (
    <main className="memory-app">
      <section className="memory-shell">
        <header className="memory-header">
          <h1>{bannerText}</h1>
          <p>PERSONAL MEMORY ASSISTANT</p>
        </header>

        <nav className="memory-tabs">
          <button
            type="button"
            className={activeTab === "ask" ? "tab-btn active" : "tab-btn"}
            onClick={() => setActiveTab("ask")}
          >
            ASK
          </button>
          <button
            type="button"
            className={activeTab === "timeline" ? "tab-btn active" : "tab-btn"}
            onClick={() => {
              setActiveTab("timeline");
              if (!timelinePayload) {
                void loadTimeline(30);
              }
            }}
          >
            TIMELINE
          </button>
          <button
            type="button"
            className={activeTab === "insights" ? "tab-btn active" : "tab-btn"}
            onClick={() => {
              setActiveTab("insights");
              if (!insightsList.length && !summaryCards.length) {
                void loadInsights(14);
              }
            }}
          >
            INSIGHTS
          </button>
        </nav>

        {error ? <p className="error-banner">{error}</p> : null}

        <section className="memory-content">
          {activeTab === "ask" ? (
            <AskTab
              question={question}
              setQuestion={setQuestion}
              onSubmit={submitAsk}
              conversation={conversation}
              loading={loadingAsk}
            />
          ) : null}

          {activeTab === "timeline" ? (
            <TimelineTab timelineData={timelinePayload} loading={loadingTimeline} />
          ) : null}

          {activeTab === "insights" ? (
            <InsightsTab
              summaryCards={summaryCards}
              insights={insightsList}
              suggestions={suggestionsList}
              loading={loadingInsights}
            />
          ) : null}
        </section>
      </section>
    </main>
  );
}
