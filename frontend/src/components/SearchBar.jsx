import { useState } from "react";

export default function SearchBar({ onSearch, isLoading }) {
  const [query, setQuery] = useState("");

  const submit = (event) => {
    event.preventDefault();
    if (!query.trim()) return;
    onSearch(query.trim());
  };

  return (
    <form onSubmit={submit} className="glass-card p-5">
      <h2 className="mb-3 text-lg font-bold text-ink-900">Natural Language Search</h2>
      <p className="mb-4 text-sm text-ink-500">
        Ask naturally, like: Could you retrieve the stuff I used for OS exam?
      </p>
      <div className="flex flex-col gap-3 md:flex-row">
        <input
          type="text"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Type your question..."
          className="w-full rounded-xl border border-ink-300 bg-white px-4 py-3 text-sm shadow-sm outline-none transition focus:border-primary-600"
        />
        <button
          type="submit"
          disabled={isLoading}
          className="rounded-xl bg-primary-700 px-5 py-3 text-sm font-semibold text-white transition hover:bg-primary-600 disabled:cursor-not-allowed disabled:bg-primary-100 disabled:text-primary-700"
        >
          {isLoading ? "Searching..." : "Search"}
        </button>
      </div>
    </form>
  );
}
