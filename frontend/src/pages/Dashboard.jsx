import { useEffect, useMemo, useState } from "react";
import DirectorySelector from "../components/DirectorySelector";
import ResultCard from "../components/ResultCard";
import SearchBar from "../components/SearchBar";
import { getIndexStatus, searchMemory, selectDirectory } from "../services/api";

function StatusPanel({ status }) {
  const progress = useMemo(() => {
    if (!status?.total_supported_files) return 0;
    return Math.min(
      100,
      Math.round((status.scanned_files / status.total_supported_files) * 100)
    );
  }, [status]);

  return (
    <section className="glass-card p-5">
      <h2 className="mb-3 text-lg font-bold text-ink-900">Index Progress</h2>
      <div className="mb-3 h-2 overflow-hidden rounded-full bg-ink-100">
        <div
          className="h-full rounded-full bg-primary-700 transition-all"
          style={{ width: `${progress}%` }}
        />
      </div>
      <div className="grid gap-2 text-sm text-ink-600 md:grid-cols-2">
        <p>Progress: {progress}%</p>
        <p>Watcher: {status?.watcher_active ? "Active" : "Inactive"}</p>
        <p>Total Supported: {status?.total_supported_files || 0}</p>
        <p>Scanned: {status?.scanned_files || 0}</p>
        <p>Indexed: {status?.indexed_files || 0}</p>
        <p>Updated: {status?.updated_files || 0}</p>
        <p>Skipped: {status?.skipped_files || 0}</p>
        <p>Failed: {status?.failed_files || 0}</p>
      </div>
      {status?.watcher_directory ? (
        <p className="mt-3 font-mono text-xs text-ink-500">{status.watcher_directory}</p>
      ) : null}
    </section>
  );
}

function QueryInsight({ analysis }) {
  if (!analysis) return null;
  return (
    <section className="glass-card p-5">
      <h2 className="mb-2 text-lg font-bold text-ink-900">Query Understanding (Groq)</h2>
      <p className="mb-2 text-sm text-ink-600">
        <span className="font-semibold text-ink-800">Intent:</span> {analysis.intent || "N/A"}
      </p>
      <p className="mb-2 text-sm text-ink-600">
        <span className="font-semibold text-ink-800">Expanded Query:</span>{" "}
        {analysis.expanded_query || "N/A"}
      </p>
      <div className="mb-2 flex flex-wrap gap-2">
        {(analysis.keywords || []).map((keyword) => (
          <span
            key={keyword}
            className="rounded-full border border-primary-100 bg-primary-50 px-3 py-1 text-xs font-semibold text-primary-700"
          >
            {keyword}
          </span>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {(analysis.time_hints || []).map((hint) => (
          <span
            key={hint}
            className="rounded-full border border-ink-200 bg-white px-3 py-1 text-xs font-semibold text-ink-700"
          >
            {hint}
          </span>
        ))}
      </div>
    </section>
  );
}

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [results, setResults] = useState([]);
  const [analysis, setAnalysis] = useState(null);
  const [error, setError] = useState("");
  const [isDirectoryBusy, setIsDirectoryBusy] = useState(false);
  const [isSearchLoading, setIsSearchLoading] = useState(false);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const nextStatus = await getIndexStatus();
        if (active) {
          setStatus(nextStatus);
        }
      } catch (apiError) {
        if (active) {
          setError(apiError.message);
        }
      }
    };

    poll();
    const interval = setInterval(poll, 2500);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, []);

  const onSelectDirectory = async (directory) => {
    setError("");
    setIsDirectoryBusy(true);
    try {
      await selectDirectory(directory);
      const nextStatus = await getIndexStatus();
      setStatus(nextStatus);
      setResults([]);
      setAnalysis(null);
    } catch (apiError) {
      setError(apiError.message);
    } finally {
      setIsDirectoryBusy(false);
    }
  };

  const onSearch = async (query) => {
    setError("");
    setIsSearchLoading(true);
    try {
      const response = await searchMemory(query);
      setResults(response.results || []);
      setAnalysis(response.analysis || null);
    } catch (apiError) {
      setError(apiError.message);
    } finally {
      setIsSearchLoading(false);
    }
  };

  return (
    <main className="mx-auto max-w-6xl px-4 py-8 md:px-6">
      <header className="mb-6">
        <p className="text-sm font-semibold uppercase tracking-widest text-primary-700">
          Windows Personal Memory Assistant
        </p>
        <h1 className="mt-2 text-3xl font-extrabold text-ink-900 md:text-4xl">
          Semantic Layer Over File Explorer
        </h1>
        <p className="mt-2 max-w-3xl text-sm text-ink-600 md:text-base">
          Recursively index your files, watch changes in real time, and retrieve files with natural
          language plus transparent ranking.
        </p>
      </header>

      <section className="mb-4">
        <DirectorySelector
          selectedDirectory={status?.selected_directory}
          isIndexing={Boolean(status?.is_indexing || isDirectoryBusy)}
          onSelect={onSelectDirectory}
        />
      </section>

      <section className="mb-4 grid gap-4 lg:grid-cols-2">
        <StatusPanel status={status} />
        <SearchBar onSearch={onSearch} isLoading={isSearchLoading || Boolean(status?.is_indexing)} />
      </section>

      {analysis ? (
        <section className="mb-4">
          <QueryInsight analysis={analysis} />
        </section>
      ) : null}

      {error ? (
        <section className="mb-4 rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </section>
      ) : null}

      <section className="space-y-4">
        {results.length === 0 ? (
          <div className="glass-card p-6 text-sm text-ink-500">
            No results yet. Select a directory, wait for indexing, and run a natural language query.
          </div>
        ) : (
          results.map((result, index) => (
            <ResultCard
              key={`${result.file_id}-${result.path}`}
              result={result}
              rank={index + 1}
            />
          ))
        )}
      </section>
    </main>
  );
}
