/* ───────────────────────────────────────────────────────────────────────────
 * CodeView — frontend renderer for the Code Intelligence pipeline.
 *
 * Mirrors ThinkingView in spirit: one root element appended to the
 * messages-area, a 9-stage timeline, and live event handling. Specific
 * to code mode it adds:
 *   • Models-in-use panel (one pill per agent role)
 *   • Model download banner with progress bar (auto-pull flow)
 *   • Execution result cards (✅ / ❌ / ⏱ with stdout/stderr expand)
 *   • Static analysis badges
 *   • Debug iteration banner
 *   • Code blocks with copy-to-clipboard + highlight.js
 *   • Review verdict badge + score ring
 *
 * Persisted via toSnapshot() / fromSnapshot() so chat history reload
 * re-mounts the rich card just like the research/thinking views.
 * ─────────────────────────────────────────────────────────────────────────── */
(function () {
    'use strict';

    const PHASES = [
        { name: 'triage',     label: 'Triage' },
        { name: 'model_prep', label: 'Models' },
        { name: 'plan',       label: 'Plan' },
        { name: 'implement',  label: 'Implement' },
        { name: 'execute',    label: 'Execute' },
        { name: 'analyze',    label: 'Analyze' },
        { name: 'test',       label: 'Test' },
        { name: 'debug',      label: 'Debug' },
        { name: 'review',     label: 'Review' },
    ];

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = (text == null) ? '' : String(text);
        return div.innerHTML;
    }

    function formatBytes(n) {
        if (!Number.isFinite(n) || n <= 0) return '0 B';
        const units = ['B', 'KB', 'MB', 'GB'];
        let i = 0;
        let v = n;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
    }

    function highlight(el, lang) {
        try {
            if (window.hljs && el) {
                if (lang) el.classList.add('language-' + lang);
                window.hljs.highlightElement(el);
            }
        } catch (_) { /* best-effort */ }
    }

    /** Build a single code block with header + copy button + hl.js. */
    function renderCodeBlock(code, lang, title) {
        const wrap = document.createElement('div');
        wrap.className = 'code-block-wrapper';
        wrap.innerHTML = `
            ${title ? `<div class="code-block-title">${escapeHtml(title)}</div>` : ''}
            <button class="code-copy-btn" type="button">Copy</button>
            <pre><code></code></pre>
        `;
        const codeEl = wrap.querySelector('code');
        codeEl.textContent = code || '';
        highlight(codeEl, lang || '');
        const btn = wrap.querySelector('.code-copy-btn');
        btn?.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(code || '');
                btn.textContent = 'Copied!';
                setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
            } catch (_) {
                btn.textContent = 'Copy failed';
                setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
            }
        });
        return wrap;
    }

    /** ───────────────────────────────────────────── CodeView class */
    class CodeView {

        constructor(prompt, effort = 'medium', language = null) {
            this.prompt = prompt || '';
            this.effort = effort || 'medium';
            this.language = (language || '').toLowerCase() || null;
            this.state = 'starting';
            this.session = null;
            this.modelsUsed = {};
            this.executions = [];
            this.staticAnalysis = null;
            this.review = null;
            this.code = '';
            this.tests = '';
            this.debugIteration = 0;
            this.maxDebugIterations = 0;
            this._activeDownload = null;  // { tag, pct }
            this.root = this._renderShell();
        }

        getElement() { return this.root; }

        // ── Shell ─────────────────────────────────────────────────────────

        _renderShell() {
            const el = document.createElement('div');
            el.className = 'amor-code-view';
            el.innerHTML = `
                <div class="amor-code-header">
                    <div class="amor-code-icon">⚙</div>
                    <div class="amor-code-headtext">
                        <div class="amor-code-title">Code Intelligence</div>
                        <div class="amor-code-subtitle" data-role="subtitle">Multi-agent local pipeline</div>
                    </div>
                    <div class="amor-code-meta-pills">
                        <span class="amor-code-effort-pill">${escapeHtml(this.effort)}</span>
                        ${this.language ? `<span class="amor-code-lang-pill">${escapeHtml(this.language)}</span>` : ''}
                    </div>
                </div>

                <div class="amor-code-prompt" data-role="prompt"></div>

                <div class="amor-code-models" data-role="models" hidden></div>
                <div class="amor-code-download-banner" data-role="download" hidden></div>

                <div class="amor-code-pipeline" data-role="pipeline"></div>

                <div class="amor-code-artifacts" data-role="artifacts"></div>
            `;

            const promptEl = el.querySelector('[data-role="prompt"]');
            promptEl.textContent = this.prompt;

            this._paintPipeline(el);
            return el;
        }

        _paintPipeline(root) {
            const host = (root || this.root).querySelector('[data-role="pipeline"]');
            if (!host) return;
            host.innerHTML = PHASES.map((p, i) => `
                <div class="cphase" data-phase="${p.name}" data-status="pending">
                    <div class="cphase-marker">
                        <span class="cphase-dot"></span>
                        <span class="cphase-num">${i + 1}</span>
                    </div>
                    <div class="cphase-meta">
                        <div class="cphase-label">${p.label}</div>
                        <div class="cphase-status">Pending</div>
                    </div>
                    ${i < PHASES.length - 1 ? '<div class="cphase-connector"></div>' : ''}
                </div>
            `).join('');
        }

        _setPhase(name, status, statusText) {
            const el = this.root.querySelector(`.cphase[data-phase="${name}"]`);
            if (!el) return;
            el.dataset.status = status;
            const txt = el.querySelector('.cphase-status');
            if (txt) txt.textContent = statusText || status;
        }

        // ── External API ──────────────────────────────────────────────────

        showTimeline(payload) {
            this.state = 'running';
            const sub = this.root.querySelector('[data-role="subtitle"]');
            if (sub) sub.textContent = 'Pipeline running…';
            if (payload && payload.session_id) {
                this.session = payload;
            }
        }

        handleEvent(evt) {
            if (!evt || !evt.type) return;
            switch (evt.type) {
                case 'snapshot':
                    this._applySnapshot(evt);
                    return;

                case 'phase_start':
                    this._setPhase(evt.phase, 'running', evt.label || 'Running…');
                    return;

                case 'phase_complete':
                    this._setPhase(evt.phase, 'done', this._phaseDoneText(evt));
                    this._mergePhaseDetail(evt.phase, evt.detail || {});
                    return;

                case 'phase_failed':
                    this._setPhase(evt.phase, 'failed', evt.error || 'Failed');
                    return;

                case 'model_download_start':
                    this._renderDownloadBanner(evt);
                    return;
                case 'model_download_progress':
                    this._updateDownloadBanner(evt);
                    return;
                case 'model_download_complete':
                case 'model_download_failed':
                    this._clearDownloadBanner(evt);
                    return;

                case 'code_ready':
                    this.code = evt.code || this.code;
                    if (evt.language) this.language = evt.language;
                    this._renderCodeArtifact();
                    return;

                case 'test_ready':
                    this.tests = evt.code || this.tests;
                    this._renderTestsArtifact();
                    return;

                case 'execution_start':
                    this._renderArtifact('execution-running',
                        '⚡ Sandbox executing…', '');
                    return;
                case 'execution_result':
                    this._addExecutionResult(evt.result || {}, evt.iteration);
                    return;

                case 'static_analysis_result':
                    this.staticAnalysis = evt.result || null;
                    this._renderStaticAnalysis();
                    return;

                case 'debug_iteration_start':
                    this.debugIteration = Number(evt.iteration || 0);
                    this.maxDebugIterations = Number(evt.max || 0);
                    this._renderDebugBanner();
                    return;

                case 'review_ready':
                    this.review = evt.review || null;
                    this._renderReview();
                    return;

                case 'deliverable_ready':
                    this._renderDeliverable(evt.markdown || '');
                    return;

                case 'cancelled':
                    this.state = 'cancelled';
                    this._setSubtitle('Cancelled by user');
                    PHASES.forEach(p => {
                        const el = this.root.querySelector(`.cphase[data-phase="${p.name}"]`);
                        if (el && el.dataset.status === 'pending') {
                            this._setPhase(p.name, 'cancelled', 'Cancelled');
                        }
                    });
                    return;

                case 'done':
                    this.state = 'done';
                    this._setSubtitle('Pipeline complete');
                    this.root.classList.add('done');
                    return;

                case 'error':
                    this.state = 'failed';
                    this._setSubtitle(`Error: ${evt.message || 'unknown'}`);
                    this.root.classList.add('failed');
                    return;

                default:
                    return;
            }
        }

        loadFromSnapshot(snap) {
            this._applySnapshot(snap || {});
            // Settle the pipeline into its terminal visual state.
            const status = (snap && snap.status) || 'completed';
            const finalLabel = ({
                completed: 'Pipeline complete',
                failed: 'Pipeline failed',
                cancelled: 'Cancelled',
            })[status] || 'Restored';
            this._setSubtitle(finalLabel);
            if (status === 'failed') this.root.classList.add('failed');
            if (status === 'completed') this.root.classList.add('done');
        }

        toSnapshot() {
            return {
                prompt: this.prompt,
                effort: this.effort,
                language: this.language,
                state: this.state,
                session: this.session,
                models_used: this.modelsUsed,
                code: this.code,
                tests: this.tests,
                execution_results: this.executions,
                static_analysis: this.staticAnalysis,
                review: this.review,
                debug_iterations: this.debugIteration,
                phases: PHASES.map(p => {
                    const el = this.root.querySelector(`.cphase[data-phase="${p.name}"]`);
                    return {
                        name: p.name,
                        label: p.label,
                        status: el ? (el.dataset.status || 'pending') : 'pending',
                    };
                }),
            };
        }

        static fromSnapshot(snap) {
            const v = new CodeView(
                snap?.prompt || '',
                snap?.effort || 'medium',
                snap?.language || null,
            );
            v.loadFromSnapshot(snap || {});
            return v;
        }

        // ── Renderers ─────────────────────────────────────────────────────

        _setSubtitle(t) {
            const sub = this.root.querySelector('[data-role="subtitle"]');
            if (sub) sub.textContent = t;
        }

        _phaseDoneText(evt) {
            const detail = evt.detail || {};
            switch (evt.phase) {
                case 'triage':
                    return detail.task_type
                        ? `${detail.task_type} · ${detail.complexity || ''}`.trim()
                        : 'Classified';
                case 'model_prep':
                    return Object.keys(detail.models_used || {}).length
                        ? `${Object.keys(detail.models_used).length} agents wired`
                        : 'Models ready';
                case 'plan':
                    return detail.title || 'Plan ready';
                case 'implement':
                    return detail.loc ? `${detail.loc} LOC` : 'Code ready';
                case 'execute': {
                    if (detail.skipped) return 'Skipped';
                    return detail.success ? '✅ Pass' : '❌ Failed';
                }
                case 'analyze': {
                    const c = detail.severity_counts || {};
                    return `${c.error || 0}E · ${c.warning || 0}W · ${c.security || 0}S`;
                }
                case 'test':
                    return detail.skipped ? 'Skipped'
                        : (detail.test_count ? `${detail.test_count} tests` : 'Tests ready');
                case 'debug':
                    if (detail.skipped) return 'No fix needed';
                    return `${detail.iterations || 0}/${detail.max_iterations || 0} iterations`;
                case 'review':
                    return detail.score != null ? `Score ${detail.score}/100` : 'Reviewed';
                default:
                    return 'Done';
            }
        }

        _mergePhaseDetail(phase, detail) {
            if (phase === 'model_prep' && detail.models_used) {
                this.modelsUsed = detail.models_used;
                this._renderModelsPanel();
            }
        }

        _renderModelsPanel() {
            const host = this.root.querySelector('[data-role="models"]');
            if (!host) return;
            const entries = Object.entries(this.modelsUsed || {});
            if (!entries.length) { host.hidden = true; return; }
            host.hidden = false;
            host.innerHTML = `
                <div class="amor-code-models-title">Models in use</div>
                <div class="amor-code-models-row">
                    ${entries.map(([role, tag]) => `
                        <span class="code-model-pill">
                            <span class="model-role">${escapeHtml(role)}</span>
                            <span class="model-tag">${escapeHtml(tag)}</span>
                        </span>
                    `).join('')}
                </div>
            `;
        }

        _renderDownloadBanner(evt) {
            const host = this.root.querySelector('[data-role="download"]');
            if (!host) return;
            const tag = evt.model || (this._activeDownload && this._activeDownload.tag) || 'model';
            const display = evt.display_name || tag;
            const sizeGb = evt.size_gb;
            this._activeDownload = { tag, pct: 0 };
            host.hidden = false;
            host.innerHTML = `
                <div class="model-download-icon">⬇</div>
                <div class="model-download-info">
                    <div class="model-download-row">
                        <span class="model-download-name">${escapeHtml(display)}</span>
                        ${sizeGb ? `<span class="model-download-size">~${sizeGb} GB</span>` : ''}
                        <span class="model-download-pct" data-role="dl-pct">0%</span>
                    </div>
                    <div class="model-download-bar">
                        <div class="model-download-fill" data-role="dl-fill" style="width: 0%"></div>
                    </div>
                    <div class="model-download-note">Downloading once — cached for all future sessions.</div>
                </div>
            `;
        }

        _updateDownloadBanner(evt) {
            const host = this.root.querySelector('[data-role="download"]');
            if (!host) return;
            const fill = host.querySelector('[data-role="dl-fill"]');
            const pctEl = host.querySelector('[data-role="dl-pct"]');
            const pct = Math.max(0, Math.min(100, Number(evt.pct || 0)));
            if (fill) fill.style.width = pct + '%';
            if (pctEl) {
                const done = formatBytes(Number(evt.bytes_done || 0));
                const total = formatBytes(Number(evt.bytes_total || 0));
                pctEl.textContent = total !== '0 B'
                    ? `${pct}% · ${done} / ${total}`
                    : `${pct}%`;
            }
            if (this._activeDownload) this._activeDownload.pct = pct;
        }

        _clearDownloadBanner(evt) {
            const host = this.root.querySelector('[data-role="download"]');
            if (!host) return;
            // Subtle fade then hide.
            host.classList.add('fading');
            setTimeout(() => {
                host.classList.remove('fading');
                host.hidden = true;
                host.innerHTML = '';
            }, 500);
            this._activeDownload = null;
        }

        _renderArtifact(slot, title, htmlOrEl) {
            const host = this.root.querySelector('[data-role="artifacts"]');
            if (!host) return;
            let block = host.querySelector(`[data-slot="${slot}"]`);
            if (!block) {
                block = document.createElement('div');
                block.className = 'amor-code-artifact';
                block.dataset.slot = slot;
                host.appendChild(block);
            }
            block.innerHTML = `<div class="amor-code-artifact-title">${escapeHtml(title)}</div>`;
            const body = document.createElement('div');
            body.className = 'amor-code-artifact-body';
            if (typeof htmlOrEl === 'string') {
                body.innerHTML = htmlOrEl;
            } else if (htmlOrEl instanceof Node) {
                body.appendChild(htmlOrEl);
            }
            block.appendChild(body);
        }

        _renderCodeArtifact() {
            if (!this.code) return;
            const host = this.root.querySelector('[data-role="artifacts"]');
            if (!host) return;
            let block = host.querySelector('[data-slot="implementation"]');
            if (!block) {
                block = document.createElement('div');
                block.className = 'amor-code-artifact';
                block.dataset.slot = 'implementation';
                block.innerHTML = `
                    <div class="amor-code-artifact-title">📦 Implementation</div>
                    <div class="amor-code-artifact-body" data-role="impl-body"></div>
                `;
                host.appendChild(block);
            }
            const body = block.querySelector('[data-role="impl-body"]');
            body.innerHTML = '';
            body.appendChild(renderCodeBlock(this.code, this.language || ''));
        }

        _renderTestsArtifact() {
            if (!this.tests) return;
            const host = this.root.querySelector('[data-role="artifacts"]');
            if (!host) return;
            let block = host.querySelector('[data-slot="tests"]');
            if (!block) {
                block = document.createElement('div');
                block.className = 'amor-code-artifact';
                block.dataset.slot = 'tests';
                block.innerHTML = `
                    <div class="amor-code-artifact-title">🧪 Tests</div>
                    <div class="amor-code-artifact-body" data-role="tests-body"></div>
                `;
                host.appendChild(block);
            }
            const body = block.querySelector('[data-role="tests-body"]');
            body.innerHTML = '';
            body.appendChild(renderCodeBlock(this.tests, this.language || ''));
        }

        _addExecutionResult(result, iteration) {
            this.executions.push(result);
            const host = this.root.querySelector('[data-role="artifacts"]');
            if (!host) return;
            let block = host.querySelector('[data-slot="executions"]');
            if (!block) {
                block = document.createElement('div');
                block.className = 'amor-code-artifact';
                block.dataset.slot = 'executions';
                block.innerHTML = `
                    <div class="amor-code-artifact-title">⚡ Execution</div>
                    <div class="amor-code-artifact-body" data-role="exec-body"></div>
                `;
                host.appendChild(block);
            }
            const body = block.querySelector('[data-role="exec-body"]');
            const card = document.createElement('div');
            const success = result.success === true;
            const timed = result.timed_out === true;
            const cls = timed ? 'execution-card--timeout'
                              : success ? 'execution-card--success'
                              : 'execution-card--failed';
            card.className = `execution-card ${cls}`;
            const icon = success ? '✅' : (timed ? '⏱' : '❌');
            const status = success ? 'Success' : (timed ? 'Timed out' : 'Failed');
            const label = (iteration > 0)
                ? `Debug iteration ${iteration}` : 'Initial run';
            card.innerHTML = `
                <div class="execution-header">
                    <span class="execution-status-icon">${icon}</span>
                    <span class="execution-status-text">${status}</span>
                    <span class="execution-label">${escapeHtml(label)}</span>
                    <span class="execution-meta">exit=${result.exit_code != null ? result.exit_code : '?'} · ${result.duration_ms || 0}ms</span>
                    <button class="execution-toggle" type="button" aria-label="Toggle output">▼</button>
                </div>
                <div class="execution-body">
                    ${result.stdout ? `<pre class="execution-stdout">${escapeHtml(result.stdout)}</pre>` : ''}
                    ${result.stderr ? `<pre class="execution-stderr">${escapeHtml(result.stderr)}</pre>` : ''}
                    ${result.error ? `<pre class="execution-stderr">${escapeHtml(result.error)}</pre>` : ''}
                </div>
            `;
            const header = card.querySelector('.execution-header');
            const bodyEl = card.querySelector('.execution-body');
            const toggle = card.querySelector('.execution-toggle');
            const expand = () => {
                bodyEl.classList.toggle('expanded');
                toggle.textContent = bodyEl.classList.contains('expanded') ? '▲' : '▼';
            };
            header.addEventListener('click', expand);
            // Auto-expand failures so the user sees the error immediately.
            if (!success && (result.stdout || result.stderr || result.error)) {
                bodyEl.classList.add('expanded');
                toggle.textContent = '▲';
            }
            body.appendChild(card);
        }

        _renderStaticAnalysis() {
            if (!this.staticAnalysis) return;
            const sa = this.staticAnalysis;
            const counts = sa.severity_counts || {};
            const html = `
                <div class="static-analysis-row">
                    ${this._badge('error', counts.error || 0)}
                    ${this._badge('warning', counts.warning || 0)}
                    ${this._badge('info', counts.info || 0)}
                    ${this._badge('security', counts.security || 0)}
                    ${sa.complexity_score != null ? `<span class="sa-complexity">CC avg ${(sa.complexity_score).toFixed(1)}</span>` : ''}
                </div>
                ${(sa.issues || []).slice(0, 5).map(i => `
                    <div class="sa-issue sa-issue--${escapeHtml(i.severity)}">
                        <span class="sa-issue-loc">${i.line ? 'L' + i.line : '—'}</span>
                        <span class="sa-issue-code">${escapeHtml(i.code || '?')}</span>
                        <span class="sa-issue-msg">${escapeHtml(i.message || '')}</span>
                    </div>
                `).join('')}
            `;
            this._renderArtifact('static-analysis', '🔬 Static Analysis', html);
        }

        _badge(severity, n) {
            return `<span class="sa-badge sa-badge--${severity}" title="${severity}">${n}</span>`;
        }

        _renderDebugBanner() {
            const host = this.root.querySelector('[data-role="artifacts"]');
            if (!host) return;
            let block = host.querySelector('[data-slot="debug-banner"]');
            if (!block) {
                block = document.createElement('div');
                block.className = 'debug-iteration-banner';
                block.dataset.slot = 'debug-banner';
                host.appendChild(block);
            }
            block.innerHTML = `
                <span class="debug-iteration-dot"></span>
                <span>Debug loop ${this.debugIteration} of ${this.maxDebugIterations || '?'}</span>
            `;
        }

        _renderReview() {
            if (!this.review) return;
            const r = this.review;
            const verdict = r.verdict || 'approved_with_minor';
            const score = Number.isFinite(r.score) ? r.score : 70;
            const verdictLabel = ({
                approved: 'Approved',
                approved_with_minor: 'Approved with minor comments',
                needs_revision: 'Needs revision',
                rejected: 'Rejected',
            })[verdict] || verdict;
            const issues = (r.issues || []).filter(i =>
                i.severity === 'critical' || i.severity === 'major'
            );
            const html = `
                <div class="review-header">
                    <span class="review-score-ring review-score-ring--${this._scoreTier(score)}">${score}</span>
                    <span class="review-verdict-badge review-verdict--${verdict}">${escapeHtml(verdictLabel)}</span>
                </div>
                ${r.final_comment ? `<div class="review-comment">${escapeHtml(r.final_comment)}</div>` : ''}
                ${issues.length ? `
                    <div class="review-issues">
                        <strong>Notable issues:</strong>
                        <ul>
                            ${issues.slice(0, 6).map(i => `
                                <li><em>${escapeHtml(i.severity)}</em> — ${escapeHtml(i.description)}</li>
                            `).join('')}
                        </ul>
                    </div>` : ''}
            `;
            this._renderArtifact('review', '🧐 Code Review', html);
        }

        _scoreTier(score) {
            if (score >= 85) return 'high';
            if (score >= 65) return 'mid';
            return 'low';
        }

        _renderDeliverable(markdown) {
            // Final markdown deliverable is also persisted to chat history;
            // we don't dump it inside the card (the artifact slots already
            // show all the structured pieces).
            this._setSubtitle('Pipeline complete');
        }

        _applySnapshot(snap) {
            if (!snap) return;
            // Phases
            (snap.phases || []).forEach(p => {
                if (!p || !p.name) return;
                const el = this.root.querySelector(`.cphase[data-phase="${p.name}"]`);
                if (el) {
                    el.dataset.status = p.status || 'pending';
                    const txt = el.querySelector('.cphase-status');
                    if (txt) txt.textContent = (p.status || 'Pending')
                        .replace('_', ' ');
                }
            });
            // Models
            this.modelsUsed = snap.models_used || this.modelsUsed || {};
            this._renderModelsPanel();
            // Code + tests
            if (snap.code) { this.code = snap.code; this._renderCodeArtifact(); }
            if (snap.tests) { this.tests = snap.tests; this._renderTestsArtifact(); }
            // Executions
            this.executions = [];
            const host = this.root.querySelector('[data-role="artifacts"]');
            const existing = host && host.querySelector('[data-slot="executions"]');
            if (existing) existing.remove();
            (snap.execution_results || []).forEach((r, i) => {
                this._addExecutionResult(r, i);
            });
            // Static analysis
            if (snap.static_analysis) {
                this.staticAnalysis = snap.static_analysis;
                this._renderStaticAnalysis();
            }
            // Review
            if (snap.review) {
                this.review = snap.review;
                this._renderReview();
            }
            // Debug iteration
            if (snap.debug_iterations) {
                this.debugIteration = snap.debug_iterations;
                this._renderDebugBanner();
            }
            if (snap.language) this.language = snap.language;
        }
    }

    window.CodeView = CodeView;
})();
