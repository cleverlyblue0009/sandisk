import ScoreBreakdown from "./ScoreBreakdown";

export default function ResultCard({ result, rank }) {
  return (
    <article className="glass-card p-5">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-primary-700">Rank #{rank}</p>
          <h3 className="text-lg font-bold text-ink-900">{result.filename}</h3>
          <p className="font-mono text-xs text-ink-500">{result.path}</p>
        </div>
        <span className="rounded-full bg-ink-900 px-3 py-1 text-xs font-semibold text-white">
          Score {result.final_score}
        </span>
      </div>

      <div className="mb-3 grid gap-2 text-sm text-ink-600 md:grid-cols-3">
        <p>
          <span className="font-semibold text-ink-800">Type:</span> {result.category}
        </p>
        <p>
          <span className="font-semibold text-ink-800">Extension:</span> {result.extension}
        </p>
        <p>
          <span className="font-semibold text-ink-800">Modified:</span>{" "}
          {new Date(result.modified_time).toLocaleString()}
        </p>
      </div>

      <div className="mb-3 rounded-xl border border-primary-100 bg-primary-50 p-3">
        <p className="text-sm font-semibold text-primary-700">Summary</p>
        <p className="mt-1 text-sm text-ink-700">{result.summary}</p>
      </div>

      <div className="mb-3 rounded-xl border border-ink-200 bg-white p-3">
        <p className="text-sm font-semibold text-ink-700">Why this file?</p>
        <p className="mt-1 text-sm text-ink-600">{result.explanation}</p>
      </div>

      {result.top_chunks?.length ? (
        <div className="mb-3 rounded-xl border border-ink-100 bg-white p-3">
          <p className="mb-2 text-sm font-semibold text-ink-700">Matched Snippets</p>
          {result.top_chunks.slice(0, 2).map((chunk) => (
            <p key={chunk.chunk_id} className="mb-2 text-sm text-ink-600">
              {chunk.content}
            </p>
          ))}
        </div>
      ) : null}

      <ScoreBreakdown scoreBreakdown={result.score_breakdown} />
    </article>
  );
}
