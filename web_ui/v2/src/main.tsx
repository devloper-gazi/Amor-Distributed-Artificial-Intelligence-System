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
            <Route path="/" component={Home} />
            <Route path="/research" component={Research} />
            <Route path="/build" component={Build} />
            <Route path="/thinking" component={Thinking} />
            <Route path="/consortium" component={Consortium} />
            <Route path="/sentinel" component={Sentinel} />
            <Route path="/system" component={Diagnostics} />
            <Route path="/settings" component={Settings} />
          </Route>

          <Route path="*" component={NotFound} />
        </Router>
      </QueryClientProvider>
    </ErrorBoundary>
  ),
  root,
);
