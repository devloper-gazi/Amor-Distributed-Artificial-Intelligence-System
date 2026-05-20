/* @refresh reload */
import { render } from "solid-js/web";
import {
  Router,
  Route,
  Navigate,
  type RouteSectionProps,
} from "@solidjs/router";
import {
  type Component,
  Show,
  createSignal,
  ErrorBoundary,
  onMount,
} from "solid-js";
import { QueryClient, QueryClientProvider } from "@tanstack/solid-query";

import "./styles/theme.css";
import "./styles/global.css";

import { AppShell } from "./components/shell/AppShell";
import { Spinner } from "./components/ui";
import { auth } from "./lib/auth";

import { Home } from "./routes/Home";
import { Research } from "./routes/Research";
import { Build } from "./routes/Build";
import { Thinking } from "./routes/Thinking";
import { Consortium } from "./routes/Consortium";
import { Sentinel } from "./routes/Sentinel";
import { Diagnostics } from "./routes/Diagnostics";
import { Settings } from "./routes/Settings";
import { Login } from "./routes/Login";
import { NotFound } from "./routes/NotFound";
import { Showcase } from "./routes/Showcase";
// Cycle C Sprint 0 Day 3 — admin baselines dashboard.
import { Baselines } from "./routes/Baselines";
// Cycle C Sprint 1 Day 4 — admin LLM dashboard.
import { LLMDashboard } from "./routes/LLM";
// Cycle C Sprint 2 Day 5 — admin Evals dashboard.
import { Evals } from "./routes/Evals";
// Cycle C Sprint 4 Day 1 — mode-agnostic chat surface (legacy).
import { Chat } from "./routes/Chat";
// Cycle UI 2026-05-20 — Unified chat single-page route at `/`.
// Replaces the 6-route mode-segregated SPA as the primary surface.
// Legacy /build /research /thinking /consortium /sentinel routes
// stay mounted below for rollback (?ui=v1 query param navigates
// back to /home when needed).
import { UnifiedChat } from "./routes/UnifiedChat";
// Cycle C Sprint 6 Day 4 — admin Training surface.
import { Training } from "./routes/Training";
// Cycle C Sprint 7 Day 3 — admin Memory viewer (Mem0 OSS).
import { Memory } from "./routes/Memory";
// Cycle C Sprint 8 Day 4 — agentic ReAct loop UI.
import { Agent } from "./routes/Agent";
// Cycle C Sprint 12 Day 1 — PWA service-worker registration.
import { registerServiceWorker } from "./lib/pwa";

const root = document.getElementById("root");
if (!root) {
  throw new Error("AMOR v2: #root not found in document");
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: 1,
    },
  },
});

// Cycle D — expose the client so module-scoped code (Build/Research/
// Thinking ``start`` functions, defined outside any component) can
// invalidate the Sessions sidebar query as soon as a new chat session
// is created.
import { setQueryClient } from "./lib/query-client";
setQueryClient(queryClient);

/* Apply saved theme before any component renders so the user
 * doesn't see a flash of the wrong theme. */
const applyInitialTheme = () => {
  try {
    const saved = localStorage.getItem("amor.theme");
    if (saved === "light" || saved === "dark" || saved === "system") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch {
    // ignore
  }
};
applyInitialTheme();

// Cycle C Sprint 12 Day 1 — register the service worker.  No-op
// in dev, no-op when the operator set ``localStorage["amor.pwa"]
// = "off"``, no-op when navigator.serviceWorker is missing.
// Errors are swallowed so a registration hiccup never blocks boot.
void registerServiceWorker();

/** Auth gate — wraps every route under AppShell.  When auth.user()
 *  is null AFTER bootstrap, redirect to /login.  The AppShell only
 *  mounts inside this gate. */
const Protected: Component<RouteSectionProps> = (props) => {
  return (
    <Show
      when={auth.bootstrapped()}
      fallback={
        <div class="flex h-screen items-center justify-center bg-bg-primary text-text-tertiary">
          <Spinner size={20} />
        </div>
      }
    >
      <Show when={auth.user()} fallback={<Navigate href="/login" />}>
        <AppShell>{props.children}</AppShell>
      </Show>
    </Show>
  );
};

/** Top-level error fallback so a thrown render error doesn't leave
 *  a white screen.  Logs to console + lets the user reset / open
 *  legacy. */
const ErrorFallback: Component<{
  err: unknown;
  reset: () => void;
}> = (props) => {
  const detail = (): string => {
    if (props.err instanceof Error) return props.err.message;
    return String(props.err);
  };
  return (
    <div class="flex min-h-screen items-center justify-center bg-bg-primary px-4 text-text-primary">
      <div class="max-w-lg space-y-3 rounded-lg border border-border-subtle bg-bg-elevated p-6">
        <h1 class="text-lg font-semibold tracking-tight">
          Something went wrong
        </h1>
        <p class="text-sm text-text-secondary">{detail()}</p>
        <button
          type="button"
          class="inline-flex h-9 items-center rounded-md bg-text-primary px-4 text-sm font-medium text-text-inverse hover:opacity-90"
          onClick={props.reset}
        >
          Reload
        </button>
      </div>
    </div>
  );
};

const App: Component<RouteSectionProps> = (props) => {
  const [bootstrapStarted, setBootstrapStarted] = createSignal(false);
  onMount(() => {
    if (!bootstrapStarted()) {
      setBootstrapStarted(true);
      auth.bootstrap();
    }
  });
  return <>{props.children}</>;
};

render(
  () => (
    <ErrorBoundary fallback={(err, reset) => <ErrorFallback err={err} reset={reset} />}>
      <QueryClientProvider client={queryClient}>
        {/* No base — FastAPI's catch-all SPA fallback at
            ``/{spa_path:path}`` serves the same shell for every
            non-API URL, so SolidJS Router routes match on the full
            pathname (e.g. ``/research`` → Research). */}
        <Router root={App}>
          {/* Public */}
          <Route path="/login" component={Login} />
          <Route path="/showcase" component={Showcase} />

          {/* Protected — every route here mounts inside AppShell */}
          <Route path="/" component={Protected}>
            {/* Cycle UI 2026-05-20 — UnifiedChat replaces Home at /.
                Legacy welcome page moves to /home for operator
                override / rollback. */}
            <Route path="/" component={UnifiedChat} />
            <Route path="/home" component={Home} />
            <Route path="/research" component={Research} />
            <Route path="/build" component={Build} />
            <Route path="/thinking" component={Thinking} />
            <Route path="/consortium" component={Consortium} />
            <Route path="/sentinel" component={Sentinel} />
            <Route path="/system" component={Diagnostics} />
            <Route path="/settings" component={Settings} />
            <Route path="/admin/baselines" component={Baselines} />
            <Route path="/admin/llm" component={LLMDashboard} />
            <Route path="/admin/evals" component={Evals} />
            <Route path="/admin/training" component={Training} />
            <Route path="/admin/memory" component={Memory} />
            <Route path="/agent" component={Agent} />
            <Route path="/chat" component={Chat} />
          </Route>

          <Route path="*" component={NotFound} />
        </Router>
      </QueryClientProvider>
    </ErrorBoundary>
  ),
  root,
);
