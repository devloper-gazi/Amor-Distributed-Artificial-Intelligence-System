/* Sentinel Evolution Console — operator-facing dashboard.
 *
 * The Sentinel card on the homepage exposes a small "Evolution
 * Console" button that opens this modal.  The Console talks to
 * /api/sentinel/evolution/* and lets the operator:
 *
 *   • inspect the production *genome* (active DAG / prompts /
 *     adapters / agents / rules / students)
 *   • browse the immutable Merkle ledger (chronological, filterable)
 *   • review *proposals* — every staging-state mutation across all
 *     subsystems
 *   • promote a single proposal (with audit trail in the ledger)
 *   • roll back DAG / prompt / adapter to a known prior version
 *   • fire a manual *trigger* for a single subsystem (queued in the
 *     ledger; the worker picks it up out-of-band)
 *
 * The Console polls every 10 s — never SSE, never holds an open
 * connection.  Mutations require a confirmation step via window.
 * confirm() before they fire.
 *
 * License: MIT.
 */

(function () {
    'use strict';

    const POLL_MS = 10_000;

    function el(tag, props, children) {
        const node = document.createElement(tag);
        if (props) {
            for (const [k, v] of Object.entries(props)) {
                if (k === 'class') node.className = v;
                else if (k === 'dataset') Object.assign(node.dataset, v);
                else if (k === 'style') Object.assign(node.style, v);
                else if (k.startsWith('on') && typeof v === 'function') {
                    node.addEventListener(k.slice(2).toLowerCase(), v);
                } else if (k === 'innerHTML') node.innerHTML = v;
                else if (v != null) node.setAttribute(k, v);
            }
        }
        if (children) {
            for (const c of [].concat(children)) {
                if (c == null) continue;
                node.appendChild(typeof c === 'string'
                    ? document.createTextNode(c) : c);
            }
        }
        return node;
    }

    function authFetch(path, init) {
        if (window.amorAuth && typeof window.amorAuth.fetch === 'function') {
            return window.amorAuth.fetch(path, init);
        }
        const headers = Object.assign(
            { 'X-Client-Id': window.AMOR_CLIENT_ID || 'sentinel-evo' },
            (init && init.headers) || {},
        );
        return fetch(path, Object.assign(
            { credentials: 'include' }, init || {}, { headers },
        ));
    }

    function fmtTs(value) {
        if (!value) return '—';
        if (typeof value === 'number') {
            try { return new Date(value * 1000).toLocaleString(); }
            catch (_) { return String(value); }
        }
        try { return new Date(value).toLocaleString(); }
        catch (_) { return String(value); }
    }

    function shortHash(h) {
        if (!h) return '∅';
        return String(h).slice(0, 8);
    }

    class EvolutionConsole {
        constructor() {
            this._modal = null;
            this._activeTab = 'genome';
            this._pollHandle = null;
            this._lastData = {};
        }

        open() {
            if (!this._modal) {
                this._modal = this._buildModal();
                document.body.appendChild(this._modal);
            }
            this._modal.style.display = 'flex';
            this._refreshAll();
            this._startPolling();
        }

        close() {
            if (this._modal) this._modal.style.display = 'none';
            this._stopPolling();
        }

        _startPolling() {
            this._stopPolling();
            this._pollHandle = window.setInterval(
                () => { this._refreshAll(); }, POLL_MS,
            );
        }

        _stopPolling() {
            if (this._pollHandle) {
                window.clearInterval(this._pollHandle);
                this._pollHandle = null;
            }
        }

        _buildModal() {
            const overlay = el('div', {
                class: 'sevo-overlay',
                role: 'dialog',
                'aria-label': 'Sentinel Evolution Console',
            });

            const header = el('div', { class: 'sevo-header' }, [
                el('h2', { class: 'sevo-title' }, '🧬 Sentinel Evolution Console'),
                el('div', { class: 'sevo-status', id: 'sevo-status-pill' },
                    'loading…'),
                el('button', {
                    class: 'sevo-close-btn',
                    'aria-label': 'Close',
                    onclick: () => this.close(),
                }, '×'),
            ]);

            const tabs = el('div', { class: 'sevo-tabs' });
            const tabList = ['genome', 'ledger', 'proposals', 'trigger', 'rollback'];
            const tabLabels = {
                genome: 'Genome',
                ledger: 'Ledger',
                proposals: 'Proposals',
                trigger: 'Trigger',
                rollback: 'Rollback',
            };
            for (const t of tabList) {
                const btn = el('button', {
                    class: t === this._activeTab
                        ? 'sevo-tab sevo-tab-active' : 'sevo-tab',
                    dataset: { tab: t },
                    onclick: () => this._switchTab(t),
                }, tabLabels[t]);
                tabs.appendChild(btn);
            }

            const body = el('div', {
                class: 'sevo-body', id: 'sevo-body',
            });

            const footer = el('div', { class: 'sevo-footer' }, [
                el('span', {}, 'AMOR Sentinel Phase 15 — Evolution Engine'),
                el('button', {
                    class: 'sevo-refresh-btn',
                    onclick: () => this._refreshAll(),
                }, '↻ Refresh'),
            ]);

            const card = el('div', { class: 'sevo-card' }, [
                header, tabs, body, footer,
            ]);
            overlay.appendChild(card);

            // Click-outside close.
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) this.close();
            });

            return overlay;
        }

        _switchTab(tab) {
            this._activeTab = tab;
            // Highlight active tab.
            const tabBtns = this._modal.querySelectorAll('.sevo-tab');
            tabBtns.forEach((b) => {
                b.classList.toggle(
                    'sevo-tab-active', b.dataset.tab === tab,
                );
            });
            this._render();
        }

        async _refreshAll() {
            try {
                const [health, genome, ledger, proposals, stats] = await Promise.all([
                    this._safeJson('/api/sentinel/evolution/health'),
                    this._safeJson('/api/sentinel/evolution/genome'),
                    this._safeJson('/api/sentinel/evolution/ledger?limit=200'),
                    this._safeJson('/api/sentinel/evolution/proposals'),
                    this._safeJson('/api/sentinel/evolution/stats'),
                ]);
                this._lastData = { health, genome, ledger, proposals, stats };
                this._renderStatusPill(health);
                this._render();
            } catch (err) {
                console.error('evolution refresh failed', err);
                const pill = document.getElementById('sevo-status-pill');
                if (pill) {
                    pill.textContent = 'offline';
                    pill.className = 'sevo-status sevo-status-bad';
                }
            }
        }

        async _safeJson(path) {
            try {
                const r = await authFetch(path);
                if (!r.ok) {
                    return { _error: r.status, _detail: await r.text() };
                }
                return await r.json();
            } catch (e) {
                return { _error: 'fetch_failed', _detail: String(e) };
            }
        }

        _renderStatusPill(health) {
            const pill = document.getElementById('sevo-status-pill');
            if (!pill) return;
            if (!health || health._error) {
                pill.textContent = 'evolution offline';
                pill.className = 'sevo-status sevo-status-bad';
                return;
            }
            if (!health.enabled) {
                pill.textContent = 'evolution disabled';
                pill.className = 'sevo-status sevo-status-warn';
                return;
            }
            if (!health.ledger_intact) {
                pill.textContent = 'ledger TAMPERED';
                pill.className = 'sevo-status sevo-status-bad';
                return;
            }
            pill.textContent = `chain ok · ${health.entry_count} entries · `
                + `tail ${shortHash(health.tail_hash)}`;
            pill.className = 'sevo-status sevo-status-good';
        }

        _render() {
            const body = this._modal.querySelector('#sevo-body');
            if (!body) return;
            body.innerHTML = '';
            const data = this._lastData || {};
            switch (this._activeTab) {
                case 'genome':    body.appendChild(this._renderGenome(data));    break;
                case 'ledger':    body.appendChild(this._renderLedger(data));    break;
                case 'proposals': body.appendChild(this._renderProposals(data)); break;
                case 'trigger':   body.appendChild(this._renderTrigger());       break;
                case 'rollback':  body.appendChild(this._renderRollback(data));  break;
                default:          body.appendChild(el('div', {}, '—'));
            }
        }

        // ─── GENOME tab ───────────────────────────────────────────────

        _renderGenome(data) {
            const root = el('div', { class: 'sevo-section' });
            const g = (data.genome && data.genome.production) || {};
            const stats = data.stats || { counts: {} };

            const head = el('div', { class: 'sevo-genome-head' }, [
                el('h3', {}, 'Production Genome'),
                el('div', { class: 'sevo-meta' },
                    `Pipeline DAG: ${g.dag_version || '—'} · `
                    + `${g.dag_node_count || 0} nodes · `
                    + `${g.dag_edge_count || 0} edges`),
            ]);
            root.appendChild(head);

            // Counts ribbon.
            const counts = el('div', { class: 'sevo-count-grid' });
            const labels = {
                prompts: 'Prompts', adapters: 'Adapters', rules: 'Rules',
                agents: 'Agents', students: 'Students', dag: 'DAGs',
            };
            for (const [key, label] of Object.entries(labels)) {
                const bucket = (stats.counts && stats.counts[key]) || {};
                const prod = bucket.production || bucket.active || 0;
                const stage = bucket.staging || bucket.shadow || 0;
                const arch = bucket.archived || bucket.dormant || 0;
                counts.appendChild(el('div', {
                    class: 'sevo-count-card',
                }, [
                    el('span', { class: 'sevo-count-label' }, label),
                    el('span', { class: 'sevo-count-prod' }, `prod: ${prod}`),
                    el('span', { class: 'sevo-count-stage' }, `staging: ${stage}`),
                    el('span', { class: 'sevo-count-arch' }, `archived: ${arch}`),
                ]));
            }
            root.appendChild(counts);

            // Active prompts.
            root.appendChild(this._table(
                'Active prompts',
                ['Agent', 'Prompt version'],
                Object.entries(g.prompts || {}).map(
                    ([k, v]) => [k, v || '∅'],
                ),
            ));

            // Active adapters.
            root.appendChild(this._table(
                'Active LoRA adapters',
                ['Agent', 'Adapter version'],
                Object.entries(g.adapters || {}).map(
                    ([k, v]) => [k, v || '∅'],
                ),
            ));

            // Active spawned agents.
            root.appendChild(this._table(
                'Active spawned agents',
                ['Name', 'Primary CWE', 'Languages'],
                (g.agents || []).map((a) => [
                    a.name, a.primary_cwe,
                    Array.isArray(a.languages) ? a.languages.join(', ') : '—',
                ]),
            ));

            // Active rules.
            root.appendChild(this._table(
                'Promoted Semgrep rules',
                ['Rule ID', 'CWE', 'Language', 'Last precision'],
                (g.rules || []).map((r) => [
                    r.rule_id, r.cwe, r.language,
                    r.last_seen_precision != null
                        ? r.last_seen_precision.toFixed(3) : '—',
                ]),
            ));

            return root;
        }

        // ─── LEDGER tab ───────────────────────────────────────────────

        _renderLedger(data) {
            const root = el('div', { class: 'sevo-section' });
            const led = data.ledger || { entries: [] };
            const head = el('div', { class: 'sevo-ledger-head' }, [
                el('h3', {}, 'Immutable Ledger'),
                el('div', { class: 'sevo-meta' },
                    `${led.total || 0} total · `
                    + `intact: ${led.intact === false ? 'NO' : 'yes'} · `
                    + `tail ${shortHash(led.tail_hash)}`),
            ]);
            root.appendChild(head);

            if (!led.entries || led.entries.length === 0) {
                root.appendChild(el('div', { class: 'sevo-empty' },
                    'No ledger entries yet.'));
                return root;
            }

            const tbl = el('table', { class: 'sevo-ledger-table' });
            const thead = el('thead', {}, el('tr', {}, [
                el('th', {}, 'Timestamp'),
                el('th', {}, 'Actor'),
                el('th', {}, 'Kind'),
                el('th', {}, 'Payload'),
                el('th', {}, 'Hash'),
            ]));
            tbl.appendChild(thead);
            const tbody = el('tbody');
            for (const entry of led.entries) {
                const tr = el('tr', {
                    class: this._kindClass(entry.kind),
                }, [
                    el('td', { class: 'sevo-cell-ts' },
                       entry.ts_iso || fmtTs(entry.ts)),
                    el('td', { class: 'sevo-cell-actor' }, entry.actor),
                    el('td', { class: 'sevo-cell-kind' }, entry.kind),
                    el('td', { class: 'sevo-cell-payload' },
                       JSON.stringify(entry.payload || {}).slice(0, 220)),
                    el('td', { class: 'sevo-cell-hash' },
                       shortHash(entry.self_hash)),
                ]);
                tbody.appendChild(tr);
            }
            tbl.appendChild(tbody);
            root.appendChild(tbl);
            return root;
        }

        _kindClass(kind) {
            if (!kind) return 'sevo-row-default';
            if (kind.endsWith('_promoted')) return 'sevo-row-promote';
            if (kind.endsWith('_rolled_back')) return 'sevo-row-rollback';
            if (kind === 'constraint_check_failed' ||
                kind === 'agent_archived')      return 'sevo-row-warn';
            if (kind === 'manual_trigger')      return 'sevo-row-trigger';
            return 'sevo-row-default';
        }

        // ─── PROPOSALS tab ────────────────────────────────────────────

        _renderProposals(data) {
            const root = el('div', { class: 'sevo-section' });
            const prop = data.proposals || { proposals: [] };
            root.appendChild(el('h3', {}, 'Pending proposals'));
            root.appendChild(el('div', { class: 'sevo-meta' },
                `${prop.total || 0} pending`));

            if (!prop.proposals || prop.proposals.length === 0) {
                root.appendChild(el('div', { class: 'sevo-empty' },
                    'Nothing in staging — no proposals to review.'));
                return root;
            }

            const tbl = el('table', { class: 'sevo-proposal-table' });
            tbl.appendChild(el('thead', {}, el('tr', {}, [
                el('th', {}, 'Kind'),
                el('th', {}, 'Target'),
                el('th', {}, 'Created'),
                el('th', {}, 'Metrics'),
                el('th', {}, 'Action'),
            ])));
            const tbody = el('tbody');
            for (const p of prop.proposals) {
                const metricsStr = p.metrics
                    ? Object.entries(p.metrics).map(
                        ([k, v]) => `${k}=${typeof v === 'number'
                            ? v.toFixed(3) : v}`,
                    ).join(', ')
                    : '—';
                tbody.appendChild(el('tr', {}, [
                    el('td', {}, el('span', {
                        class: `sevo-pill sevo-pill-${p.kind}`,
                    }, p.kind)),
                    el('td', { class: 'sevo-cell-id' }, p.id),
                    el('td', {}, fmtTs(p.created_at)),
                    el('td', { class: 'sevo-cell-metrics' }, metricsStr),
                    el('td', {}, el('button', {
                        class: 'sevo-promote-btn',
                        onclick: () => this._handlePromote(p),
                    }, 'Promote')),
                ]));
            }
            tbl.appendChild(tbody);
            root.appendChild(tbl);
            return root;
        }

        async _handlePromote(p) {
            const note = window.prompt(
                `Promote ${p.kind} ${p.id}?\n\nOptional note (e.g. ticket #).`,
                '',
            );
            if (note === null) return;
            const body = {
                kind: p.kind,
                target_id: p.id,
                agent_or_label: p.agent_or_label,
                note: note || null,
            };
            try {
                const r = await authFetch(
                    '/api/sentinel/evolution/promote',
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    },
                );
                const j = await r.json();
                if (!r.ok) {
                    alert(`Promote failed: ${j.detail || r.status}`);
                    return;
                }
                alert(`Promoted: ${j.message}`);
                this._refreshAll();
            } catch (e) {
                alert(`Promote crashed: ${e}`);
            }
        }

        // ─── TRIGGER tab ──────────────────────────────────────────────

        _renderTrigger() {
            const root = el('div', { class: 'sevo-section' });
            root.appendChild(el('h3', {}, 'Manual subsystem trigger'));
            root.appendChild(el('p', { class: 'sevo-meta' },
                'Queues a manual run for the selected subsystem.  '
                + 'The trigger is recorded in the ledger; the worker '
                + 'picks it up out-of-band.  Each subsystem has its '
                + 'own server-side enable flag.'));

            const subs = ['prompt', 'rule', 'spawn', 'dag',
                          'lora', 'distill', 'curriculum'];
            const sel = el('select', { id: 'sevo-trigger-sub' });
            for (const s of subs) {
                sel.appendChild(el('option', { value: s }, s));
            }
            const note = el('input', {
                id: 'sevo-trigger-note',
                type: 'text',
                placeholder: 'Optional note (audit trail)',
                maxlength: '500',
            });
            const payload = el('textarea', {
                id: 'sevo-trigger-payload',
                rows: '4',
                placeholder:
                    'Optional JSON payload, e.g. {"agent": "auditor", "n_mutants": 3}',
            });
            const btn = el('button', {
                class: 'sevo-trigger-btn',
                onclick: () => this._handleTrigger(),
            }, 'Queue trigger');

            const form = el('div', { class: 'sevo-form' }, [
                el('label', {}, 'Subsystem'), sel,
                el('label', {}, 'Payload (JSON)'), payload,
                el('label', {}, 'Note'), note,
                btn,
            ]);
            root.appendChild(form);
            return root;
        }

        async _handleTrigger() {
            const sub = document.getElementById('sevo-trigger-sub').value;
            const noteEl = document.getElementById('sevo-trigger-note');
            const payloadEl = document.getElementById('sevo-trigger-payload');
            let payload = {};
            const raw = (payloadEl.value || '').trim();
            if (raw) {
                try { payload = JSON.parse(raw); }
                catch (e) {
                    alert(`Payload is not valid JSON: ${e.message}`);
                    return;
                }
            }
            if (!window.confirm(
                `Queue manual trigger for "${sub}"?\n\n`
                + `Payload: ${JSON.stringify(payload, null, 2)}`,
            )) return;
            try {
                const r = await authFetch(
                    `/api/sentinel/evolution/trigger/${sub}`,
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            payload, note: noteEl.value || null,
                        }),
                    },
                );
                const j = await r.json();
                if (!r.ok) {
                    alert(`Trigger failed: ${j.detail || r.status}`);
                    return;
                }
                alert(`Trigger queued: ${j.message}`);
                this._refreshAll();
            } catch (e) {
                alert(`Trigger crashed: ${e}`);
            }
        }

        // ─── ROLLBACK tab ─────────────────────────────────────────────

        _renderRollback(data) {
            const root = el('div', { class: 'sevo-section' });
            root.appendChild(el('h3', {}, 'Rollback to a prior version'));
            root.appendChild(el('p', { class: 'sevo-meta' },
                'Pick a kind, agent / pipeline label, and target version.  '
                + 'The current production pointer flips, and a '
                + '*_rolled_back ledger entry is appended.'));

            const kindSel = el('select', { id: 'sevo-rollback-kind' });
            for (const k of ['dag', 'prompt', 'adapter']) {
                kindSel.appendChild(el('option', { value: k }, k));
            }
            const labelInput = el('input', {
                id: 'sevo-rollback-label',
                type: 'text',
                placeholder: "e.g. 'pipeline' (DAG) / 'auditor' (prompt) / 'auditor' (adapter)",
                maxlength: '120',
            });
            const versionInput = el('input', {
                id: 'sevo-rollback-version',
                type: 'text',
                placeholder: 'target version, e.g. v002 / 2026.05.01-genesis',
                maxlength: '200',
            });
            const noteInput = el('input', {
                id: 'sevo-rollback-note',
                type: 'text',
                placeholder: 'Optional note',
                maxlength: '500',
            });
            const btn = el('button', {
                class: 'sevo-rollback-btn',
                onclick: () => this._handleRollback(),
            }, 'Roll back');

            root.appendChild(el('div', { class: 'sevo-form' }, [
                el('label', {}, 'Kind'), kindSel,
                el('label', {}, 'Agent / pipeline'), labelInput,
                el('label', {}, 'Target version'), versionInput,
                el('label', {}, 'Note'), noteInput,
                btn,
            ]));
            return root;
        }

        async _handleRollback() {
            const body = {
                kind: document.getElementById('sevo-rollback-kind').value,
                agent_or_label: document.getElementById('sevo-rollback-label').value.trim(),
                target_version: document.getElementById('sevo-rollback-version').value.trim(),
                note: document.getElementById('sevo-rollback-note').value || null,
            };
            if (!body.agent_or_label || !body.target_version) {
                alert('Both label and target version are required.');
                return;
            }
            if (!window.confirm(
                `Roll back ${body.kind} ${body.agent_or_label} `
                + `to ${body.target_version}?  This flips the production pointer.`,
            )) return;
            try {
                const r = await authFetch(
                    '/api/sentinel/evolution/rollback',
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    },
                );
                const j = await r.json();
                if (!r.ok) {
                    alert(`Rollback failed: ${j.detail || r.status}`);
                    return;
                }
                alert(`Rolled back: ${j.message}`);
                this._refreshAll();
            } catch (e) {
                alert(`Rollback crashed: ${e}`);
            }
        }

        // ─── helpers ──────────────────────────────────────────────────

        _table(title, headers, rows) {
            const wrap = el('div', { class: 'sevo-subsection' });
            wrap.appendChild(el('h4', {}, title));
            if (!rows || rows.length === 0) {
                wrap.appendChild(el('div', { class: 'sevo-empty' }, '— none —'));
                return wrap;
            }
            const tbl = el('table', { class: 'sevo-data-table' });
            tbl.appendChild(el('thead', {}, el('tr', {},
                headers.map((h) => el('th', {}, h)))));
            const tbody = el('tbody');
            for (const row of rows) {
                tbody.appendChild(el('tr', {},
                    row.map((c) => el('td', {}, c == null ? '—' : String(c)))));
            }
            tbl.appendChild(tbody);
            wrap.appendChild(tbl);
            return wrap;
        }
    }

    // Singleton + entry-point binding.
    const console_ = new EvolutionConsole();
    window.AmorEvolutionConsole = console_;

    function bind() {
        const btn = document.getElementById('sentinel-evolution-console-btn');
        if (btn && !btn._sevoBound) {
            btn._sevoBound = true;
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                console_.open();
            });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }
})();
