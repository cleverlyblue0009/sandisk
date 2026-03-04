import { useEffect, useState } from "react";

export default function DirectorySelector({ selectedDirectory, isIndexing, onSelect }) {
  const [directory, setDirectory] = useState(selectedDirectory || "");

  useEffect(() => {
    if (selectedDirectory) {
      setDirectory(selectedDirectory);
    }
  }, [selectedDirectory]);

  const submit = (event) => {
    event.preventDefault();
    if (!directory.trim()) return;
    onSelect(directory.trim());
  };

  return (
    <form onSubmit={submit} className="glass-card p-5">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-bold text-ink-900">Directory Selection</h2>
        {isIndexing ? (
          <span className="rounded-full bg-primary-100 px-3 py-1 text-xs font-semibold text-primary-700">
            Indexing...
          </span>
        ) : (
          <span className="rounded-full bg-ink-100 px-3 py-1 text-xs font-semibold text-ink-700">
            Ready
          </span>
        )}
      </div>
      <p className="mb-4 text-sm text-ink-500">
        Enter a Windows path (example: C:\Users\YourUser\Documents) to start recursive indexing and real-time watch.
      </p>
      <div className="flex flex-col gap-3 md:flex-row">
        <input
          type="text"
          value={directory}
          onChange={(event) => setDirectory(event.target.value)}
          placeholder="C:\\Users\\Username\\Documents"
          className="w-full rounded-xl border border-ink-300 bg-white px-4 py-3 text-sm shadow-sm outline-none transition focus:border-primary-600"
        />
        <button
          type="submit"
          disabled={isIndexing}
          className="rounded-xl bg-ink-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-ink-700 disabled:cursor-not-allowed disabled:bg-ink-300"
        >
          {isIndexing ? "Indexing..." : "Start Indexing"}
        </button>
      </div>
      {selectedDirectory ? (
        <p className="mt-3 font-mono text-xs text-ink-500">Active: {selectedDirectory}</p>
      ) : null}
    </form>
  );
}
