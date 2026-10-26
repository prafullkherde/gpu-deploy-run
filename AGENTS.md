# Agent Instructions

## Graphify

- When the local `graphify` CLI is available and `graphify-out/graph.json` exists, use `graphify query` to orient on codebase questions before broad source searches.
- If the CLI or graph is unavailable, continue with normal source inspection; do not install tools or generate a graph without the user's request.
- Treat graph links and summaries as navigation aids. Confirm behavior in source and tests before making changes.
- Use code-only extraction (`graphify extract . --code-only`) to build or refresh the local graph. It requires no model backend and avoids sending repository content to a semantic extraction service.
- Never trigger paid Vast.ai or GPU workflow actions as part of graph exploration.

## Repository Boundaries

- The GitHub Actions workflow is the control plane and GPU instances are billable. Preserve its cleanup and destroy guarantees.
- Keep generated renders, run logs, audiovisual inputs, and local environments out of the graph and out of commits unless explicitly requested.