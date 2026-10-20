# Architecture Decision Records (ADR)

Each record states the context, what was checked, the decision, the alternatives rejected and why, and the consequences. Records are not rewritten when later work changes details; the current behaviour is in [contracts](../contracts.md) and [architecture](../architecture.md).

| No. | Decision |
|---|---|
| [0001](0001-resident-server-own-vissim.md) | A resident MCP server owns the one Vissim it started, blocks only a protected list, and writes files only on an explicit save |
| [0002](0002-view-layout-and-capture.md) | View tools (`set_view`, `capture_view`); the view layout is a copy of the `.layx` |
| [0003](0003-launcher-through-server.md) | A launcher routes `.inpx` double-clicks through the server, because COM cannot attach to a Vissim you start yourself |
