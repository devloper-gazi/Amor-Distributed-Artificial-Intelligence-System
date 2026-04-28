/**
 * Consortium Mode controller — drives the launcher modal + live view.
 *
 * Hooks the Consortium capability card click → opens a modal that
 * collects the goal + depth + language + research toggle → POSTs to
 * /api/consortium/start → swaps the welcome card for a live view that
 * subscribes to the SSE event stream and renders phase progress, gate
 * results, an event log, and the final README + download button.
 *
 * 100% local — talks to the AMOR backend via fetch + SSE; no external
 * services. Drops into the existing chat-research.js layout without
 * touching its state machine.
 */

(function () {
    'use strict';

    const PHASE_ORDER = ['scope', 'research', 'thinking', 'implementation'];
    const STATUS_LABELS = {
        pending: 'pending',
        in_progress: 'running',
        completed: 'done',
        failed: 'failed',
    };

    function authHeaders() {
        const headers = {};
        try {
            const token = localStorage.getItem('amor.accessToken');
            if (token) headers['Authorization'] = `Bearer ${token}`;
            let cid = localStorage.getItem('amor.clientId');
            if (!cid) {
                cid = `web-${Math.random().toString(36).slice(2, 10)}`;
                localStorage.setItem('amor.clientId', cid);
            }
            headers['X-Client-Id'] = cid;
        } catch (_) {}
        return headers;
    }

    function escapeHtml(s) {
        return String(s || '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;',
            '"': '&quot;', "'": '&#39;',
        })[c]);
    }

    class ConsortiumController {
        constructor() {
            this._activeSessionId = null;
            this._activeViewEl = null;
            this._eventSource = null;
            this._eventLog = [];
        }

        // ── Modal launcher ────────────────────────────────────────────

        openLauncher() {
            const modal = document.getElementById('consortiumModal');
            if (!modal) return;
            modal.hidden = false;
            this._wireModalOnce();
            const goal = document.getElementById('consortiumGoal');
            if (goal) {
                goal.value = '';
                setTimeout(() => goal.focus(), 30);
            }
        }

        closeLauncher() {
            const modal = document.getElementById('consortiumModal');
            if (modal) modal.hidden = true;
        }

        _wireModalOnce() {
            const modal = document.getElementById('consortiumModal');
            if (!modal || modal.dataset.wired) return;
            modal.dataset.wired = '1';

            const close = () => this.closeLauncher();
            document.getElementById('consortiumModalClose')?.addEventListener('click', close);
            document.getElementById('consortiumModalBackdrop')?.addEventListener('click', close);
            document.getElementById('consortiumCancelBtn')?.addEventListener('click', close);
            document.getElementById('consortiumStartBtn')?.addEventListener('click', () => {
                this._submit().catch(err => {
                    console.warn('Consortium start failed:', err);
                    alert(`Failed to start: ${err.message}`);
                });
            });
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && !modal.hidden) close();
            });
        }

        async _submit() {
            const goal = (document.getElementById('consortiumGoal')?.value || '').trim();
            if (goal.length < 8) {
                alert('Goal must be at least 8 characters.');
                return;
            }
            const depth = document.getElementById('consortiumDepth')?.value || 'medium';
            const language = (document.getElementById('consortiumLanguage')?.value || '').trim() || null;
            const allow = document.getElementById('consortiumAllowResearch')?.checked !== false;

            const body = {
                goal, depth, language,
                deliverable_type: 'code_module',
                allow_external_research: allow,
            };

            const resp = await fetch('/api/consortium/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...authHeaders() },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                const detail = await resp.text().catch(() => '');
                throw new Error(`HTTP ${resp.status} — ${detail.slice(0, 300)}`);
            }
            const data = await resp.json();
            const sid = data.session_id;
            if (!sid) throw new Error('No session_id in response');

            this.closeLauncher();
            this._launchView(sid, { goal, depth });
        }

        // ── Live view ─────────────────────────────────────────────────

        _launchView(sessionId, scopePreview) {
            this._activeSessionId = sessionId;
            this._eventLog = [];
            this._seenEventIds = new Set();

            const tpl = document.getElementById('consortiumViewTemplate');
            const messagesArea = document.getElementById('messagesArea');
            const welcome = document.getElementById('welcomeContainer');
            if (welcome) welcome.style.display = 'none';
            if (!tpl || !messagesArea) return;

            const node = tpl.content.firstElementChild.cloneNode(true);
            node.dataset.sessionId = sessionId;
            const nameEl = node.querySelector('.consortium-view-name');
            if (nameEl) {
                nameEl.textContent = scopePreview.goal.length > 80
                    ? scopePreview.goal.slice(0, 80) + '…'
                    : scopePreview.goal;
            }
            const statusEl = node.querySelector('[data-status]');
            if (statusEl) statusEl.textContent = 'starting';

            const cancelBtn = node.querySelector('.consortium-cancel');
            cancelBtn.hidden = false;
            cancelBtn.addEventListener('click', () => this._cancel(sessionId));

            const dl = node.querySelector('.consortium-download');
            dl.href = `/api/consortium/${encodeURIComponent(sessionId)}/artifact`;

            // Replace any existing live view (only one at a time).
            messagesArea.querySelectorAll('.consortium-view').forEach(n => n.remove());
            messagesArea.prepend(node);
            this._activeViewEl = node;

            this._subscribe(sessionId);
        }

        _subscribe(sessionId) {
            if (this._eventSource) {
                try { this._eventSource.close(); } catch (_) {}
                this._eventSource = null;
            }
            // EventSource doesn't accept custom headers, so we pass auth
            // via query string. The backend's get_optional_user accepts
            // ?access_token=... + a server-side X-Client-Id was set on
            // /start so the session is already keyed.
            const params = new URLSearchParams();
            try {
                const token = localStorage.getItem('amor.accessToken');
                if (token) params.set('access_token', token);
            } catch (_) {}
            const url = `/api/consortium/${encodeURIComponent(sessionId)}/events`
                + (params.toString() ? `?${params.toString()}` : '');
            const es = new EventSource(url);
            this._eventSource = es;

            es.onmessage = (e) => {
                try {
                    const event = JSON.parse(e.data);
                    this._renderEvent(event);
                    if (['consortium_completed', 'consortium_error',
                         'consortium_cancelled'].includes(event.type)) {
                        es.close();
                        this._eventSource = null;
                    }
                } catch (err) {
                    console.warn('Consortium event parse error:', err);
                }
            };
            es.onerror = (err) => {
                console.warn('Consortium SSE error:', err);
            };
        }

        async _cancel(sessionId) {
            // v7 — immediate UI feedback. Disable the button + flip the
            // status pill to "cancelling…" the instant the user clicks,
            // so they don't think the click was lost while the bg task
            // finishes the current LLM call (can be 30+ seconds).
            const view = this._activeViewEl;
            const cancelBtn = view?.querySelector('.consortium-cancel');
            const statusEl = view?.querySelector('[data-status]');
            if (cancelBtn) {
                cancelBtn.disabled = true;
                const span = cancelBtn.querySelector('span') || cancelBtn;
                span.textContent = 'Cancelling…';
            }
            if (statusEl) {
                statusEl.textContent = 'cancelling';
                statusEl.dataset.value = 'cancelling';
            }
            this._appendLog?.('○ cancel requested');

            // Fire the request. We expect the SSE stream to deliver a
            // `consortium_cancelled` event within a few seconds; if it
            // doesn't (e.g., the bg task is genuinely stuck), the
            // force-quit timer below flips the UI to "cancelled" and
            // unblocks the user.
            try {
                const resp = await fetch(
                    `/api/consortium/${encodeURIComponent(sessionId)}/cancel`,
                    { method: 'POST', headers: authHeaders() },
                );
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            } catch (err) {
                console.warn('Cancel POST failed:', err);
                this._appendLog?.(`✗ cancel POST failed: ${err.message}`);
            }

            // Force-quit timer — 8 seconds is enough for a clean
            // cancel; past that we declare the session done from the
            // UI's perspective regardless of what the bg task does.
            if (this._forceQuitTimer) clearTimeout(this._forceQuitTimer);
            this._forceQuitTimer = setTimeout(() => {
                if (this._eventSource) {
                    try { this._eventSource.close(); } catch (_) {}
                    this._eventSource = null;
                }
                if (statusEl && statusEl.dataset.value === 'cancelling') {
                    statusEl.textContent = 'cancelled';
                    statusEl.dataset.value = 'cancelled';
                }
                if (cancelBtn) cancelBtn.hidden = true;
                this._appendLog?.('○ force-quit (no terminal event arrived)');
            }, 8000);
        }

        // ── Event rendering ───────────────────────────────────────────

        _renderEvent(event) {
            if (!this._activeViewEl) return;
            const t = String(event.type || '');
            // v6 client-side dedup — a flaky reconnect could deliver
            // the same event_id twice (snapshot + cache replay). Keep
            // a per-session set; cap so it doesn't grow unbounded.
            if (event.event_id) {
                if (!this._seenEventIds) this._seenEventIds = new Set();
                if (this._seenEventIds.has(event.event_id)) return;
                this._seenEventIds.add(event.event_id);
                if (this._seenEventIds.size > 1000) {
                    // Evict the oldest by recreating the set with the
                    // most recent ~500 entries.
                    this._seenEventIds = new Set(
                        Array.from(this._seenEventIds).slice(-500),
                    );
                }
            }
            this._eventLog.push(event);

            if (t === 'consortium_snapshot') {
                this._renderSnapshot(event);
                return;
            }
            if (t === 'consortium_phase_start') {
                this._setPhaseState(event.phase, 'in_progress');
                this._appendLog(`▶ ${event.phase}`);
                return;
            }
            if (t === 'consortium_phase_complete') {
                this._setPhaseState(event.phase, 'completed');
                this._appendLog(`✓ ${event.phase}`);
                return;
            }
            if (t === 'consortium_gate') {
                const gate = event.gate || {};
                this._renderGate(gate);
                // v6 — reflect gate verdict back onto the phase chip so
                // a failed gate doesn't leave a green ✓ next to red
                // findings. Phase chip stays "completed" but gains a
                // `data-gate-status` for CSS to render an amber/red dot.
                if (gate.phase) {
                    const node = this._activeViewEl.querySelector(
                        `.consortium-phase[data-phase="${gate.phase}"]`,
                    );
                    if (node) {
                        node.dataset.gateStatus = gate.status || '';
                    }
                }
                return;
            }
            if (t === 'consortium_completed') {
                const status = event.status || 'ok';
                const statusEl = this._activeViewEl.querySelector('[data-status]');
                if (statusEl) {
                    statusEl.textContent = status;
                    statusEl.dataset.value = status;
                }
                const cancelBtn = this._activeViewEl.querySelector('.consortium-cancel');
                if (cancelBtn) cancelBtn.hidden = true;
                const dl = this._activeViewEl.querySelector('.consortium-download');
                if (dl) dl.hidden = (status !== 'ok');
                this._renderReadme();
                this._appendLog(`● done (${status})`);
                if (this._forceQuitTimer) {
                    clearTimeout(this._forceQuitTimer);
                    this._forceQuitTimer = null;
                }
                return;
            }
            if (t === 'consortium_cancelled') {
                this._appendLog('○ cancelled');
                if (this._forceQuitTimer) {
                    clearTimeout(this._forceQuitTimer);
                    this._forceQuitTimer = null;
                }
                return;
            }
            if (t === 'consortium_error') {
                this._appendLog(`✗ error: ${event.error || 'unknown'}`);
                return;
            }
            if (t.startsWith('consortium:')) {
                // Inner phase event: keep the log lean.
                const parts = t.split(':');
                const phase = parts[1];
                const inner = parts.slice(2).join(':');
                if (inner === 'phase_start' || inner === 'phase_complete') {
                    const label = event.phase || event.label || '';
                    this._appendLog(`   ${phase}: ${inner.replace('phase_', '')} ${label}`);
                }
                return;
            }
        }

        _renderSnapshot(snapshot) {
            const view = this._activeViewEl;
            if (!view) return;
            const statusEl = view.querySelector('[data-status]');
            if (statusEl) statusEl.textContent = snapshot.status || 'started';
            for (const ph of (snapshot.phases || [])) {
                this._setPhaseState(ph.name, ph.status);
            }
            for (const v of (snapshot.verifications || [])) {
                this._renderGate(v);
            }
        }

        _setPhaseState(phase, state) {
            const view = this._activeViewEl;
            if (!view || !phase) return;
            const node = view.querySelector(`.consortium-phase[data-phase="${phase}"]`);
            if (!node) return;
            node.dataset.state = state || 'pending';
            const stateEl = node.querySelector('[data-state]');
            if (stateEl) {
                stateEl.textContent = STATUS_LABELS[state] || state || 'pending';
            }
        }

        _renderGate(gate) {
            const view = this._activeViewEl;
            const wrap = view?.querySelector('[data-gates]');
            if (!view || !wrap) return;
            // Replace existing entry for the same phase if present.
            const existing = wrap.querySelector(
                `.consortium-gate[data-phase="${gate.phase}"]`,
            );
            const status = gate.status || 'passed_warn';
            const score = gate.score ?? 0;
            const html = `
                <span class="consortium-gate-icon">
                    <i class="fas fa-${
                        status === 'passed' ? 'check-circle'
                        : status === 'failed' ? 'times-circle'
                        : 'exclamation-circle'
                    }"></i>
                </span>
                <div class="consortium-gate-body">
                    <span class="consortium-gate-title">
                        ${escapeHtml(gate.phase)} · score ${escapeHtml(score)}
                    </span>
                    ${gate.summary
                        ? `<span class="consortium-gate-summary">${escapeHtml(gate.summary)}</span>`
                        : ''}
                    ${(gate.findings || []).length
                        ? `<ul class="consortium-gate-findings">${
                            (gate.findings || []).map(f =>
                                `<li>${escapeHtml(f)}</li>`,
                            ).join('')
                        }</ul>`
                        : ''}
                </div>
            `;
            if (existing) {
                existing.dataset.status = status;
                existing.innerHTML = html;
            } else {
                const node = document.createElement('div');
                node.className = 'consortium-gate';
                node.dataset.phase = gate.phase || '';
                node.dataset.status = status;
                node.innerHTML = html;
                wrap.appendChild(node);
            }
        }

        _appendLog(line) {
            const view = this._activeViewEl;
            const log = view?.querySelector('[data-log]');
            if (!log) return;
            const entry = document.createElement('div');
            entry.className = 'consortium-log-line';
            entry.textContent = line;
            log.appendChild(entry);
            // Keep only the last 200 lines so a long pipeline doesn't
            // bloat the DOM.
            while (log.childElementCount > 200) {
                log.firstElementChild.remove();
            }
            log.scrollTop = log.scrollHeight;
        }

        async _renderReadme() {
            if (!this._activeSessionId || !this._activeViewEl) return;
            try {
                const resp = await fetch(
                    `/api/consortium/${encodeURIComponent(this._activeSessionId)}/status`,
                    { headers: authHeaders() },
                );
                if (!resp.ok) return;
                const data = await resp.json();
                const md = data?.bundle?.readme_markdown;
                if (!md) return;
                const wrap = this._activeViewEl.querySelector('[data-readme]');
                if (!wrap) return;
                wrap.hidden = false;
                // Use marked.js if loaded, else fall back to <pre>.
                if (window.marked && typeof window.marked.parse === 'function') {
                    wrap.innerHTML = window.marked.parse(md);
                } else {
                    const pre = document.createElement('pre');
                    pre.textContent = md;
                    wrap.innerHTML = '';
                    wrap.appendChild(pre);
                }
            } catch (err) {
                console.warn('Readme fetch failed:', err);
            }
        }
    }

    // Singleton — installed onto window so app.js can find it.
    window.consortiumController = new ConsortiumController();
})();
