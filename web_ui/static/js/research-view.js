/*
 * ResearchView — Claude Research-style live research card.
 *
 * Owns a single DOM node that renders:
 *   - A phase timeline (Planning → Gathering → Analyzing → Writing report)
 *   - Discovered sub-questions
 *   - Sources as they come in (numbered citation cards)
 *   - The final professional markdown report with inline [n] citations
 *     that link back to the source cards
 *
 * Consumes SSE events from  GET /api/local-ai/research/{session_id}/events
 * produced by AdvancedResearcher.
 */

(function (global) {
  "use strict";

  const PHASE_DEFS = [
    { name: "planning",     label: "Planning",     icon: "fa-stream" },
    { name: "gathering",    label: "Gathering",    icon: "fa-globe" },
    { name: "analyzing",    label: "Analyzing",    icon: "fa-microscope" },
    { name: "synthesizing", label: "Writing",      icon: "fa-feather-alt" },
  ];

  // ── Minimal, safe markdown renderer for the report body ────────────
  //
  // Handles what the synthesis prompt produces: # / ## / ### headings,
  // **bold**, *italic*, `inline code`, - / * bullets, numbered lists,
  // paragraphs, and inline [n] citation markers. All raw text is escaped.

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderInline(text, validIds) {
    let out = escapeHtml(text);

    // Inline code: `code`
    out = out.replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`);

    // Bold: **text**
    out = out.replace(/\*\*([^*\n]+)\*\*/g, (_, c) => `<strong>${c}</strong>`);

    // Italic: *text*
    out = out.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s.,;:!?)\]]|$)/g,
      (_, pre, c) => `${pre}<em>${c}</em>`);

    // Citations: [1] or [1, 2] or [1,2,3]
    out = out.replace(/\[([0-9]+(?:\s*,\s*[0-9]+)*)\]/g, (full, group) => {
      const ids = group.split(",").map(s => s.trim()).filter(Boolean);
      const parts = ids.map(idStr => {
        const id = parseInt(idStr, 10);
        if (!validIds || validIds.has(id)) {
          return `<a href="#" class="citation-link" data-citation-id="${id}">[${id}]</a>`;
        }
        return `[${id}]`;
      });
      return parts.join("");
    });

    return out;
  }

  function renderMarkdown(md, validIds) {
    if (!md) return "";
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let i = 0;
    let inList = false;
    let listTag = "ul";

    function closeList() {
      if (inList) {
        out.push(`</${listTag}>`);
        inList = false;
      }
    }

    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      // Blank line
      if (!trimmed) {
        closeList();
        i++;
        continue;
      }

      // Headings
      const h = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if (h) {
        closeList();
        const level = h[1].length;
        out.push(`<h${level}>${renderInline(h[2], validIds)}</h${level}>`);
        i++;
        continue;
      }

      // Bullet list
      const bullet = trimmed.match(/^[-*•]\s+(.+)$/);
      if (bullet) {
        if (!inList || listTag !== "ul") {
          closeList();
          out.push(`<ul>`);
          inList = true;
          listTag = "ul";
        }
        out.push(`<li>${renderInline(bullet[1], validIds)}</li>`);
        i++;
        continue;
      }

      // Ordered list
      const ord = trimmed.match(/^\d+[.)]\s+(.+)$/);
      if (ord) {
        if (!inList || listTag !== "ol") {
          closeList();
          out.push(`<ol>`);
          inList = true;
          listTag = "ol";
        }
        out.push(`<li>${renderInline(ord[1], validIds)}</li>`);
        i++;
        continue;
      }

      // Block quote
      if (trimmed.startsWith("> ")) {
        closeList();
        const chunks = [];
        while (i < lines.length && lines[i].trim().startsWith("> ")) {
          chunks.push(lines[i].trim().slice(2));
          i++;
        }
        out.push(`<blockquote>${renderInline(chunks.join(" "), validIds)}</blockquote>`);
        continue;
      }

      // Paragraph — accumulate consecutive non-blank, non-special lines
      closeList();
      const para = [trimmed];
      i++;
      while (i < lines.length) {
        const next = lines[i].trim();
        if (!next) break;
        if (/^(#{1,4})\s+/.test(next)) break;
        if (/^[-*•]\s+/.test(next)) break;
        if (/^\d+[.)]\s+/.test(next)) break;
        if (next.startsWith("> ")) break;
        para.push(next);
        i++;
      }
      out.push(`<p>${renderInline(para.join(" "), validIds)}</p>`);
    }

    closeList();
    return out.join("\n");
  }

  // ── ResearchView ──────────────────────────────────────────────────

  class ResearchView {
    constructor(query, depth) {
      this.query = query;
      this.depth = depth || "medium";
      this.root = document.createElement("div");
      this.root.className = "research-card";

      this.phaseState = {};
      PHASE_DEFS.forEach(p => { this.phaseState[p.name] = "pending"; });

      this.subQuestions = [];      // string[]
      this.sources = new Map();    // id -> source obj
      this.reportMarkdown = "";
      this.confidence = null;
      this.activeTab = "report";
      this.startedAt = Date.now();
      this.endedAt = null;

      this._buildSkeleton();
      this._bindEvents();
    }

    getElement() { return this.root; }

    // ── DOM construction ────────────────────────────────────────────

    _buildSkeleton() {
      const depthLabel = {
        basic: "Basic",
        medium: "Medium",
        deep: "Deep",
        expert: "Expert",
        ultra: "Ultra",
        // legacy aliases
        quick: "Basic",
        standard: "Medium",
      }[this.depth] || "Medium";

      this.root.innerHTML = `
        <div class="research-header">
          <div class="research-title-row">
            <div class="research-title-icon"><i class="fas fa-search"></i></div>
            <div class="research-title-text">
              <div class="research-query">${escapeHtml(this.query)}</div>
              <div class="research-subtitle">
                <span class="rmeta"><i class="fas fa-layer-group"></i> ${depthLabel} research</span>
                <span class="rmeta-sep">·</span>
                <span class="rmeta" data-role="elapsed"><i class="fas fa-clock"></i> 0s</span>
                <span class="rmeta-sep" data-role="sep-confidence" style="display:none">·</span>
                <span class="rmeta rconfidence" data-role="confidence" style="display:none"></span>
              </div>
            </div>
          </div>

          <div class="research-timeline" role="list">
            ${PHASE_DEFS.map((p, idx) => `
              <div class="rphase" data-phase="${p.name}" role="listitem">
                <div class="rphase-marker">
                  <i class="fas ${p.icon}"></i>
                  <span class="rphase-num">${idx + 1}</span>
                </div>
                <div class="rphase-meta">
                  <div class="rphase-label">${p.label}</div>
                  <div class="rphase-status">Pending</div>
                </div>
                ${idx < PHASE_DEFS.length - 1 ? '<div class="rphase-connector"></div>' : ''}
              </div>
            `).join("")}
          </div>
        </div>

        <div class="research-subquestions" data-role="subq-container" style="display:none">
          <div class="research-section-title">
            <i class="fas fa-list-ul"></i> Sub-questions
          </div>
          <ol class="rsubq-list" data-role="subq-list"></ol>
        </div>

        <div class="research-tabs" role="tablist">
          <button class="research-tab active" data-tab="report" role="tab" aria-selected="true">
            <i class="fas fa-file-alt"></i> Report
          </button>
          <button class="research-tab" data-tab="sources" role="tab" aria-selected="false">
            <i class="fas fa-book"></i> Sources <span class="rtab-count" data-role="src-count">0</span>
          </button>
        </div>

        <div class="research-tab-panels">
          <div class="research-panel rpanel-report active" data-panel="report">
            <div class="research-report-placeholder" data-role="report-placeholder">
              <div class="rplaceholder-spinner"></div>
              <div class="rplaceholder-text">Waiting for research to complete…</div>
            </div>
            <article class="research-report-body" data-role="report-body" style="display:none"></article>
          </div>

          <div class="research-panel rpanel-sources" data-panel="sources" style="display:none">
            <div class="rsources-list" data-role="sources-list">
              <div class="rsources-empty" data-role="sources-empty">
                <i class="fas fa-search"></i>
                <p>Sources will appear here as they are gathered.</p>
              </div>
            </div>
          </div>
        </div>
      `;

      this.timer = setInterval(() => this._tickElapsed(), 1000);
    }

    _bindEvents() {
      this.root.addEventListener("click", (e) => {
        const tabBtn = e.target.closest(".research-tab");
        if (tabBtn) {
          this._switchTab(tabBtn.dataset.tab);
          return;
        }
        const citation = e.target.closest(".citation-link");
        if (citation) {
          e.preventDefault();
          const id = parseInt(citation.dataset.citationId, 10);
          this._focusCitation(id);
          return;
        }
      });
    }

    _switchTab(tab) {
      this.activeTab = tab;
      this.root.querySelectorAll(".research-tab").forEach(btn => {
        const active = btn.dataset.tab === tab;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-selected", active ? "true" : "false");
      });
      this.root.querySelectorAll(".research-panel").forEach(panel => {
        const active = panel.dataset.panel === tab;
        panel.classList.toggle("active", active);
        panel.style.display = active ? "block" : "none";
      });
    }

    _focusCitation(id) {
      this._switchTab("sources");
      // Wait for the panel to be visible, then scroll & highlight
      requestAnimationFrame(() => {
        const card = this.root.querySelector(`.rsource-card[data-source-id="${id}"]`);
        if (!card) return;
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("flash");
        setTimeout(() => card.classList.remove("flash"), 1400);
      });
    }

    _tickElapsed() {
      const end = this.endedAt || Date.now();
      const secs = Math.max(0, Math.round((end - this.startedAt) / 1000));
      const el = this.root.querySelector('[data-role="elapsed"]');
      if (el) {
        const mm = Math.floor(secs / 60);
        const ss = secs % 60;
        el.innerHTML = mm > 0
          ? `<i class="fas fa-clock"></i> ${mm}m ${ss}s`
          : `<i class="fas fa-clock"></i> ${ss}s`;
      }
    }

    // ── Event handlers ──────────────────────────────────────────────

    /** Feed a single SSE event into the view. */
    handleEvent(evt) {
      if (!evt || !evt.type) return;
      switch (evt.type) {
        case "snapshot":
          this._applySnapshot(evt);
          break;
        case "phase_start":
          this._setPhaseStatus(evt.phase, "in_progress");
          this._setPhaseStatusText(evt.phase, this._phaseRunningText(evt.phase));
          break;
        case "phase_complete":
          this._setPhaseStatus(evt.phase, "completed");
          this._setPhaseStatusText(evt.phase, this._phaseDoneText(evt.phase, evt.detail || {}));
          break;
        case "phase_failed":
          this._setPhaseStatus(evt.phase, "failed");
          this._setPhaseStatusText(evt.phase, `Failed: ${evt.error || "unknown error"}`);
          break;
        case "sub_question":
          this._addSubQuestion(evt.question);
          break;
        case "search_start":
          this._setPhaseStatusText("gathering", `Searching: ${this._truncate(evt.sub_question, 60)}`);
          break;
        case "search_done":
          this._setPhaseStatusText("gathering", `Found ${evt.found} results`);
          break;
        case "scrape_start":
          this._setPhaseStatusText("gathering", `Fetching ${evt.total} pages…`);
          break;
        case "source_added":
          this._addSource(evt);
          break;
        case "analyzing_source":
          this._setPhaseStatusText(
            "analyzing",
            `Reading source ${evt.index + 1}/${evt.total}: ${this._truncate(evt.title || "", 50)}`
          );
          break;
        case "source_refined":
          this._refineSource(evt);
          break;
        case "translation_start":
          this._setPhaseStatusText("gathering", `Translating ${evt.total} non-English sources…`);
          break;
        case "report_ready":
          this.reportMarkdown = evt.markdown || "";
          this.confidence = evt.confidence;
          this._renderReport();
          this._renderConfidence();
          break;
        case "done":
          this._finalize();
          break;
        case "error":
          this._showError(evt.message || "Research failed");
          break;
      }
    }

    _applySnapshot(snap) {
      (snap.sub_questions || []).forEach(q => this._addSubQuestion(q));
      (snap.live_sources || []).forEach(s => this._addSource(s));
      (snap.phases || []).forEach(p => {
        this._setPhaseStatus(p.name, p.status);
        if (p.status === "completed") {
          this._setPhaseStatusText(p.name, this._phaseDoneText(p.name, p.detail || {}));
        }
      });
      if (snap.report_markdown) {
        this.reportMarkdown = snap.report_markdown;
        this._renderReport();
      }
      if (snap.citations && snap.citations.length) {
        snap.citations.forEach(c => {
          const existing = this.sources.get(c.id);
          if (!existing) {
            this._addSource({
              id: c.id, url: c.url, title: c.title, domain: c.domain,
              snippet: c.snippet, sub_question_index: c.sub_question_index,
            });
          }
          this._refineSource({ id: c.id, relevance: c.relevance, findings: c.findings });
        });
      }
      if (snap.confidence) {
        this.confidence = snap.confidence;
        this._renderConfidence();
      }
      if (snap.status === "completed") this._finalize();
    }

    // ── Mutations ───────────────────────────────────────────────────

    _setPhaseStatus(name, status) {
      this.phaseState[name] = status;
      const el = this.root.querySelector(`.rphase[data-phase="${name}"]`);
      if (!el) return;
      el.classList.remove("rphase-pending", "rphase-active", "rphase-done", "rphase-failed");
      if (status === "in_progress") el.classList.add("rphase-active");
      else if (status === "completed") el.classList.add("rphase-done");
      else if (status === "failed") el.classList.add("rphase-failed");
      else el.classList.add("rphase-pending");
    }

    _setPhaseStatusText(name, text) {
      const el = this.root.querySelector(`.rphase[data-phase="${name}"] .rphase-status`);
      if (el) el.textContent = text;
    }

    _phaseRunningText(phase) {
      switch (phase) {
        case "planning":     return "Breaking down the query…";
        case "gathering":    return "Searching the web…";
        case "analyzing":    return "Reading sources…";
        case "synthesizing": return "Writing report…";
        default:             return "Working…";
      }
    }

    _phaseDoneText(phase, detail) {
      const d = detail || {};
      switch (phase) {
        case "planning":
          return `${(d.sub_questions && d.sub_questions.length) || this.subQuestions.length} sub-questions`;
        case "gathering":
          return `${d.sources_collected != null ? d.sources_collected : this.sources.size} sources`;
        case "analyzing":
          return `${d.kept != null ? d.kept : this.sources.size} relevant`;
        case "synthesizing":
          return `${d.length || this.reportMarkdown.length} chars · ${d.confidence != null ? d.confidence + "% confidence" : "done"}`;
        default:
          return "Done";
      }
    }

    _addSubQuestion(q) {
      if (!q) return;
      if (this.subQuestions.includes(q)) return;
      this.subQuestions.push(q);
      const container = this.root.querySelector('[data-role="subq-container"]');
      const list = this.root.querySelector('[data-role="subq-list"]');
      container.style.display = "block";
      const li = document.createElement("li");
      li.textContent = q;
      list.appendChild(li);
    }

    _addSource(src) {
      if (!src || src.id == null) return;
      if (this.sources.has(src.id)) return;
      this.sources.set(src.id, { ...src });
      this._renderSourceCard(src, /*isNew*/ true);
      this._updateSourceCount();
    }

    _refineSource(evt) {
      const src = this.sources.get(evt.id);
      if (!src) return;
      src.relevance = evt.relevance;
      src.findings = evt.findings;
      const card = this.root.querySelector(`.rsource-card[data-source-id="${evt.id}"]`);
      if (!card) return;
      const rel = card.querySelector('[data-role="relevance"]');
      if (rel && evt.relevance != null) {
        const pct = Math.round(evt.relevance * 100);
        rel.style.display = "inline-flex";
        rel.innerHTML = `<i class="fas fa-bolt"></i> ${pct}% relevant`;
      }
      const findingsBox = card.querySelector('[data-role="findings"]');
      if (findingsBox && evt.findings) {
        findingsBox.style.display = "block";
        findingsBox.innerHTML = this._renderFindings(evt.findings);
      }
    }

    _renderFindings(text) {
      const lines = text.split("\n").map(l => l.replace(/^[-•*]\s+/, "").trim()).filter(Boolean);
      if (!lines.length) return "";
      return `<ul>${lines.map(l => `<li>${escapeHtml(l)}</li>`).join("")}</ul>`;
    }

    _renderSourceCard(src, isNew) {
      const list = this.root.querySelector('[data-role="sources-list"]');
      const empty = this.root.querySelector('[data-role="sources-empty"]');
      if (empty) empty.remove();

      const card = document.createElement("div");
      card.className = "rsource-card" + (isNew ? " enter" : "");
      card.dataset.sourceId = String(src.id);
      card.innerHTML = `
        <div class="rsource-head">
          <div class="rsource-num">${src.id}</div>
          <div class="rsource-title-wrap">
            <a class="rsource-title" href="${escapeHtml(src.url || "#")}" target="_blank" rel="noopener noreferrer">
              ${escapeHtml(src.title || src.domain || src.url || "Source")}
            </a>
            <div class="rsource-meta">
              <span class="rsource-domain"><i class="fas fa-link"></i> ${escapeHtml(src.domain || "")}</span>
              ${src.translated ? `<span class="rsource-badge translated"><i class="fas fa-language"></i> ${escapeHtml((src.original_language || "auto").toUpperCase())}</span>` : ""}
              <span class="rsource-badge relevance" data-role="relevance" style="display:none"></span>
            </div>
          </div>
        </div>
        ${src.snippet ? `<div class="rsource-snippet">${escapeHtml(src.snippet)}</div>` : ""}
        <div class="rsource-findings" data-role="findings" style="display:none"></div>
      `;
      list.appendChild(card);
      if (isNew) {
        requestAnimationFrame(() => card.classList.remove("enter"));
      }
    }

    _updateSourceCount() {
      const el = this.root.querySelector('[data-role="src-count"]');
      if (el) el.textContent = String(this.sources.size);
    }

    _renderReport() {
      const placeholder = this.root.querySelector('[data-role="report-placeholder"]');
      const body = this.root.querySelector('[data-role="report-body"]');
      if (!this.reportMarkdown) return;
      const validIds = new Set(Array.from(this.sources.keys()));
      body.innerHTML = renderMarkdown(this.reportMarkdown, validIds);
      body.style.display = "block";
      if (placeholder) placeholder.style.display = "none";
    }

    _renderConfidence() {
      if (this.confidence == null) return;
      const sep = this.root.querySelector('[data-role="sep-confidence"]');
      const el = this.root.querySelector('[data-role="confidence"]');
      if (!el) return;
      const cls = this.confidence >= 80 ? "high" : this.confidence >= 55 ? "medium" : "low";
      el.className = `rmeta rconfidence ${cls}`;
      el.innerHTML = `<i class="fas fa-chart-line"></i> ${this.confidence}% confidence`;
      el.style.display = "inline-flex";
      if (sep) sep.style.display = "inline";
    }

    _finalize() {
      this.endedAt = Date.now();
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
      this._tickElapsed();
      this.root.classList.add("done");
    }

    _showError(message) {
      this.endedAt = Date.now();
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
      const placeholder = this.root.querySelector('[data-role="report-placeholder"]');
      if (placeholder) {
        placeholder.innerHTML = `
          <div class="rplaceholder-error">
            <i class="fas fa-triangle-exclamation"></i>
            <div>${escapeHtml(message)}</div>
          </div>
        `;
      }
      this.root.classList.add("failed");
    }

    _truncate(s, n) {
      const str = String(s || "");
      return str.length > n ? str.slice(0, n - 1) + "…" : str;
    }

    /** Return a serialisable snapshot so the chat history can restore the card. */
    toSnapshot() {
      return {
        query: this.query,
        depth: this.depth,
        phases: PHASE_DEFS.map(p => ({
          name: p.name, label: p.label, status: this.phaseState[p.name],
        })),
        sub_questions: this.subQuestions.slice(),
        citations: Array.from(this.sources.values()),
        report_markdown: this.reportMarkdown,
        confidence: this.confidence,
        startedAt: this.startedAt,
        endedAt: this.endedAt,
      };
    }

    /** Build a fully-populated view from a saved snapshot (for history restore). */
    static fromSnapshot(snap) {
      const v = new ResearchView(snap.query, snap.depth);
      // Preserve original durations so history review shows the real elapsed time
      // instead of resetting the counter to "0s" on restore.
      if (snap.startedAt) v.startedAt = snap.startedAt;
      if (snap.endedAt) v.endedAt = snap.endedAt;
      v._applySnapshot({
        sub_questions: snap.sub_questions || [],
        live_sources: [],
        citations: snap.citations || [],
        phases: snap.phases || [],
        report_markdown: snap.report_markdown || "",
        confidence: snap.confidence,
        status: "completed",
      });
      v._finalize();
      return v;
    }
  }

  global.ResearchView = ResearchView;
  global.__renderResearchMarkdown = renderMarkdown;  // for tests / debugging
})(window);
