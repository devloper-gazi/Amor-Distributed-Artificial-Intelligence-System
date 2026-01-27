// ==================== Configuration ====================
// Same-origin API calls (works behind the nginx gateway too)
const API_BASE_URL = '';
const CLIENT_ID_STORAGE_KEY = 'chat_client_id';
const USER_ID_STORAGE_KEY = 'chat_user_id'; // reserved for future auth

// ==================== State Management ====================
const state = {
    currentMode: 'research', // 'research' | 'thinking' | 'coding'
    currentSessionId: null,
    sidebarOpen: false,
    darkMode: false,
    clientId: null,
    userId: null,
    historyView: 'all', // 'all' | 'folder' | 'archived'
    selectedFolderId: null,
    folders: [],
    sessionsAllBase: [],
    sessionsAllView: [],
    _historyIndex: new Map(),
    _pendingActionSessionId: null,
    _pendingFolderId: null,
    sessions: {
        research: [],
        thinking: [],
        coding: []
    }
};

// Make state globally available for ChatController
window.appState = state;

// ==================== DOM Ready ====================
document.addEventListener('DOMContentLoaded', () => {
    console.log('🤖 Initializing AI Research Assistant...');
    initializeApp();
});

// ==================== Initialization ====================
function initializeApp() {
    // Core UI setup
    setupSidebarToggle();
    setupModeSelector();
    setupWelcomeCards();
    setupModalHandlers();
    setupThemeToggle();
    setupSettingsHandlers();
    setupKeyboardShortcuts();
    setupHistoryUI();

    // Load preferences
    loadThemePreference();
    state.clientId = getOrCreateClientId();
    state.userId = localStorage.getItem(USER_ID_STORAGE_KEY) || null;
    exposeChatAuthToWindow();

    // Load initial history + create a fresh server session for the current mode
    loadSessionsForMode(state.currentMode);
    ensureCurrentServerSession();

    // Optional legacy migration (localStorage -> MongoDB) runs in background.
    migrateLegacyLocalStorageSessions().finally(() => loadSessionsForMode(state.currentMode));

    // Check system health
    checkSystemHealth();

    // Initialize current mode
    updateModeDisplay(state.currentMode);
    updateWelcomeScreen(state.currentMode);

    console.log('✅ UI initialized successfully');
}

function getOrCreateClientId() {
    let id = localStorage.getItem(CLIENT_ID_STORAGE_KEY);
    if (id && id.trim()) return id;

    if (crypto?.randomUUID) {
        id = crypto.randomUUID();
    } else {
        id = 'client_' + Date.now() + '_' + Math.random().toString(36).slice(2);
    }

    localStorage.setItem(CLIENT_ID_STORAGE_KEY, id);
    return id;
}

function exposeChatAuthToWindow() {
    window.getChatHeaders = () => {
        const headers = { 'X-Client-Id': state.clientId };
        if (state.userId) headers['X-User-Id'] = state.userId;
        return headers;
    };
}

async function apiFetch(path, options = {}) {
    const headers = {
        ...(options.headers || {}),
        ...(window.getChatHeaders ? window.getChatHeaders() : {}),
    };
    return fetch(`${API_BASE_URL}${path}`, { ...options, headers });
}

// ==================== Sidebar Toggle ====================
function setupSidebarToggle() {
    const sidebarToggle = document.getElementById('sidebarToggle');
    const sidebar = document.getElementById('sidebarCollapsible');
    const backdrop = document.getElementById('sidebarBackdrop');
    const historyBtn = document.getElementById('historyBtn');

    // Toggle button click
    sidebarToggle?.addEventListener('click', toggleSidebar);
    historyBtn?.addEventListener('click', toggleSidebar);

    // Backdrop click to close
    backdrop?.addEventListener('click', closeSidebar);

    // New chat button
    const newChatBtn = document.getElementById('newChatBtn');
    newChatBtn?.addEventListener('click', () => {
        createNewChat().catch(console.error);
        closeSidebar();
    });

    // Sidebar links
    const analyticsBtn = document.getElementById('sidebarAnalyticsBtn');
    const settingsBtn = document.getElementById('sidebarSettingsBtn');

    analyticsBtn?.addEventListener('click', () => {
        openModal('analyticsModal');
        closeSidebar();
    });

    settingsBtn?.addEventListener('click', () => {
        openModal('settingsModal');
        closeSidebar();
    });

    // Mode radio buttons in sidebar
    const modeRadios = document.querySelectorAll('.mode-option input[type="radio"]');
    modeRadios.forEach(radio => {
        radio.addEventListener('change', (e) => {
            if (e.target.checked) {
                switchMode(e.target.value).catch(console.error);
            }
        });
    });
}

function toggleSidebar() {
    state.sidebarOpen = !state.sidebarOpen;

    const sidebar = document.getElementById('sidebarCollapsible');
    const backdrop = document.getElementById('sidebarBackdrop');

    if (state.sidebarOpen) {
        sidebar?.classList.add('open');
        backdrop?.classList.add('visible');
        // Update chat history display
        renderChatHistory().catch(console.error);
    } else {
        sidebar?.classList.remove('open');
        backdrop?.classList.remove('visible');
    }
}

function closeSidebar() {
    if (!state.sidebarOpen) return;

    state.sidebarOpen = false;
    const sidebar = document.getElementById('sidebarCollapsible');
    const backdrop = document.getElementById('sidebarBackdrop');

    sidebar?.classList.remove('open');
    backdrop?.classList.remove('visible');
}

