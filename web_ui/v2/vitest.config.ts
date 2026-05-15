import { defineConfig } from "vitest/config";
import solid from "vite-plugin-solid";

// ``vite-plugin-solid`` and ``@solidjs/testing-library`` need the
// browser / development resolution conditions so ``solid-js/web``
// resolves to the client (DOM) entry rather than the server one.
// Without this, anything that imports a Solid component (e.g. via
// ``components/ui/index.ts``) blows up with "Client-only API called
// on the server side" the moment ``splitProps``/``createSignal``
// runs in the test.
export default defineConfig({
  plugins: [solid({ hot: false, ssr: false })],
  resolve: {
    conditions: ["development", "browser"],
  },
  test: {
    environment: "happy-dom",
    globals: false,
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    server: {
      // ``@solidjs/testing-library`` is ESM-only and ships its own
      // resolution; vitest's bundle pipeline must NOT externalise it.
      deps: {
        inline: [/solid-js/, /@solidjs\/testing-library/],
      },
    },
  },
});
