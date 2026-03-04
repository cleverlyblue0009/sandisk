export default function ScoreBreakdown({ scoreBreakdown }) {
  if (!scoreBreakdown) return null;

  return (
    <details className="rounded-xl border border-ink-100 bg-ink-50/70 p-3">
      <summary className="cursor-pointer text-sm font-semibold text-ink-700">
        Score Breakdown
      </summary>
      <div className="mt-3 space-y-1 text-xs text-ink-600">
        <p className="font-mono">semantic_score: {scoreBreakdown.semantic_score}</p>
        <p className="font-mono">recency_score: {scoreBreakdown.recency_score}</p>
        <p className="font-mono">keyword_match_score: {scoreBreakdown.keyword_match_score}</p>
        <p className="font-mono">semantic_component: {scoreBreakdown.semantic_component}</p>
        <p className="font-mono">recency_component: {scoreBreakdown.recency_component}</p>
        <p className="font-mono">keyword_component: {scoreBreakdown.keyword_component}</p>
        <p className="font-mono text-ink-800">formula: {scoreBreakdown.formula}</p>
      </div>
    </details>
  );
}
