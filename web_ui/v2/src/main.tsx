/* @refresh reload */
import { render } from "solid-js/web";
import { Router, Route } from "@solidjs/router";
import { QueryClient, QueryClientProvider } from "@tanstack/solid-query";

import "./styles/theme.css";
import "./styles/global.css";

import { App } from "./App";
import { Showcase } from "./routes/Showcase";

const root = document.getElementById("root");
if (!root) {
  throw new Error("AMOR v2: #root not found in document");
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 30_000, // 30 s — matches diagnostics dashboard cadence
      retry: 1,
    },
  },
});

render(
  () => (
    <QueryClientProvider client={queryClient}>
      {/* base="/v2" — FastAPI mounts the SPA shell at /v2 and the
          ``/v2/{rest:path}`` catch-all serves the same shell so deep
          links work.  Without `base`, SolidJS Router would treat the
          full pathname as the route and "/v2/showcase" wouldn't match
          either route below. */}
      <Router base="/v2">
        <Route path="/" component={App} />
        <Route path="/showcase" component={Showcase} />
      </Router>
    </QueryClientProvider>
  ),
  root,
);