function setupHistoryUI() {
    const viewAllBtn = document.getElementById('historyViewAll');
    const viewArchivedBtn = document.getElementById('historyViewArchived');
    const historyList = document.getElementById('chatHistoryList');
    const folderList = document.getElementById('chatFoldersList');

    viewAllBtn?.addEventListener('click', () => {
        state.historyView = 'all';
        state.selectedFolderId = null;
        renderChatHistory().catch(console.error);
    });

    viewArchivedBtn?.addEventListener('click', () => {
        state.historyView = 'archived';
        state.selectedFolderId = null;
        renderChatHistory().catch(console.error);
    });

    // Event delegation for history item clicks and action button.
    historyList?.addEventListener('click', (e) => {
        const actionBtn = e.target.closest('.history-item-actions');
        if (actionBtn) {
            e.preventDefault();
            e.stopPropagation();
            const sessionId = actionBtn.dataset.sessionId;
            const rect = actionBtn.getBoundingClientRect();
            openHistoryContextMenu({ sessionId, x: rect.right, y: rect.bottom }).catch(console.error);
            return;
        }

        const item = e.target.closest('.history-item');
        if (!item) return;
        const sessionId = item.dataset.sessionId;
        const mode = item.dataset.sessionMode;
        if (!sessionId || !mode) return;
        loadSessionFromHistory({ sessionId, mode }).catch(console.error);
        closeSidebar();
    });

    historyList?.addEventListener('contextmenu', (e) => {
        const item = e.target.closest('.history-item');
        if (!item) return;
        e.preventDefault();
        const sessionId = item.dataset.sessionId;
        if (!sessionId) return;
        openHistoryContextMenu({ sessionId, x: e.clientX, y: e.clientY }).catch(console.error);
    });

    // Folder list interactions (event delegation)
    folderList?.addEventListener('click', (e) => {
        const menuBtn = e.target.closest('.folder-actions-menu');
        if (menuBtn) {
            e.preventDefault();
            e.stopPropagation();
            const folderId = menuBtn.dataset.folderId;
            const rect = menuBtn.getBoundingClientRect();
            openFolderContextMenu({ folderId, x: rect.right, y: rect.bottom }).catch(console.error);
            return;
        }

        const delBtn = e.target.closest('.folder-actions-delete');
        if (delBtn) {
            e.preventDefault();
            e.stopPropagation();
            const folderId = delBtn.dataset.folderId;
            if (!folderId) return;
            openConfirmFolderDeleteModal(folderId);
            return;
        }

        const mainBtn = e.target.closest('.folder-main');
        if (mainBtn) {
            e.preventDefault();
            const folderId = mainBtn.dataset.folderId;
            if (!folderId) return;
            state.historyView = 'folder';
            state.selectedFolderId = folderId;
            renderChatHistory().catch(console.error);
        }
    });

    // Folder modal interactions
    const folderSelect = document.getElementById('moveToFolderSelect');
    const newFolderRow = document.getElementById('newFolderNameRow');
    folderSelect?.addEventListener('change', () => {
        if (!newFolderRow) return;
        newFolderRow.style.display = folderSelect.value === '__new__' ? 'flex' : 'none';
    });

    document.getElementById('moveToFolderCancelBtn')?.addEventListener('click', () => closeModal('moveToFolderModal'));
    document.getElementById('moveToFolderSaveBtn')?.addEventListener('click', () => handleMoveToFolderSave().catch(console.error));

    document.getElementById('confirmDeleteCancelBtn')?.addEventListener('click', () => closeModal('confirmDeleteModal'));
    document.getElementById('confirmDeleteBtn')?.addEventListener('click', () => handleConfirmDelete().catch(console.error));

    document.getElementById('renameFolderCancelBtn')?.addEventListener('click', () => closeModal('renameFolderModal'));
    document.getElementById('renameFolderSaveBtn')?.addEventListener('click', () => handleRenameFolderSave().catch(console.error));

    document.getElementById('confirmFolderDeleteCancelBtn')?.addEventListener('click', () => closeModal('confirmFolderDeleteModal'));
    document.getElementById('confirmFolderDeleteBtn')?.addEventListener('click', () => handleConfirmFolderDelete().catch(console.error));

    // Close context menu on outside click / escape
    document.addEventListener('click', () => {
        hideHistoryContextMenu();
        hideFolderContextMenu();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            hideHistoryContextMenu();
            hideFolderContextMenu();
        }
    });
}

// ==================== Mode Switching ====================
function setupModeSelector() {
    const modeSelector = document.getElementById('modeSelector');

    modeSelector?.addEventListener('change', (e) => {
        switchMode(e.target.value).catch(console.error);
    });
}

async function switchMode(newMode) {
    console.log(`🔄 Switching mode: ${state.currentMode} → ${newMode}`);

    applyMode(newMode);
    // Start a fresh server-backed conversation for the selected mode.
    await createNewChat();
}

function applyMode(newMode) {
    // Update state
    state.currentMode = newMode;

    // Update ChatController mode
    if (window.chatController) {
        window.chatController.setMode(newMode);
    }

    // Update UI
    updateModeDisplay(newMode);

    // Update radio buttons in sidebar
    const modeRadios = document.querySelectorAll('.mode-option input[type="radio"]');
    modeRadios.forEach(radio => {
        radio.checked = (radio.value === newMode);
    });

    // Update welcome screen
    updateWelcomeScreen(newMode);
}

function updateModeDisplay(mode) {
    const modeNames = {
        research: 'Research',
        thinking: 'Thinking',
        coding: 'Coding'
    };

    const currentModeDisplay = document.getElementById('currentModeDisplay');
    if (currentModeDisplay) {
        currentModeDisplay.textContent = modeNames[mode];
    }
}

