/* Sentinel — minimal V1 launcher + live SSE renderer.
 *
 * The 6th capability card on the homepage opens this modal.  The
 * user picks a scan profile + paths (or pastes code), the launcher
 * POSTs to /api/sentinel/start, then streams /events into a
 * results panel.  Findings download as SARIF / Markdown / HTML.
 *
 * Design pillars (per the V1 plan):
 *   • Vanilla JS, no framework dep — matches the existing
 *     ChatController / ConsortiumController shape.
 *   • Severity-color coded findings (red / orange / yellow / grey).
 *   • Live agent activity panel ("agent flight tracker").
 *   • Three download buttons (SARIF / MD / HTML) once the run lands.
 *   • Cancel button mid-flight.
 *
 * License: MIT.
 */

(function () {
    'use strict';

    const SEV_COLOR = {
        critical: '#f85149',
        high: '#ff7b72',
        medium: '#d29922',
        low: '#7ee787',
        info: '#8b949e',
    };

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
                else node.setAttribute(k, v);
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
            { 'X-Client-Id': window.AMOR_CLIENT_ID || 'sentinel-ui' },
            (init && init.headers) || {},
        );
        return fetch(path, Object.assign({ credentials: 'include' }, init || {}, { headers }));
    }

    class SentinelLauncher {
        constructor() {
            this._modal = null;
            this._sessionId = null;
            this._eventSource = null;
            this._panel = null;
        }

        open() {
            if (this._modal) {
                this._modal.style.display = 'flex';
                return;
            }
            this._modal = this._buildModal();
            document.body.appendChild(this._modal);
        }

        close() {
            if (this._modal) this._modal.style.display = 'none';
            if (this._eventSource) {
                try { this._eventSource.close(); } catch (_) {}
                this._eventSource = null;
            }
        }

        _buildModal() {
            const profileSelect = el('select', { id: 'sentinel-profile', class: 'sentinel-input' }, [
                el('option', { value: 'quick' }, 'Quick — static + ML only (~30s)'),
                el('option', { value: 'standard', selected: 'selected' }, 'Standard — + auditor + patcher (~3 min)'),
                el('option', { value: 'deep' }, 'Deep — + reasoner + redteam + judge (~10-15 min)'),
                el('option', { value: 'paranoid' }, 'Paranoid — Deep + synthetic injection self-test'),
            ]);
            const pathsInput = el('textarea', {
                id: 'sentinel-paths', class: 'sentinel-input',
                rows: '3',
                placeholder: 'Paths to scan (one per line) — relative to the server CWD',
            });
            const codeBox = el('textarea', {
                id: 'sentinel-code', class: 'sentinel-input',
                rows: '6',
                placeholder: 'Or paste code directly here (overrides paths)',
            });
            const startBtn = el('button',
                { class: 'sentinel-btn primary', onClick: () => this._submit() },
                'Start scan',
            );
            const cancelBtn = el('button',
                { class: 'sentinel-btn', onClick: () => this.close() }, 'Close');

            const form = el('div', { class: 'sentinel-form' }, [
                el('label', null, 'Scan profile'),
                profileSelect,
                el('label', null, 'Paths'),
                pathsInput,
                el('label', null, 'Code (optional)'),
                codeBox,
                el('div', { class: 'sentinel-actions' }, [startBtn, cancelBtn]),
            ]);

            this._panel = el('div', { class: 'sentinel-results', hidden: 'hidden' });

            const card = el('div', { class: 'sentinel-modal-card' }, [
                el('header', { class: 'sentinel-modal-header' }, [
                    el('h3', null, '🛡️ Sentinel security scan'),
                    el('button', { class: 'sentinel-btn ghost', onClick: () => this.close() }, '×'),
                ]),
                form,
                this._panel,
            ]);

            const overlay = el('div', { class: 'sentinel-modal-overlay' });
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) this.close();
            });
            const wrap = el('div', { class: 'sentinel-modal' }, [overlay, card]);
            wrap.style.display = 'flex';
            return wrap;
        }

        async _submit() {
            const profile = document.getElementById('sentinel-profile').value;
            const pathsText = document.getElementById('sentinel-paths').value || '';
            const code = document.getElementById('sentinel-code').value || '';
            const paths = pathsText.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
            if (!paths.length && !code.trim()) {
                this._renderError('Provide at least one path OR paste code.');
                return;
            }
            this._panel.hidden = false;
            this._panel.innerHTML = '';
            this._panel.appendChild(el('div', { class: 'sentinel-status' },
                'Starting scan…'));
            try {
                const resp = await authFetch('/api/sentinel/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        prompt: 'audit',
                        paths,
                        code_context: code || null,
                        scan_profile: profile,
                    }),
                });
                if (!resp.ok) {
                    const detail = await resp.text();
                    this._renderError('Failed to start: ' + (detail || resp.status));
                    return;
                }
                const data = await resp.json();
                this._sessionId = data.session_id;
                this._renderRunning(data.session_id);
                this._stream(data.session_id);
            } catch (err) {
                this._renderError(String(err));
            }
        }

        _stream(sid) {
            const url = `/api/sentinel/${encodeURIComponent(sid)}/events`;
            const es = new EventSource(url, { withCredentials: true });
            this._eventSource = es;
            es.onmessage = (msg) => {
                let evt = null;
                try { evt = JSON.parse(msg.data); } catch (_) { return; }
                this._handleEvent(evt);
            };
            es.onerror = () => {
                // Connection closed (stream ended or network error).  Try
                // to refresh the status snapshot to render the final
                // bundle.
                try { es.close(); } catch (_) {}
                this._eventSource = null;
                this._refreshStatus(sid);
            };
        }

        _handleEvent(evt) {
            const log = this._panel.querySelector('.sentinel-log');
            if (log) {
                const row = el('div', { class: 'sentinel-log-row' });
                const t = evt.type || 'event';
                const phase = evt.phase ? ` [${evt.phase}]` : '';
                row.textContent = `${t}${phase}`;
                log.appendChild(row);
                log.scrollTop = log.scrollHeight;
            }
            if (evt.type === 'sentinel_completed' || evt.type === 'sentinel_done') {
                this._refreshStatus(this._sessionId);
            }
        }

        async _refreshStatus(sid) {
            try {
                const resp = await authFetch(
                    `/api/sentinel/${encodeURIComponent(sid)}/status`);
                const data = await resp.json();
                this._renderResults(data);
            } catch (err) {
                this._renderError('Status fetch failed: ' + err);
            }
        }

        _renderRunning(sid) {
            this._panel.innerHTML = '';
            this._panel.appendChild(el('div', { class: 'sentinel-status' },
                `Scan running — session ${sid.slice(0, 8)}…`));
            this._panel.appendChild(el('div', { class: 'sentinel-actions' }, [
                el('button',
                    { class: 'sentinel-btn ghost',
                      onClick: () => this._cancel(sid) },
                    'Cancel scan'),
            ]));
            this._panel.appendChild(el('div', { class: 'sentinel-log' }));
        }

        async _cancel(sid) {
            try {
                await authFetch(`/api/sentinel/${encodeURIComponent(sid)}/cancel`,
                                { method: 'POST' });
            } catch (_) {}
            if (this._eventSource) {
                try { this._eventSource.close(); } catch (_) {}
                this._eventSource = null;
            }
            const status = this._panel.querySelector('.sentinel-status');
            if (status) status.textContent = 'Scan cancelled.';
        }

        _renderResults(data) {
            const bundle = (data && data.bundle) || null;
            this._panel.innerHTML = '';

            if (data && data.error) {
                this._renderError(data.error);
                return;
            }

            const header = el('div', { class: 'sentinel-results-header' }, [
                el('h4', null, '🛡️ Sentinel report'),
                el('div', { class: 'sentinel-meta' }, [
                    `Profile: ${data.scan_profile || '?'} · `,
                    `Status: ${data.status || '?'} · `,
                    `Risk: ${(bundle && bundle.repo_risk_score) || 0} / 10`,
                ]),
            ]);
            this._panel.appendChild(header);

            if (!bundle) {
                this._panel.appendChild(el('div', { class: 'sentinel-status' },
                    'Scan complete but no bundle returned.'));
                return;
            }

            // Histogram
            const hist = bundle.severity_histogram || {};
            const histRow = el('div', { class: 'sentinel-histogram' });
            for (const lvl of ['critical', 'high', 'medium', 'low', 'info']) {
                histRow.appendChild(el('span', {
                    class: 'sentinel-hist-bucket',
                    style: { borderColor: SEV_COLOR[lvl] },
                }, `${lvl}: ${hist[lvl] || 0}`));
            }
            this._panel.appendChild(histRow);

            // Findings list
            const findings = bundle.findings || [];
            const list = el('div', { class: 'sentinel-findings' });
            if (!findings.length) {
                list.appendChild(el('div', { class: 'sentinel-empty' },
                    'No findings — repo is clean for this profile.'));
            } else {
                for (const f of findings) {
                    list.appendChild(this._renderFinding(f));
                }
            }
            this._panel.appendChild(list);

            // Download bar
            this._panel.appendChild(this._renderDownloadBar(data.session_id));
        }

        _renderFinding(f) {
            const sev = f.severity || 'low';
            return el('div', { class: 'sentinel-finding',
                               style: { borderLeftColor: SEV_COLOR[sev] || '#888' } }, [
                el('div', { class: 'sentinel-finding-head' }, [
                    el('span', { class: 'sentinel-tag',
                                 style: { background: SEV_COLOR[sev],
                                          color: '#0e1116' } }, sev),
                    el('strong', null, f.tool || ''),
                    el('span', { class: 'sentinel-rule' },
                        ` · ${f.rule_id || f.cwe || ''}`),
                ]),
                el('div', { class: 'sentinel-finding-loc' },
                    `${f.file || 'unknown'} : ${f.line_start || 0}` +
                    (f.cwe ? ` · ${f.cwe}` : '') +
                    (f.cvss_base_score
                        ? ` · CVSS ${f.cvss_base_score.toFixed(1)}`
                        : '')),
                el('div', { class: 'sentinel-finding-msg' },
                    (f.raw_message || '').slice(0, 600)),
            ]);
        }

        _renderDownloadBar(sid) {
            const mk = (label, fmt) => el('a', {
                class: 'sentinel-btn',
                href: `/api/sentinel/${encodeURIComponent(sid)}/artifact?format=${fmt}`,
                target: '_blank',
                rel: 'noopener',
            }, label);
            return el('div', { class: 'sentinel-actions' }, [
                mk('SARIF', 'sarif'),
                mk('Markdown', 'md'),
                mk('HTML', 'html'),
                mk('Zip (all)', 'zip'),
            ]);
        }

        _renderError(msg) {
            this._panel.innerHTML = '';
            this._panel.appendChild(el('div', { class: 'sentinel-error' },
                'Error: ' + msg));
        }
    }

    window.sentinelLauncher = new SentinelLauncher();
    window.openSentinelLauncher = () => window.sentinelLauncher.open();
})();
