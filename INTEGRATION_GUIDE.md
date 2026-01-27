# AI Chat Research Integration Guide

## Overview
This guide shows you how to integrate the AI Chat Research interface into your main application at `http://localhost:8000/#ai-chat`.

## Step 1: Add CSS Link to index.html

In `web_ui/templates/index.html`, update line 7-8 to include the chat research CSS:

```html
<link rel="stylesheet" href="/static/css/styles.css">
<link rel="stylesheet" href="/static/css/chat-research.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
```

## Step 2: Add Navigation Item

In `web_ui/templates/index.html`, add this after line 113 (after the Coding nav item):

```html
<li class="nav-item" data-page="ai-chat" data-module="ai-chat" role="menuitem">
    <i class="fas fa-comments nav-icon"></i>
    <span class="nav-text">AI Chat</span>
    <kbd class="nav-shortcut">⌘5</kbd>
</li>
```

## Step 3: Add the AI Chat Page Content

In `web_ui/templates/index.html`, add this BEFORE the Settings Page section (before line 532):

```html
<!-- AI Chat Module -->
<div id="ai-chat-page" class="page" role="tabpanel" aria-labelledby="ai-chat-tab" style="padding: 0; height: calc(100vh - 60px); display: flex; flex-direction: column;">
    <!-- Chat Research Interface -->
    <div class="chat-research-container" style="flex: 1; display: flex; flex-direction: column; height: 100%;">
        <!-- Header -->
        <header class="chat-header" style="flex-shrink: 0;">
            <div class="chat-header-content">
                <h1 class="chat-title">AI Research Assistant</h1>
                <div class="ai-toggle">
                    <label class="toggle-label">
                        <input type="checkbox" id="useClaudeAPI" class="toggle-checkbox">
                        <span class="toggle-slider"></span>
                        <span class="toggle-text">
                            <span class="local-text">Local AI</span>
                            <span class="claude-text">Claude API</span>
                        </span>
                    </label>
                </div>
            </div>
        </header>

        <!-- Main Chat Container -->
        <div class="chat-container" style="flex: 1; display: flex; flex-direction: column; min-height: 0;">
            <!-- Messages Area -->
            <div class="messages-area" id="messagesArea" style="flex: 1; overflow-y: auto;">
                <div class="welcome-message">
                    <div class="welcome-icon">🔍</div>
                    <h2>Welcome to AI Research</h2>
                    <p>Ask me anything you'd like to research. I can:</p>
                    <ul>
                        <li>Conduct comprehensive research on any topic</li>
                        <li>Search and scrape web sources</li>
                        <li>Analyze and synthesize findings</li>
                        <li>Provide detailed, well-structured reports</li>
                    </ul>
                    <p class="ai-mode-hint">
                        <strong>Current Mode:</strong> <span id="currentAIMode">Local AI (Offline + Web Scraping)</span>
                    </p>
                </div>
            </div>

            <!-- Input Area -->
            <div class="input-area" style="flex-shrink: 0;">
                <div class="input-container">
                    <textarea
                        id="chatInput"
                        placeholder="What would you like me to research? (e.g., 'Research the latest advancements in quantum computing')"
                        rows="1"
                    ></textarea>
                    <button id="sendButton" class="send-button">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="22" y1="2" x2="11" y2="13"></line>
                            <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
                        </svg>
                    </button>
                </div>
                <div class="input-footer">
                    <span class="character-count" id="characterCount">0 characters</span>
                    <span class="ai-indicator" id="aiIndicator">
                        <span class="indicator-dot"></span>
                        <span id="aiModeText">Local AI</span>
                    </span>
                </div>
            </div>
        </div>

        <!-- Research Progress Modal -->
        <div class="progress-modal" id="progressModal" style="display: none;">
            <div class="progress-content">
                <div class="progress-header">
                    <h3>Research in Progress</h3>
                    <div class="progress-status" id="progressStatus">Initializing...</div>
                </div>
                <div class="progress-bar-container">
                    <div class="progress-bar" id="progressBar" style="width: 0%"></div>
                </div>
                <div class="agent-activity" id="agentActivity">
                    <div class="agent-item">
                        <span class="agent-icon">🔍</span>
                        <span class="agent-name">Research Specialist</span>
                        <span class="agent-status">Idle</span>
                    </div>
                    <div class="agent-item">
                        <span class="agent-icon">📊</span>
                        <span class="agent-name">Data Analyst</span>
                        <span class="agent-status">Idle</span>
                    </div>
                    <div class="agent-item">
                        <span class="agent-icon">✍️</span>
                        <span class="agent-name">Technical Writer</span>
                        <span class="agent-status">Idle</span>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>
```

## Step 4: Add JavaScript

In `web_ui/templates/index.html`, update line 631 to include the chat research script:

```html
<script src="/static/js/app.js"></script>
<script src="/static/js/chat-research.js"></script>
```

## Step 5: Copy Updated Files to Docker Container

Run these commands to copy the updated files:

```bash
docker cp web_ui/templates/index.html amor-app-local:/app/web_ui/templates/
docker cp web_ui/static/css/chat-research.css amor-app-local:/app/web_ui/static/css/
docker cp web_ui/static/js/chat-research.js amor-app-local:/app/web_ui/static/js/
```

## Done!

Now you can access:
- **Main Application**: `http://localhost:8000/`
- **AI Chat Research**: `http://localhost:8000/#ai-chat`

Click on "AI Chat" in the sidebar or press `⌘5` to navigate to the chat interface!

## Features

- **Integrated Navigation**: Seamlessly switch between modules
- **Hash-based Routing**: Works with your existing navigation system
- **Web Scraping**: Local AI with real web source scraping
- **Claude API**: Toggle to use Claude for research
- **All Other Modules**: Remain intact and functional