function updateWelcomeScreen(mode) {
    // Highlight active capability card
    const cards = document.querySelectorAll('.capability-card');
    cards.forEach(card => {
        if (card.dataset.mode === mode) {
            card.style.borderColor = 'var(--mono-border-dark)';
            card.style.backgroundColor = 'var(--mono-bg-hover)';
        } else {
            card.style.borderColor = 'var(--mono-border-light)';
            card.style.backgroundColor = 'var(--mono-bg-secondary)';
        }
    });
}

function setupWelcomeCards() {
    // Use event delegation so we don't attach duplicate handlers on every mode switch.
    const container = document.querySelector('.capability-cards');
    if (!container) return;

    container.addEventListener('click', (e) => {
        const card = e.target.closest('.capability-card');
        if (!card) return;

        const cardMode = card.dataset.mode;
        if (cardMode && cardMode !== state.currentMode) {
            switchMode(cardMode).catch(console.error);
        }

        // Focus input for faster first interaction
        const chatInput = document.getElementById('chatInput');
        chatInput?.focus();
    });
}

// ==================== Session Management ====================
async function ensureCurrentServerSession() {
    if (state.currentSessionId) return;
    try {
        const session = await createSessionOnServer(state.currentMode);
        state.currentSessionId = session.id;
        if (window.chatController?.setChatSessionId) {
            window.chatController.setChatSessionId(state.currentSessionId);
        }
    } catch (e) {
        console.error('Failed to create initial server session:', e);
    }
}

function loadSessionForMode(mode) {
    console.log(`📂 Loading session for mode: ${mode}`);
    // No-op: sessions are now server-backed. Kept for backward compatibility.
}

async function createNewChat() {
    console.log('➕ Creating new chat');

    // Create new session on server
    const session = await createSessionOnServer(state.currentMode);
    state.currentSessionId = session.id;

    // Clear messages using ChatController
    if (window.chatController) {
        window.chatController.clearMessages();
        if (window.chatController.setChatSessionId) {
            window.chatController.setChatSessionId(state.currentSessionId);
        }
    }

    // Focus input
    const chatInput = document.getElementById('chatInput');
    chatInput?.focus();
}

async function renderChatHistory() {
    const historyList = document.getElementById('chatHistoryList');
    if (!historyList) return;

    await loadHistoryData();
    const sessions = state.sessionsAllView || [];

    if (sessions.length === 0) {
        historyList.innerHTML = `
            <div class="history-empty">
                <i class="fas fa-comments"></i>
                <p>No chat history yet</p>
            </div>
        `;
        renderFoldersList();
        renderHistoryControls();
        return;
    }

    // Group by date
    const grouped = groupSessionsByDate(sessions);

    let html = '';
    Object.entries(grouped).forEach(([date, items]) => {
        html += `<div class="history-group-title">${date}</div>`;
        items.forEach(session => {
            const isActive = session.id === state.currentSessionId;
            const modeLabel = formatModeShort(session.mode);
            const isPinned = !!session.pinned;
            const pinnedIcon = isPinned ? '<i class="fas fa-thumbtack pinned-icon"></i>' : '';
            html += `
                <div class="history-item ${isActive ? 'active' : ''} ${isPinned ? 'pinned' : ''}" data-session-id="${session.id}" data-session-mode="${session.mode}">
                    <div class="history-item-content">
                        <div class="history-item-title">${pinnedIcon}${escapeHtml(session.title || 'Untitled Chat')}</div>
                        <div class="history-item-sub">
                            <span class="history-item-date">${formatTime(session.updatedAt)}</span>
                            <span class="history-item-mode">${modeLabel}</span>
                        </div>
                    </div>
                    <button class="history-item-actions" type="button" aria-label="Chat actions" data-session-id="${session.id}">
                        <i class="fas fa-ellipsis-v"></i>
                    </button>
                </div>
            `;
        });
    });

    historyList.innerHTML = html;
    renderFoldersList();
    renderHistoryControls();
}

async function loadSession(sessionId) {
    console.log(`📖 Loading session: ${sessionId}`);

    // Update current session ID
    state.currentSessionId = sessionId;
    if (window.chatController?.setChatSessionId) {
        window.chatController.setChatSessionId(state.currentSessionId);
    }

    // Load session + messages from server
    const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!response.ok) {
        throw new Error(`Failed to load session: ${response.statusText}`);
    }
    const session = await response.json();

    if (window.chatController && session.messages) {
        window.chatController.loadMessages(session.messages);
    }
}

async function loadSessionFromHistory({ sessionId, mode }) {
    if (mode && mode !== state.currentMode) {
        applyMode(mode);
    }
    await loadSession(sessionId);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text ?? '';
    return div.innerHTML;
}

function formatModeShort(mode) {
    const m = (mode || '').toLowerCase();
    if (m === 'research') return 'R';
    if (m === 'thinking') return 'T';
    if (m === 'coding') return 'C';
    return '?';
}

function parseServerDate(value) {
    if (!value) return NaN;
    if (typeof value === 'number') return value;
    if (typeof value !== 'string') return Date.parse(value);

    const s = value.trim();
    // If the server sends a timezone-aware ISO timestamp, parse as-is.
    // If it is timezone-naive (common with MongoDB/PyMongo), treat it as UTC
    // to avoid browsers interpreting it as local time.
    const hasTzSuffix = /([zZ]|[+-]\d{2}:\d{2})$/.test(s);
    return Date.parse(hasTzSuffix ? s : `${s}Z`);
}

