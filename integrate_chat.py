#!/usr/bin/env python3
"""
Auto-integration script for AI Chat Research interface
This script integrates the chat research interface into the main application
"""

import re

def integrate_chat_interface():
    """Integrate chat research into index.html"""

    # Read the current index.html
    with open('web_ui/templates/index.html', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Add CSS link
    if 'chat-research.css' not in content:
        content = content.replace(
            '<link rel="stylesheet" href="/static/css/styles.css">',
            '<link rel="stylesheet" href="/static/css/styles.css">\n    <link rel="stylesheet" href="/static/css/chat-research.css">'
        )
        print("✓ Added chat-research.css link")
    else:
        print("- chat-research.css already included")

    # 2. Add navigation item
    if 'data-page="ai-chat"' not in content:
        nav_item = '''                    <li class="nav-item" data-page="ai-chat" data-module="ai-chat" role="menuitem">
                        <i class="fas fa-comments nav-icon"></i>
                        <span class="nav-text">AI Chat</span>
                        <kbd class="nav-shortcut">⌘5</kbd>
                    </li>
'''
        content = content.replace(
            '''                    <li class="nav-item" data-page="coding" data-module="coding" role="menuitem">
                        <i class="fas fa-code nav-icon"></i>
                        <span class="nav-text">Coding</span>
                        <kbd class="nav-shortcut">⌘4</kbd>
                    </li>
                </ul>
            </div>

            <!-- System Section -->''',
            '''                    <li class="nav-item" data-page="coding" data-module="coding" role="menuitem">
                        <i class="fas fa-code nav-icon"></i>
                        <span class="nav-text">Coding</span>
                        <kbd class="nav-shortcut">⌘4</kbd>
                    </li>
''' + nav_item + '''                </ul>
            </div>

            <!-- System Section -->'''
        )
        print("✓ Added AI Chat navigation item")
    else:
        print("- AI Chat nav item already exists")

    # 3. Add AI Chat page content
    if 'id="ai-chat-page"' not in content:
        ai_chat_page = '''
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
'''
        content = content.replace(
            '        <!-- Settings Page -->',
            ai_chat_page + '\n        <!-- Settings Page -->'
        )
        print("✓ Added AI Chat page content")
    else:
        print("- AI Chat page already exists")

    # 4. Add JavaScript
    if 'chat-research.js' not in content:
        content = content.replace(
            '<script src="/static/js/app.js"></script>',
            '<script src="/static/js/app.js"></script>\n    <script src="/static/js/chat-research.js"></script>'
        )
        print("✓ Added chat-research.js script")
    else:
        print("- chat-research.js already included")

    # Write the updated content
    with open('web_ui/templates/index.html', 'w', encoding='utf-8') as f:
        f.write(content)

    print("\n✓✓✓ Integration complete! ✓✓✓")
    print("\nNext steps:")
    print("1. Run: docker cp web_ui/templates/index.html amor-app-local:/app/web_ui/templates/")
    print("2. Open: http://localhost:8000/#ai-chat")
    print("3. Click 'AI Chat' in the sidebar or press ⌘5")

if __name__ == '__main__':
    try:
        integrate_chat_interface()
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
