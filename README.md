# TaxAssist — Local-first Tax Policy Knowledge Base

**A searchable, fully local knowledge base of Chinese tax policy** — it collects the
public policy documents of the State Taxation Administration and 31 provincial tax
bureaus, determines which of them are still in force, and makes all of it
full-text searchable. Nothing leaves the machine.

中文说明见 **[README.zh-CN.md](README.zh-CN.md)**.

---

## What problem it solves

Looking up a policy is not the hard part. Three things are:

| Problem | How this project addresses it |
|---|---|
| **Documents are scattered across 32 official sites** | One index, full-text searchable, with filters by region / column / validity / year / tax type |
| **You cannot tell which documents are still valid** | Every document carries a validity verdict **with its evidence** — official marking, official repeal catalogue, or "no repeal found" (explicitly *not* a confirmation) |
| **Text buried inside attachments is invisible** | PDF, Word, Excel and archives are parsed and indexed — including legacy `.doc` / `.wps` via local WPS COM conversion |

---

## Three non-negotiable boundaries

This project is built around three rules that are enforced in code, not just promised:

1. **Client data never leaves the machine.** Policy text and translations live in a
   local SQLite database. The assistant runs on a **local** language model. No
   customer fact is ever written to any database or sent to a third party.
2. **Network access is only for fetching public policy.** There is an outbound
   denylist guard; scraping targets are official government sites.
3. **Every conclusion is traceable.** A validity verdict without its source snippet
   and URL is treated as a bug. The UI shows "official marking / inferred from
   another document / no repeal evidence" and never dresses the last one up as
   confirmation.

---

## Quick start

Requires Python 3.12. The commands below assume Windows (the deployment target);
the code itself is portable.

```bash
# 1) Install
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2) Create / upgrade the database
.venv/Scripts/python.exe -m taxassist initdb

# 3) First full import (~5,000+ documents; windowed by year)
.venv/Scripts/python.exe -m taxassist collect --full --year-from 1984

# 4) Daily incremental fetch (7-day overlap window, deduplicated by doc_uid)
.venv/Scripts/python.exe -m taxassist collect --days 7

# 5) Run everything (web + background worker), no console windows, logs to data/logs/
.venv/Scripts/python.exe -m taxassist service start
.venv/Scripts/python.exe -m taxassist service status
.venv/Scripts/python.exe -m taxassist service stop
```

The web UI listens on `http://127.0.0.1:8765/` by default.

> `service start` manages the web service and the worker. It does **not** start
> ollama — that binary lives at a machine-specific path, so the command only checks
> port 11434 and tells you what to do. Set `TAXASSIST_OLLAMA_EXE` if you want it
> handled too.

---

## Ways to query the knowledge base

All read-only, all local. Pick whichever fits your workflow.

| Interface | Command | Use case |
|---|---|---|
| **Web UI** | `taxassist service start` | Humans: search, browse, read with filters |
| **Tax assistant** | web UI `/assistant` | Ask a question grounded in your facts; returns an analysis **with cited policy excerpts** |
| **CLI search** | `taxassist search <keywords>` | Scripts and quick checks |
| **Local JSON API** | `taxassist kb` | Local integrations (`http://127.0.0.1:8767/`, no auth, binds loopback only) |
| **HTTP API + MCP** | `taxassist serve --expose` then `/api/*` | Remote clients, with per-account keys, metering and billing |
| **MCP (stdio)** | `taxassist mcp` | Attach to Claude Desktop / Cursor as a local server |

The HTTP API and MCP-over-HTTP entry point are covered in the in-app docs
(`/account/docs`), including one-command setup for Claude Code, Cursor and
Claude Desktop.

---

## How the data is collected

Scraping targets the official policy databases. A few things worth knowing:

- **Raw responses are archived per page** before parsing, so a parser improvement
  can be replayed offline (`taxassist reparse`) without re-fetching.
- **Attachments are parsed by content, not by extension.** Real-world data: files
  named `.docx` whose content is legacy OLE2, and files named `.xls` whose content
  is actually a ZIP (i.e. real `.xlsx`). The parser sniffs magic bytes and routes
  accordingly.
- **Legacy formats go through local WPS** (`KWPS.Application` / `KET.Application`
  COM) to produce a modern file, which is then parsed.
- **Attachments with no text layer** (scanned PDFs) are reported as
  `no_text_layer` — deliberately *not* counted as failures; they need OCR, which is
  a different job.

### Completeness contract

If a fetch returns fewer documents than the source reports, the run is marked
incomplete and the web UI shows a warning. Silent partial data is treated as worse
than a visible failure, because it corrupts every downstream judgement.

---

## Validity determination

Evidence is ranked, and the ranking is enforced:

1. **Official validity marking on the document's own page** — most reliable, low fill rate
2. Official list interface `xxgk_aging` — fill rate ~2%, effectively unusable
3. **Repeal statements inside other documents' text** ("this announcement repeals …") — parsed, may mis-attribute
4. **Official "repealed documents catalogue" announcements** — most authoritative.
   The list lives in an `.xls` attachment (the announcement body is one sentence);
   it is parsed into `official_catalog` relations — 1,743 extracted, 1,312 matched
   to documents in the library

Two design constraints were paid for in bugs and are now non-negotiable:

- **The default verdict says it is a default.** Most documents are valid; labelling
  everything "unknown" would make the system useless, but presenting an inference as
  an official confirmation would be worse than having no system at all.
- **Relation extraction and validity determination run in separate passes.** Doing
  both in one loop creates an order dependency: a document judged before a later
  document's repeal statement against it is seen gets wrongly marked "in force" —
  a directional error (treating repealed as valid) that is worse than a miss.

---

## Project layout

```
src/taxassist/
  collect/          scrapers (national + provincial), detail pages, attachments
  effect.py         validity determination and citation extraction
  kb.py             search (FTS + BM25 weights), snippets, citations
  assistant.py      local-LLM tax assistant (fact extraction → retrieval → report)
  billing.py        customers, dual balances, API keys, usage metering
  service.py        start / stop / status for the whole system, windowless
  worker.py         background stages: fetch → publish → verify (+ translate)
  web/              FastAPI app, split into one module per concern
tests/              ~470 tests
```

---

## Status

| | |
|---|---|
| Documents in library | ~13,900 |
| Title translations | 100% |
| Body translations | ~44% (ongoing, local model) |
| Attachment parsing | ~84% (scanned PDFs need OCR) |
| Tests | all passing; `ruff` clean |

**Not done yet:** OCR for scanned attachments; the MCP configuration has been
verified at the protocol level with `curl`, but not yet in a real Cursor / Claude
Desktop client (no such client on the development machine).

---

## Deployment

**Do not deploy this to Cloudflare Workers / Pages.** Workers is a JS/TS edge
runtime — it cannot run Python/FastAPI, has no filesystem, and cannot host a SQLite
database. The architecture is deliberately local-first.

For remote access, the intended path is a tunnel (Cloudflare Tunnel or Tailscale)
in front of the loopback service, with the auth gate enabled (`--expose`). See the
Chinese README for the step-by-step.

---

## License

Private project, not yet licensed for redistribution.
