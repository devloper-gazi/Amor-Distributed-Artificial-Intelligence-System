import { type Component } from "solid-js";
import { A } from "@solidjs/router";
import { TopBar } from "../components/shell/TopBar";

export const NotFound: Component = () => (
  <div data-mode="system" class="flex h-full flex-col">
    <TopBar title="Not found" subtitle="this route doesn't exist" />
    <div class="flex flex-1 items-center justify-center px-6 py-8">
      <div class="max-w-md text-center">
        <p class="text-2xl font-semibold tracking-tight">404</p>
        <p class="mt-2 text-sm text-text-body">
          That URL doesn't lead anywhere in v2.  Try the home screen
          or open a specific mode from the sidebar.
        </p>
        <div class="mt-6 flex justify-center gap-2">
          <A
            href="/"
            class="inline-flex h-9 items-center rounded-md border border-border-strong-v25 bg-bg-elevated px-4 text-sm hover:bg-bg-hover"
          >
            Home
          </A>
        </div>
      </div>
    </div>
  </div>
);
