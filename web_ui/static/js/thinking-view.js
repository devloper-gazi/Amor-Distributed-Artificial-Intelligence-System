/**
 * Amor — Thinking Mode live card.
 *
 * Two UX states live inside one component:
 *   1. Clarifying-questions form (before the pipeline starts)
 *   2. Live reasoning timeline (after the user submits answers)
 *
 * The same root element stays mounted so the transition from "form" to
 * "timeline" is fluid — we just swap the body content in place.
 */
(function () {
    'use strict';

    const EFFORT_LABELS = {
        basic: 'Basic',
        medium: 'Medium',
        deep: 'Deep',
        expert: 'Expert',
        ultra: 'Ultra',
        // legacy aliases for older snapshots
        quick: 'Basic',
        standard: 'Medium',
    };
    const COMPLEXITY_CHIP = {
        trivial: { label: 'Simple', dot: 'var(--mono-400, #7a7a7a)' },
        moderate: { label: 'Moderate', dot: '#6f9bd8' },
        complex: { label: 'Complex', dot: '#d4a84a' },
        expert: { label: 'Expert', dot: '#d16f6f' },
    };
    const PHASE_ICONS = {
        understand: 'fa-brain',
        decompose: 'fa-diagram-project',
        explore: 'fa-compass',
        evaluate: 'fa-scale-balanced',
        synthesize: 'fa-feather-pointed',
        critique: 'fa-magnifying-glass',
    };

    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    // Minimal, safe Markdown → HTML for the deliverable. We deliberately do
    // NOT pull in a full parser here; the deliverable is trusted output from
    // our own pipeline but we still escape raw text before formatting so a
    // prompt-injection attempt can't inject scripts.
    function renderMarkdown(md) {
        if (!md) return '';
        let html = escapeHtml(md);
        // Fenced code blocks
        html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, (_m, lang, body) => {
            return `<pre class="amor-md-code"><code class="lang-${escapeHtml(lang || 'text')}">${body}</code></pre>`;
        });
        // Inline code
        html = html.replace(/`([^`\n]+)`/g, '<code class="amor-md-inline">$1</code>');
        // Headers
        html = html.replace(/^###### (.+)$/gm, '<h6>$1</h6>');
        html = html.replace(/^##### (.+)$/gm, '<h5>$1</h5>');
        html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
        html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
        html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
        html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
        // Bold + italic
        html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
        // Lists
        html = html.replace(/^(?:[ \t]*[-*][ \t]+.+(?:\n|$))+/gm, (block) => {
            const items = block.trim().split(/\n/).map(line =>
                `<li>${line.replace(/^[ \t]*[-*][ \t]+/, '')}</li>`
            ).join('');
            return `<ul>${items}</ul>`;
        });
        html = html.replace(/^(?:[ \t]*\d+\.[ \t]+.+(?:\n|$))+/gm, (block) => {
            const items = block.trim().split(/\n/).map(line =>
                `<li>${line.replace(/^[ \t]*\d+\.[ \t]+/, '')}</li>`
            ).join('');
            return `<ol>${items}</ol>`;
        });
        // Paragraphs
        const blocks = html.split(/\n{2,}/).map(b => {
            const t = b.trim();
            if (!t) return '';
            if (/^<(h\d|ul|ol|pre|blockquote|div)/i.test(t)) return t;
            return `<p>${t.replace(/\n/g, '<br/>')}</p>`;
        });
        return blocks.join('\n');
    }

    class ThinkingView {
        constructor({ prompt, effort = 'medium', provider = 'local' } = {}) {
            this.prompt = prompt || '';
            this.effort = effort;
            this.provider = provider;

            this.state = 'idle'; // idle | questioning | thinking | done | failed
            this.session = null;
            this.analysis = null; // AnalyzeResponse
            this.onSubmitAnswers = null; // set by caller
            this.onProceedWithoutAnswers = null;

            this.root = document.createElement('div');
            this.root.className = 'amor-thinking';
            this._renderShell();
        }

        getElement() {
            return this.root;
        }

        // ─────────────────────────────────────────────────────────── Shell

        _renderShell() {
            this.root.innerHTML = `
                <div class="amor-thinking-head">
                    <div class="amor-thinking-head-l">
                        <span class="amor-thinking-logo" aria-hidden="true"><i class="fas fa-brain"></i></span>
                        <div>
                            <div class="amor-thinking-title">Thinking mode</div>
                            <div class="amor-thinking-sub" data-role="subtitle">Analyzing your request…</div>
                        </div>
                    </div>
                    <div class="amor-thinking-head-r" data-role="chips"></div>
                </div>
                <div class="amor-thinking-body" data-role="body">
                    <div class="amor-thinking-loading">
                        <span class="amor-spinner"></span>
                        <span>Analyzing the request…</span>
                    </div>
                </div>
            `;
            this._body = this.root.querySelector('[data-role="body"]');
            this._subtitle = this.root.querySelector('[data-role="subtitle"]');
            this._chips = this.root.querySelector('[data-role="chips"]');
            this._paintChips();
        }

        _paintChips() {
            const chips = [];
            chips.push(`<span class="amor-chip"><i class="fas fa-gauge-high"></i>${EFFORT_LABELS[this.effort] || 'Medium'}</span>`);
            chips.push(`<span class="amor-chip"><i class="fas fa-microchip"></i>${this.provider === 'claude' ? 'Claude API' : 'Local AI'}</span>`);
            if (this.analysis?.complexity) {
                const c = COMPLEXITY_CHIP[this.analysis.complexity] || COMPLEXITY_CHIP.moderate;
                chips.push(`<span class="amor-chip"><span class="amor-dot" style="background:${c.dot}"></span>${c.label}</span>`);
            }
            if (this.analysis?.detected_deliverable) {
                chips.push(`<span class="amor-chip amor-chip-ghost"><i class="fas fa-file-lines"></i>${escapeHtml(this.analysis.detected_deliverable)}</span>`);
            }
            this._chips.innerHTML = chips.join('');
        }

        // ─────────────────────────────────────────────────────────── State transitions

        /**
         * Show the clarifying-questions form. `analysis` is the response
         * from POST /api/thinking/analyze.
         */
        showQuestions(analysis) {
            this.analysis = analysis;
            this.state = 'questioning';
            this._subtitle.textContent = analysis.rationale || 'A few quick questions first…';
            this._paintChips();

            const questions = Array.isArray(analysis.questions) ? analysis.questions : [];

            this._body.innerHTML = `
                <form class="amor-thinking-questions" data-role="form">
                    <div class="amor-thinking-rationale">
                        <i class="fas fa-circle-info"></i>
                        <span>${escapeHtml(analysis.rationale || 'A few high-leverage clarifications will make the answer sharper.')}</span>
                    </div>
                    <div class="amor-thinking-qlist">
                        ${questions.map((q, i) => this._renderQuestion(q, i)).join('')}
                    </div>
                    <div class="amor-thinking-actions">
                        <button type="button" class="amor-btn amor-btn-ghost" data-action="skip">
                            Skip — just think
                        </button>
                        <button type="submit" class="amor-btn amor-btn-primary">
                            <i class="fas fa-arrow-right-to-bracket"></i>
                            Start thinking
                        </button>
                    </div>
                </form>
            `;

            const form = this._body.querySelector('[data-role="form"]');
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                this._collectAndSubmit(form);
            });
            form.querySelector('[data-action="skip"]').addEventListener('click', () => {
                if (this.onProceedWithoutAnswers) this.onProceedWithoutAnswers();
            });
            // Suggestion chips → fill the input
            form.querySelectorAll('[data-suggestion]').forEach(btn => {
                btn.addEventListener('click', () => {
                    const qid = btn.getAttribute('data-suggestion');
                    const input = form.querySelector(`[data-qid="${CSS.escape(qid)}"]`);
                    if (input) {
                        input.value = btn.getAttribute('data-value') || btn.textContent.trim();
                        input.focus();
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                });
            });
        }

        _renderQuestion(q, i) {
            const suggestions = Array.isArray(q.suggestions) ? q.suggestions : [];
            const isMultiline = q.input_type === 'multiline';
            const inputId = `amor-q-${q.id}`;
            const inputHtml = isMultiline
                ? `<textarea id="${escapeHtml(inputId)}" data-qid="${escapeHtml(q.id)}" rows="2" placeholder="${escapeHtml(q.placeholder || 'Your answer…')}"></textarea>`
                : `<input id="${escapeHtml(inputId)}" data-qid="${escapeHtml(q.id)}" type="text" placeholder="${escapeHtml(q.placeholder || 'Your answer…')}" />`;
            return `
                <div class="amor-thinking-q">
                    <label for="${escapeHtml(inputId)}" class="amor-thinking-qlabel">
                        <span class="amor-thinking-qnum">${i + 1}</span>
                        <span class="amor-thinking-qtext">${escapeHtml(q.question)}</span>
                    </label>
                    ${q.why_it_matters ? `<div class="amor-thinking-qwhy"><i class="fas fa-lightbulb"></i> ${escapeHtml(q.why_it_matters)}</div>` : ''}
                    ${inputHtml}
                    ${suggestions.length ? `
                        <div class="amor-thinking-qsuggestions">
                            ${suggestions.map(s => `<button type="button" class="amor-suggestion" data-suggestion="${escapeHtml(q.id)}" data-value="${escapeHtml(s)}">${escapeHtml(s)}</button>`).join('')}
                        </div>` : ''}
                </div>
            `;
        }

        _collectAndSubmit(form) {
            const answers = {};
            form.querySelectorAll('[data-qid]').forEach(input => {
                const qid = input.getAttribute('data-qid');
                const val = (input.value || '').trim();
                if (val) answers[qid] = val;
            });
            if (this.onSubmitAnswers) this.onSubmitAnswers(answers);
        }

        /**
         * Swap to the live-reasoning timeline. `session` is an initial
         * snapshot (or null — the view will render a "starting…" stub and
         * fill in on the first `snapshot` event).
         */
        showTimeline(session) {
            this.session = session || { phases: [] };
            this.state = 'thinking';
            this._subtitle.textContent = 'Reasoning through your request step by step';
            this._paintChips();

            this._body.innerHTML = `
                <div class="amor-thinking-pipeline" data-role="pipeline"></div>
                <div class="amor-thinking-artifacts" data-role="artifacts"></div>
            `;
            this._paintPipeline();
            this._paintArtifacts();
        }

        /**
         * Update the view from an SSE event.
         */
        handleEvent(evt) {
            if (!evt || !evt.type) return;
            if (evt.type === 'snapshot') {
                this.session = { ...(this.session || {}), ...evt };
                this._paintPipeline();
                this._paintArtifacts();
                return;
            }
            if (evt.type === 'phase_start') {
                this._updatePhase(evt.phase, { status: 'in_progress' });
            } else if (evt.type === 'phase_complete') {
                this._updatePhase(evt.phase, { status: 'completed', detail: evt.detail });
                this._mergeDetail(evt.phase, evt.detail);
            } else if (evt.type === 'phase_failed') {
                this._updatePhase(evt.phase, { status: 'failed', detail: { error: evt.error } });
            } else if (evt.type === 'deliverable_ready') {
                this.session = this.session || {};
                this.session.deliverable_markdown = evt.markdown;
            } else if (evt.type === 'done') {
                this.state = 'done';
                this._subtitle.textContent = 'Done.';
            } else if (evt.type === 'error') {
                this.state = 'failed';
                this._subtitle.textContent = evt.message || 'Something went wrong.';
            } else if (evt.type === 'cancelled') {
                // Phase C2/D3 — backend signalled the pipeline was
                // cancelled by the user. Mark in-progress phases as
                // cancelled (grey) so the timeline reflects reality.
                this.state = 'cancelled';
                this._subtitle.textContent = 'Cancelled.';
                if (Array.isArray(this.session?.phases)) {
                    for (const p of this.session.phases) {
                        if (p.status === 'in_progress' || p.status === 'pending') {
                            p.status = 'cancelled';
                        }
                    }
                }
            }
            this._paintPipeline();
            this._paintArtifacts();
        }

        /**
         * Phase D3 — rehydrate from a query_records snapshot fetched on
         * page reload. Compatible shape with the SSE 'snapshot' event;
         * forwards through the same path so the rendering code stays
         * a single source of truth.
         */
        loadFromSnapshot(snap) {
            if (!snap) return;
            this.handleEvent({ type: 'snapshot', ...snap });
        }

        _updatePhase(name, patch) {
            if (!this.session) this.session = { phases: [] };
            if (!Array.isArray(this.session.phases)) this.session.phases = [];
            let phase = this.session.phases.find(p => p.name === name);
            if (!phase) {
                phase = { name, label: name, status: 'pending', detail: {} };
                this.session.phases.push(phase);
            }
            Object.assign(phase, patch);
        }

        _mergeDetail(name, detail) {
            if (!this.session || !detail) return;
            if (name === 'understand') this.session.understanding = detail;
            else if (name === 'decompose') this.session.sub_questions = detail.sub_questions || [];
            else if (name === 'explore') this.session.alternatives = detail.alternatives || [];
            else if (name === 'evaluate') this.session.decision = detail;
            else if (name === 'synthesize') this.session.deliverable_markdown = detail.markdown || this.session.deliverable_markdown;
            else if (name === 'critique') this.session.critique = detail;
        }

        // ─────────────────────────────────────────────────────────── Painting

        _paintPipeline() {
            const host = this.root.querySelector('[data-role="pipeline"]');
            if (!host) return;
            const phases = (this.session?.phases) || [];
            host.innerHTML = phases.map(p => this._renderPipelineStep(p)).join('');
        }

        _renderPipelineStep(p) {
            const icon = PHASE_ICONS[p.name] || 'fa-circle';
            const status = p.status || 'pending';
            let rightEl = '';
            if (status === 'in_progress') {
                rightEl = '<span class="amor-spinner"></span>';
            } else if (status === 'completed') {
                rightEl = '<i class="fas fa-check"></i>';
            } else if (status === 'failed') {
                rightEl = '<i class="fas fa-triangle-exclamation"></i>';
            } else if (status === 'skipped') {
                rightEl = '<i class="fas fa-forward"></i>';
            } else {
                rightEl = '<span class="amor-pipe-pending"></span>';
            }
            return `
                <div class="amor-pipe-step amor-pipe-${status}">
                    <div class="amor-pipe-icon"><i class="fas ${icon}"></i></div>
                    <div class="amor-pipe-label">
                        <div class="amor-pipe-name">${escapeHtml(p.label || p.name)}</div>
                        <div class="amor-pipe-status">${escapeHtml(status)}</div>
                    </div>
                    <div class="amor-pipe-right">${rightEl}</div>
                </div>
            `;
        }

        _paintArtifacts() {
            const host = this.root.querySelector('[data-role="artifacts"]');
            if (!host || !this.session) return;
            const s = this.session;
            const parts = [];

            if (s.understanding) {
                const u = s.understanding;
                parts.push(this._renderArtifact('Understanding', 'fa-brain', `
                    ${u.restatement ? `<p class="amor-art-lead">${escapeHtml(u.restatement)}</p>` : ''}
                    ${this._renderTagList('Constraints', u.constraints, 'hard')}
                    ${this._renderTagList('Preferences', u.preferences, 'soft')}
                    ${this._renderTagList('Assumptions', u.assumptions, 'ghost')}
                    ${this._renderTagList('Unknowns', u.unknowns, 'warn')}
                `));
            }

            if (Array.isArray(s.sub_questions) && s.sub_questions.length) {
                const items = s.sub_questions.map(q => `
                    <li>
                        <span class="amor-art-q-index">${escapeHtml(String(q.index ?? '•'))}</span>
                        <div>
                            <div class="amor-art-q-text">${escapeHtml(q.question || '')}</div>
                            ${q.why ? `<div class="amor-art-q-why">${escapeHtml(q.why)}</div>` : ''}
                        </div>
                    </li>`).join('');
                parts.push(this._renderArtifact('Sub-questions', 'fa-diagram-project', `<ol class="amor-art-qlist">${items}</ol>`));
            }

            if (Array.isArray(s.alternatives) && s.alternatives.length) {
                const chosenId = s.decision?.chosen_id;
                const alts = s.alternatives.map(a => `
                    <div class="amor-alt ${a.id === chosenId ? 'is-chosen' : ''}">
                        <div class="amor-alt-head">
                            <div class="amor-alt-name">${escapeHtml(a.name)}${a.id === chosenId ? ' <span class="amor-alt-badge">Chosen</span>' : ''}</div>
                            <div class="amor-alt-meta">
                                <span class="amor-alt-risk risk-${escapeHtml(a.risk || 'medium')}">Risk: ${escapeHtml(a.risk || 'medium')}</span>
                                <span class="amor-alt-effort">Effort: ${escapeHtml(a.effort || 'medium')}</span>
                            </div>
                        </div>
                        ${a.summary ? `<p class="amor-alt-summary">${escapeHtml(a.summary)}</p>` : ''}
                        <div class="amor-alt-grid">
                            ${this._renderProCon('Pros', a.pros || [], 'pro')}
                            ${this._renderProCon('Cons', a.cons || [], 'con')}
                        </div>
                        ${a.best_when ? `<div class="amor-alt-bestwhen"><i class="fas fa-star"></i> ${escapeHtml(a.best_when)}</div>` : ''}
                    </div>
                `).join('');
                parts.push(this._renderArtifact('Alternatives', 'fa-compass', alts));
            }

            if (s.decision && s.decision.justification) {
                const d = s.decision;
                parts.push(this._renderArtifact('Decision', 'fa-scale-balanced', `
                    <p class="amor-art-lead">${escapeHtml(d.justification)}</p>
                    ${typeof d.confidence === 'number' ? `<div class="amor-confidence"><span class="amor-confidence-bar"><span style="width:${Math.max(0, Math.min(100, d.confidence))}%"></span></span><span class="amor-confidence-label">Confidence: ${d.confidence}%</span></div>` : ''}
                    ${this._renderTagList('Trade-offs', d.key_trade_offs, 'ghost')}
                    ${this._renderTagList('Would reconsider if', d.would_reconsider_if, 'warn')}
                `));
            }

            if (s.deliverable_markdown) {
                parts.push(this._renderArtifact(
                    'Deliverable',
                    'fa-feather-pointed',
                    `<div class="amor-md">${renderMarkdown(s.deliverable_markdown)}</div>`,
                    { expandable: true }
                ));
            }

            if (s.critique && (s.critique.risks?.length || s.critique.next_steps?.length || s.critique.open_questions?.length)) {
                const c = s.critique;
                const risksHtml = (c.risks || []).map(r => `
                    <li class="amor-risk amor-risk-${escapeHtml(r.severity || 'medium')}">
                        <span class="amor-risk-title">${escapeHtml(r.title || '')}</span>
                        ${r.detail ? `<span class="amor-risk-detail">${escapeHtml(r.detail)}</span>` : ''}
                    </li>
                `).join('');
                parts.push(this._renderArtifact('Self-critique', 'fa-magnifying-glass', `
                    ${risksHtml ? `<div class="amor-sub-head">Risks</div><ul class="amor-risk-list">${risksHtml}</ul>` : ''}
                    ${this._renderTagList('Open questions', c.open_questions, 'ghost')}
                    ${this._renderTagList('Next steps', c.next_steps, 'soft')}
                `));
            }

            if (s.status === 'failed' && s.error) {
                parts.push(`<div class="amor-thinking-error"><i class="fas fa-triangle-exclamation"></i> ${escapeHtml(s.error)}</div>`);
            }

            host.innerHTML = parts.join('');
        }

        _renderArtifact(title, icon, innerHtml, opts = {}) {
            return `
                <section class="amor-artifact ${opts.expandable ? 'is-expandable' : ''}">
                    <header class="amor-artifact-head">
                        <i class="fas ${icon}"></i>
                        <span>${escapeHtml(title)}</span>
                    </header>
                    <div class="amor-artifact-body">${innerHtml}</div>
                </section>
            `;
        }

        _renderTagList(label, items, variant = 'ghost') {
            if (!Array.isArray(items) || !items.length) return '';
            const tags = items.map(t => `<li class="amor-tag amor-tag-${variant}">${escapeHtml(t)}</li>`).join('');
            return `<div class="amor-sub-head">${escapeHtml(label)}</div><ul class="amor-tag-list">${tags}</ul>`;
        }

        _renderProCon(label, items, variant) {
            const list = (items || []).map(t => `<li><i class="fas ${variant === 'pro' ? 'fa-plus' : 'fa-minus'}"></i>${escapeHtml(t)}</li>`).join('');
            return `
                <div class="amor-pc">
                    <div class="amor-pc-head amor-pc-${variant}">${escapeHtml(label)}</div>
                    <ul>${list || '<li class="amor-pc-empty">—</li>'}</ul>
                </div>
            `;
        }

        /** Serialize for chat-history persistence. */
        toSnapshot() {
            return {
                prompt: this.prompt,
                effort: this.effort,
                provider: this.provider,
                analysis: this.analysis,
                session: this.session,
                state: this.state,
            };
        }

        static fromSnapshot(snap) {
            const v = new ThinkingView({
                prompt: snap.prompt,
                effort: snap.effort,
                provider: snap.provider,
            });
            v.analysis = snap.analysis || null;
            v.session = snap.session || null;
            v.state = snap.state || 'done';
            v._paintChips();
            if (v.state === 'questioning' && v.analysis) {
                v.showQuestions(v.analysis);
            } else if (v.session) {
                v.showTimeline(v.session);
                v._paintPipeline();
                v._paintArtifacts();
            }
            return v;
        }
    }

    window.ThinkingView = ThinkingView;
})();