function groupSessionsByDate(sessions) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const tomorrow = new Date(today);
    tomorrow.setDate(tomorrow.getDate() + 1);
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    const weekAgo = new Date(today);
    weekAgo.setDate(weekAgo.getDate() - 7);

    const groups = {
        'Today': [],
        'Tomorrow': [],
        'Yesterday': [],
        'Last 7 Days': [],
        'Older': []
    };

    sessions.forEach(session => {
        const sessionDate = new Date(session.updatedAt);
        const sessionDay = new Date(sessionDate.getFullYear(), sessionDate.getMonth(), sessionDate.getDate());

        if (sessionDay.getTime() === today.getTime()) {
            groups['Today'].push(session);
        } else if (sessionDay.getTime() === tomorrow.getTime()) {
            groups['Tomorrow'].push(session);
        } else if (sessionDay.getTime() === yesterday.getTime()) {
            groups['Yesterday'].push(session);
        } else if (sessionDate >= weekAgo) {
            groups['Last 7 Days'].push(session);
        } else {
            groups['Older'].push(session);
        }
    });

    // Remove empty groups
    Object.keys(groups).forEach(key => {
        if (groups[key].length === 0) {
            delete groups[key];
        }
    });

    return groups;
}

async function loadFolders() {
    try {
        const resp = await apiFetch('/api/folders?limit=200&offset=0');
        if (!resp.ok) throw new Error(resp.statusText);
        const data = await resp.json();
        state.folders = (data.folders || []).map(f => ({
            id: f.id,
            name: f.name,
            createdAt: parseServerDate(f.created_at),
            updatedAt: parseServerDate(f.updated_at),
            pinned: !!f.pinned,
        }));
    } catch (e) {
        console.error('Failed to load folders:', e);
        state.folders = [];
    }
}

async function loadSessionsAll({ includeArchived = false, folderId = null } = {}) {
    const qs = new URLSearchParams();
    qs.set('limit', '200');
    qs.set('offset', '0');
    qs.set('include_archived', includeArchived ? 'true' : 'false');
    if (folderId) qs.set('folder_id', folderId);

    const response = await apiFetch(`/api/sessions/all?${qs.toString()}`);
    if (!response.ok) throw new Error(`Failed to list sessions: ${response.statusText}`);
    const data = await response.json();
    return (data.sessions || []).map(s => ({
        id: s.id,
        mode: s.mode,
        title: s.title,
        createdAt: parseServerDate(s.created_at),
        updatedAt: parseServerDate(s.updated_at),
        archived: !!s.archived,
        folderId: s.folder_id || null,
        pinned: !!s.pinned,
    }));
}

async function loadHistoryData() {
    await loadFolders();

    // Base: unarchived sessions across all modes (for counts + default view)
    const base = await loadSessionsAll({ includeArchived: false, folderId: null });
    state.sessionsAllBase = base;

    let viewSessions = base;
    if (state.historyView === 'folder' && state.selectedFolderId) {
        viewSessions = await loadSessionsAll({ includeArchived: false, folderId: state.selectedFolderId });
    } else if (state.historyView === 'archived') {
        const all = await loadSessionsAll({ includeArchived: true, folderId: null });
        viewSessions = all.filter(s => s.archived);
    }

    state.sessionsAllView = viewSessions;
    state._historyIndex = new Map(viewSessions.map(s => [s.id, s]));
}

function renderHistoryControls() {
    const viewAllBtn = document.getElementById('historyViewAll');
    const viewArchivedBtn = document.getElementById('historyViewArchived');
    if (viewAllBtn) viewAllBtn.classList.toggle('active', state.historyView !== 'archived');
    if (viewArchivedBtn) viewArchivedBtn.classList.toggle('active', state.historyView === 'archived');
}

function renderFoldersList() {
    const list = document.getElementById('chatFoldersList');
    const section = document.getElementById('chatFoldersSection');
    if (!list || !section) return;

    const counts = new Map();
    (state.sessionsAllBase || []).forEach(s => {
        if (!s.folderId) return;
        counts.set(s.folderId, (counts.get(s.folderId) || 0) + 1);
    });

    if (!state.folders || state.folders.length === 0) {
        list.innerHTML = `<div class="folders-empty">No folders yet</div>`;
        section.style.display = 'block';
        return;
    }

    list.innerHTML = state.folders
        .map(f => {
            const isSelected = state.historyView === 'folder' && state.selectedFolderId === f.id;
            const count = counts.get(f.id) || 0;
            return `
                <div class="folder-row ${isSelected ? 'active' : ''}" data-folder-id="${f.id}">
                    <button class="folder-main" type="button" data-folder-id="${f.id}" aria-label="Select folder ${escapeHtml(f.name)}">
                        <span class="folder-item-name">${escapeHtml(f.name)}</span>
                        <span class="folder-item-count">${count}</span>
                    </button>
                    <div class="folder-actions">
                        <button class="folder-actions-menu" type="button" aria-label="Folder actions" data-folder-id="${f.id}">
                            <i class="fas fa-ellipsis-v"></i>
                        </button>
                        <button class="folder-actions-delete" type="button" aria-label="Delete folder" data-folder-id="${f.id}">
                            <span aria-hidden="true">&times;</span>
                        </button>
                    </div>
                </div>
            `;
        })
        .join('');

    section.style.display = 'block';
}

function hideFolderContextMenu() {
    const menu = document.getElementById('folderContextMenu');
    if (!menu || menu.style.display === 'none') return;
    
    // Add closing animation class
    menu.classList.add('closing');
    
    // Wait for animation to complete before hiding
    setTimeout(() => {
        menu.style.display = 'none';
        menu.classList.remove('closing');
        menu.innerHTML = '';
    }, 100);
}

