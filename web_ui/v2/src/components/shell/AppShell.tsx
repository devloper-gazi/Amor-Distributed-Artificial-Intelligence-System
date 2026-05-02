import { type Component, type JSX, createSignal, onMount } from "solid-js";
import { Sidebar } from "./Sidebar";

interface AppShellProps {
  children: JSX.Element;
}

/**
 * Top-level layout.  Sidebar + main content with a sliding-collapse
 * sidebar.  The collapse state persists in localStorage so the user's
 * chrome density carries across reloads.
 */
export const AppShell: Component<AppShellProps> = (props) => {
  const [collapsed, setCollapsed] = createSignal(false);

  onMount(() => {
    try {
      const saved = localStorage.getItem("amor.sidebar.collapsed");
      if (saved === "1") setCollapsed(true);
    } catch {
      // ignore — localStorage may be unavailable in private modes
    }
  });

  const toggle = () => {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem("amor.sidebar.collapsed", next ? "1" : "0");
      } catch {
        // ignore
      }
      return next;
    });
  };

  return (
    <div class="flex h-full bg-bg-primary text-text-primary">
      <Sidebar collapsed={collapsed()} onToggle={toggle} />
      <main class="flex min-w-0 flex-1 flex-col">{props.children}</main>
    </div>
  );
};