async function openFolderContextMenu({ folderId, x, y }) {
    const menu = document.getElementById('folderContextMenu');
    if (!menu) return;
    if (!folderId) return;

    const folder = (state.folders || []).find(f => f.id === folderId);
    if (!folder) return;

    state._pendingFolderId = folderId;

    const canMoveTop = !folder.pinned;
    const canMoveBottom = !!folder.pinned;

    const items = [
        canMoveTop ? { id: 'move_top', label: 'Move to top' } : null,
        canMoveBottom ? { id: 'move_bottom', label: 'Move to bottom' } : null,
        { id: 'rename', label: 'Rename…' },
    ].filter(Boolean);

    menu.innerHTML = items
        .map(i => `<button class="context-menu-item" type="button" data-action="${i.id}">${i.label}</button>`)
        .join('');

    menu.style.display = 'block';
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - 220))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - 160))}px`;

    menu.addEventListener('click', (e) => e.stopPropagation(), { once: true });
    menu.querySelectorAll('.context-menu-item').forEach(btn => {
        btn.addEventListener('click', () => handleFolderMenuAction(btn.dataset.action).catch(console.error));
    });
}

async function handleFolderMenuAction(action) {
    const folderId = state._pendingFolderId;
    if (!folderId) return;

    if (action === 'move_top') {
        await apiFetch(`/api/folders/${encodeURIComponent(folderId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned: true }),
        });
        showToast('Folder moved to top', 'success');
        hideFolderContextMenu();
        await renderChatHistory();
        return;
    }

    if (action === 'move_bottom') {
        await apiFetch(`/api/folders/${encodeURIComponent(folderId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned: false }),
        });
        showToast('Folder moved to bottom', 'success');
        hideFolderContextMenu();
        await renderChatHistory();
        return;
    }

    if (action === 'rename') {
        hideFolderContextMenu();
        openRenameFolderModal(folderId);
    }
}

function openRenameFolderModal(folderId) {
    const folder = (state.folders || []).find(f => f.id === folderId);
    if (!folder) return;

    state._pendingFolderId = folderId;
    const input = document.getElementById('renameFolderInput');
    if (input) input.value = folder.name || '';
    openModal('renameFolderModal');
}

async function handleRenameFolderSave() {
    const folderId = state._pendingFolderId;
    if (!folderId) return;
    const input = document.getElementById('renameFolderInput');
    const name = (input?.value || '').trim();
    if (!name) {
        showToast('Please enter a folder name', 'info');
        return;
    }

    const resp = await apiFetch(`/api/folders/${encodeURIComponent(folderId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
    });
    if (!resp.ok) {
        showToast('Failed to rename folder', 'info');
        return;
    }

    closeModal('renameFolderModal');
    showToast('Folder renamed', 'success');
    await renderChatHistory();
}

function openConfirmFolderDeleteModal(folderId) {
    const folder = (state.folders || []).find(f => f.id === folderId);
    if (!folder) return;

    state._pendingFolderId = folderId;
    const text = document.getElementById('confirmFolderDeleteText');
    if (text) text.textContent = `Delete folder “${folder.name || 'Untitled'}”? Chats will be removed from this folder.`;
    openModal('confirmFolderDeleteModal');
}

async function handleConfirmFolderDelete() {
    const folderId = state._pendingFolderId;
    if (!folderId) return;

    const resp = await apiFetch(`/api/folders/${encodeURIComponent(folderId)}`, { method: 'DELETE' });
    if (!resp.ok) {
        showToast('Failed to delete folder', 'info');
        return;
    }

    // If user is viewing this folder, bounce back to All.
    if (state.historyView === 'folder' && state.selectedFolderId === folderId) {
        state.historyView = 'all';
        state.selectedFolderId = null;
    }

    closeModal('confirmFolderDeleteModal');
    showToast('Folder deleted', 'success');
    await renderChatHistory();
}
function hideHistoryContextMenu() {
    const menu = document.getElementById('historyContextMenu');
    if (!menu || menu.style.display === 'none') return;
    
    // Add closing animation class
    menu.classList.add('closing');
    
    // Wait for animation to complete before hiding
    setTimeout(() => {
        menu.style.display = 'none';
        menu.classList.remove('closing');
        menu.innerHTML = '';
    }, 100);
}

async function openHistoryContextMenu({ sessionId, x, y }) {
    const menu = document.getElementById('historyContextMenu');
    if (!menu) return;

    const session = state._historyIndex.get(sessionId);
    if (!session) return;

    state._pendingActionSessionId = sessionId;

    const canArchive = !session.archived;
    const canUnarchive = !!session.archived;
    const hasFolder = !!session.folderId;
    const canPin = !session.pinned;
    const canUnpin = !!session.pinned;

    const items = [
        canPin ? { id: 'pin', label: 'Pin' } : null,
        canUnpin ? { id: 'unpin', label: 'Unpin' } : null,
        canArchive ? { id: 'archive', label: 'Archive' } : null,
        canUnarchive ? { id: 'unarchive', label: 'Unarchive' } : null,
        { id: 'move', label: 'Move to folder…' },
        hasFolder ? { id: 'remove_folder', label: 'Remove from folder' } : null,
        { id: 'delete', label: 'Delete' },
    ].filter(Boolean);

    menu.innerHTML = items
        .map(i => `<button class="context-menu-item" type="button" data-action="${i.id}">${i.label}</button>`)
        .join('');

    menu.style.display = 'block';
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - 220))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - 200))}px`;

    // Stop clicks inside menu from closing it immediately.
    menu.addEventListener('click', (e) => e.stopPropagation(), { once: true });

    menu.querySelectorAll('.context-menu-item').forEach(btn => {
        btn.addEventListener('click', () => handleHistoryMenuAction(btn.dataset.action).catch(console.error));
    });
}

async function handleHistoryMenuAction(action) {
    const sessionId = state._pendingActionSessionId;
    if (!sessionId) return;

    if (action === 'pin') {
        await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned: true }),
        });
        showToast('Chat pinned', 'success');
        hideHistoryContextMenu();
        await renderChatHistory();
        return;
    }

    if (action === 'unpin') {
        await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned: false }),
        });
        showToast('Chat unpinned', 'success');
        hideHistoryContextMenu();
        await renderChatHistory();
        return;
    }

    if (action === 'archive') {
        await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ archived: true }),
        });
        showToast('Chat archived', 'success');
        hideHistoryContextMenu();
        await renderChatHistory();
        return;
    }

    if (action === 'unarchive') {
        await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ archived: false }),
        });
        showToast('Chat restored', 'success');
        hideHistoryContextMenu();
        await renderChatHistory();
        return;
    }

    if (action === 'remove_folder') {
        await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folder_id: null }),
        });
        showToast('Removed from folder', 'success');
        hideHistoryContextMenu();
        await renderChatHistory();
        return;
    }

    if (action === 'move') {
        hideHistoryContextMenu();
        openMoveToFolderModal(sessionId).catch(console.error);
        return;
    }

    if (action === 'delete') {
        hideHistoryContextMenu();
        openConfirmDeleteModal(sessionId);
    }
}

async function openMoveToFolderModal(sessionId) {
    const session = state._historyIndex.get(sessionId);
    if (!session) return;

    state._pendingActionSessionId = sessionId;
    const titleEl = document.getElementById('moveToFolderChatTitle');
    if (titleEl) titleEl.textContent = session.title || 'Untitled Chat';

    await loadFolders();

    const select = document.getElementById('moveToFolderSelect');
    const newFolderRow = document.getElementById('newFolderNameRow');
    const newFolderInput = document.getElementById('newFolderNameInput');
    if (newFolderRow) newFolderRow.style.display = 'none';
    if (newFolderInput) newFolderInput.value = '';

    if (select) {
        const current = session.folderId || '';
        select.innerHTML = [
            `<option value="">No folder</option>`,
            ...(state.folders || []).map(f => `<option value="${f.id}">${escapeHtml(f.name)}</option>`),
            `<option value="__new__">+ New folder…</option>`,
        ].join('');
        select.value = current;
    }

    openModal('moveToFolderModal');
}

async function handleMoveToFolderSave() {
    const sessionId = state._pendingActionSessionId;
    if (!sessionId) return;

    const select = document.getElementById('moveToFolderSelect');
    const newFolderInput = document.getElementById('newFolderNameInput');
    if (!select) return;

    let folderId = select.value;
    if (folderId === '__new__') {
        const name = (newFolderInput?.value || '').trim();
        if (!name) {
            showToast('Please enter a folder name', 'info');
            return;
        }

        const resp = await apiFetch('/api/folders', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        if (!resp.ok) {
            showToast('Failed to create folder', 'info');
            return;
        }
        const created = await resp.json();
        folderId = created.id;
    }

    await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder_id: folderId || null }),
    });

    closeModal('moveToFolderModal');
    showToast('Chat moved', 'success');
    await renderChatHistory();
}

function openConfirmDeleteModal(sessionId) {
    const session = state._historyIndex.get(sessionId);
    if (!session) return;
    state._pendingActionSessionId = sessionId;
    const text = document.getElementById('confirmDeleteText');
    if (text) text.textContent = `Delete “${session.title || 'Untitled Chat'}”? This cannot be undone.`;
    openModal('confirmDeleteModal');
}

async function handleConfirmDelete() {
    const sessionId = state._pendingActionSessionId;
    if (!sessionId) return;

    const resp = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
    if (!resp.ok) {
        showToast('Failed to delete chat', 'info');
        return;
    }

    closeModal('confirmDeleteModal');
    showToast('Chat deleted', 'success');

    if (state.currentSessionId === sessionId) {
        state.currentSessionId = null;
        await ensureCurrentServerSession();
        if (window.chatController?.setChatSessionId) {
            window.chatController.setChatSessionId(state.currentSessionId);
        }
        window.chatController?.clearMessages();
    }

    await renderChatHistory();
}

async function loadSessionsForMode(mode) {
    try {
        const response = await apiFetch(`/api/sessions?mode=${encodeURIComponent(mode)}`);
        if (!response.ok) {
            throw new Error(`Failed to list sessions: ${response.statusText}`);
        }
        const data = await response.json();
        const sessions = (data.sessions || []).map(s => ({
            id: s.id,
            mode: s.mode,
            title: s.title,
            createdAt: parseServerDate(s.created_at),
            updatedAt: parseServerDate(s.updated_at),
            archived: !!s.archived,
            folderId: s.folder_id || null,
            pinned: !!s.pinned,
        }));
        state.sessions[mode] = sessions;
    } catch (e) {
        console.error('Failed to load sessions:', e);
        state.sessions[mode] = state.sessions[mode] || [];
    }
}

async function createSessionOnServer(mode) {
    const response = await apiFetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode })
    });
    if (!response.ok) {
        throw new Error(`Failed to create session: ${response.statusText}`);
    }
    return await response.json();
}

function formatTime(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// ==================== Modal Handlers ====================
function setupModalHandlers() {
    // Settings entrypoint is in the sidebar; keep modal handler here.

    // Modal close buttons
    const closeButtons = document.querySelectorAll('.modal-close');
    closeButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
            const modalId = e.target.closest('.modal').id;
            closeModal(modalId);
        });
    });

    // Close on backdrop click
    const modals = document.querySelectorAll('.modal');
    modals.forEach(modal => {
        const backdrop = modal.querySelector('.modal-backdrop');
        backdrop?.addEventListener('click', () => {
            closeModal(modal.id);
        });
    });

    // ESC key to close modals
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeAllModals();
        }
    });
}

function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;

    modal.style.display = 'flex';

    // Load modal-specific data
    if (modalId === 'analyticsModal') {
        loadAnalyticsData();
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal || modal.style.display === 'none') return;

    // Add closing animation class
    modal.classList.add('closing');
    
    // Wait for animation to complete before hiding
    setTimeout(() => {
        modal.style.display = 'none';
        modal.classList.remove('closing');
    }, 150);
}

function closeAllModals() {
    const modals = document.querySelectorAll('.modal');
    modals.forEach(modal => {
        if (modal.style.display !== 'none') {
            modal.classList.add('closing');
            setTimeout(() => {
                modal.style.display = 'none';
                modal.classList.remove('closing');
            }, 150);
        }
    });
}

// ==================== Analytics ====================
async function loadAnalyticsData() {
    try {
        // Get total chats from server
        const modes = ['research', 'thinking', 'coding'];
        let totalChats = 0;
        for (const m of modes) {
            const resp = await apiFetch(`/api/sessions?mode=${encodeURIComponent(m)}&limit=200&offset=0`);
            if (!resp.ok) continue;
            const data = await resp.json();
            totalChats += (data.sessions || []).length;
        }

        // Update stat cards
        document.getElementById('totalChats').textContent = totalChats;

        // Check API health
        const healthResponse = await fetch(`${API_BASE_URL}/api/chat/health`);
        const healthData = await healthResponse.json();

        const apiStatus = document.getElementById('apiStatus');
        const localAIStatus = document.getElementById('localAIStatus');

        if (apiStatus) {
            apiStatus.textContent = healthData.claude_api_configured ? 'Healthy' : 'Not Configured';
        }

        if (localAIStatus) {
            localAIStatus.textContent = healthData.local_ai_available ? 'Available' : 'Unavailable';
        }
    } catch (error) {
        console.error('Failed to load analytics:', error);
    }
}

// ==================== Theme Toggle ====================
function setupThemeToggle() {
    const toggle = document.getElementById('themeToggle');
    const darkModeToggle = document.getElementById('darkModeToggle');

    toggle?.addEventListener('click', toggleTheme);
    darkModeToggle?.addEventListener('change', (e) => {
        if (e.target.checked) {
            enableDarkMode();
        } else {
            disableDarkMode();
        }
    });
}

function toggleTheme() {
    if (state.darkMode) {
        disableDarkMode();
    } else {
        enableDarkMode();
    }
}

function enableDarkMode() {
    document.body.classList.add('dark-mode');
    state.darkMode = true;
    localStorage.setItem('theme', 'dark');

    const toggle = document.getElementById('themeToggle');
    if (toggle) {
        toggle.innerHTML = '<i class="fas fa-sun"></i>';
    }

    const darkModeToggle = document.getElementById('darkModeToggle');
    if (darkModeToggle) {
        darkModeToggle.checked = true;
    }
}

function disableDarkMode() {
    document.body.classList.remove('dark-mode');
    state.darkMode = false;
    localStorage.setItem('theme', 'light');

    const toggle = document.getElementById('themeToggle');
    if (toggle) {
        toggle.innerHTML = '<i class="fas fa-moon"></i>';
    }

    const darkModeToggle = document.getElementById('darkModeToggle');
    if (darkModeToggle) {
        darkModeToggle.checked = false;
    }
}

function loadThemePreference() {
    const savedTheme = localStorage.getItem('theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;

    if (savedTheme === 'dark' || (!savedTheme && prefersDark)) {
        enableDarkMode();
    } else {
        disableDarkMode();
    }
}

// ==================== Settings Handlers ====================
function setupSettingsHandlers() {
    const keyboardToggle = document.getElementById('keyboardToggle');
    const resetMetricsBtn = document.getElementById('resetMetricsBtn');
    const exportDataBtn = document.getElementById('exportDataBtn');
    const clearHistoryBtn = document.getElementById('clearHistoryBtn');

    // Keyboard shortcuts toggle
    keyboardToggle?.addEventListener('change', (e) => {
        localStorage.setItem('keyboardEnabled', e.target.checked);
        showToast(e.target.checked ? 'Keyboard shortcuts enabled' : 'Keyboard shortcuts disabled', 'info');
    });

    // Reset metrics
    resetMetricsBtn?.addEventListener('click', () => {
        showToast('Metrics reset', 'info');
    });

    // Export data
    exportDataBtn?.addEventListener('click', () => {
        exportChatHistory();
    });

    // Clear history
    clearHistoryBtn?.addEventListener('click', () => {
        if (confirm('Are you sure you want to clear all chat history? This cannot be undone.')) {
            clearAllChatHistory();
        }
    });

    // Load saved preferences
    const keyboardEnabled = localStorage.getItem('keyboardEnabled') !== 'false';
    if (keyboardToggle) {
        keyboardToggle.checked = keyboardEnabled;
    }
}

function exportChatHistory() {
    (async () => {
        const modes = ['research', 'thinking', 'coding'];
        const exportData = {};
        for (const mode of modes) {
            const resp = await apiFetch(`/api/sessions?mode=${encodeURIComponent(mode)}&limit=200&offset=0`);
            if (!resp.ok) continue;
            const data = await resp.json();
            const sessions = data.sessions || [];
            exportData[mode] = [];
            for (const s of sessions) {
                const detailResp = await apiFetch(`/api/sessions/${encodeURIComponent(s.id)}`);
                if (!detailResp.ok) continue;
                const detail = await detailResp.json();
                exportData[mode].push(detail);
            }
        }

        const dataStr = JSON.stringify(exportData, null, 2);
        const dataBlob = new Blob([dataStr], { type: 'application/json' });

        const url = URL.createObjectURL(dataBlob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `chat-history-${Date.now()}.json`;
        link.click();

        URL.revokeObjectURL(url);
        showToast('Chat history exported', 'success');
    })().catch(err => {
        console.error(err);
        showToast('Failed to export chat history', 'info');
    });
}

function clearAllChatHistory() {
    (async () => {
        const modes = ['research', 'thinking', 'coding'];
        for (const mode of modes) {
            const resp = await apiFetch(`/api/sessions?mode=${encodeURIComponent(mode)}&limit=200&offset=0`);
            if (!resp.ok) continue;
            const data = await resp.json();
            const sessions = data.sessions || [];
            for (const s of sessions) {
                await apiFetch(`/api/sessions/${encodeURIComponent(s.id)}`, { method: 'DELETE' });
            }
            state.sessions[mode] = [];
        }

        state.currentSessionId = null;
        await ensureCurrentServerSession();

        if (window.chatController) {
            window.chatController.clearMessages();
            if (window.chatController.setChatSessionId) {
                window.chatController.setChatSessionId(state.currentSessionId);
            }
        }

        showToast('Chat history cleared', 'success');
        closeModal('settingsModal');
    })().catch(err => {
        console.error(err);
        showToast('Failed to clear chat history', 'info');
    });
}

// ==================== System Health ====================
async function checkSystemHealth() {
    try {
        const response = await fetch(`${API_BASE_URL}/health`);
        const data = await response.json();

        updateSystemStatus(data);
    } catch (error) {
        console.error('Health check failed:', error);
        updateSystemStatus({ status: 'error' });
    }
}

function updateSystemStatus(data) {
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');

    if (!statusDot || !statusText) return;

    if (data.status === 'healthy') {
        statusDot.classList.add('healthy');
        statusText.textContent = 'System Healthy';
    } else {
        statusDot.classList.remove('healthy');
        statusText.textContent = 'Offline';
    }
}

// ==================== Keyboard Shortcuts ====================
function setupKeyboardShortcuts() {
    document.addEventListener('keydown', (e) => {
        const keyboardEnabled = localStorage.getItem('keyboardEnabled') !== 'false';
        if (!keyboardEnabled) return;

        const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
        const cmdKey = isMac ? e.metaKey : e.ctrlKey;

        // Cmd/Ctrl + K - Toggle sidebar (acts as command palette)
        if (cmdKey && e.key === 'k') {
            e.preventDefault();
            toggleSidebar();
            return;
        }

        // Cmd/Ctrl + N - New chat
        if (cmdKey && e.key === 'n') {
            e.preventDefault();
            createNewChat().catch(console.error);
            return;
        }

        // Escape - Close sidebar or modals
        if (e.key === 'Escape') {
            e.preventDefault();
            if (state.sidebarOpen) {
                closeSidebar();
            } else {
                closeAllModals();
            }
            return;
        }

        // Cmd/Ctrl + 1-3 - Switch modes
        if (cmdKey && ['1', '2', '3'].includes(e.key)) {
            e.preventDefault();
            const modes = ['research', 'thinking', 'coding'];
            switchMode(modes[parseInt(e.key) - 1]).catch(console.error);
            return;
        }
    });
}

// ==================== Toast Notifications ====================
function showToast(message, type = 'info', duration = 3000) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;

    container.appendChild(toast);

    // Auto-remove with exit animation
    setTimeout(() => {
        toast.classList.add('hiding');
        setTimeout(() => toast.remove(), 200);
    }, duration);
}

// ==================== Utility Functions ====================
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// ==================== Console Welcome ====================
console.log('%c✨ AI Research Assistant', 'font-size: 20px; font-weight: bold;');
console.log('%cMonochrome chat-first interface', 'font-size: 12px; color: #666;');
console.log('%cKeyboard shortcuts: ⌘K (sidebar) • ⌘N (new chat) • ⌘1-3 (modes) • ESC (close)', 'font-size: 11px; color: #999;');

// ==================== Legacy Migration (localStorage -> MongoDB) ====================
async function migrateLegacyLocalStorageSessions() {
    const legacy = localStorage.getItem('chat_sessions');
    if (!legacy) return;

    let parsed;
    try {
        parsed = JSON.parse(legacy);
    } catch {
        return;
    }

    if (!parsed || typeof parsed !== 'object') return;

    try {
        const modes = ['research', 'thinking', 'coding'];
        for (const mode of modes) {
            const sessions = parsed[mode] || [];
            for (const s of sessions) {
                const created = await createSessionOnServer(mode);
                const sid = created.id;
                const msgs = s.messages || [];
                for (const m of msgs) {
                    await apiFetch(`/api/sessions/${encodeURIComponent(sid)}/messages`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            role: m.role,
                            content: m.content,
                            format: m.format || 'text',
                            aiType: m.aiType,
                            extras: m.extras || {}
                        })
                    });
                }
                // Best-effort title preservation
                if (s.title) {
                    await apiFetch(`/api/sessions/${encodeURIComponent(sid)}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title: s.title })
                    });
                }
            }
        }

        localStorage.removeItem('chat_sessions');
        console.log('✅ Migrated legacy localStorage chat_sessions to MongoDB');
    } catch (e) {
        console.warn('⚠️ Legacy migration failed (will keep localStorage):', e);
    }
}
