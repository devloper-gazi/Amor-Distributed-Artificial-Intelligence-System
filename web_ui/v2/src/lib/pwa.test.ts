/**
 * Cycle C Sprint 12 Day 1 — PWA registration helper tests.
 *
 * The module's contract is "do nothing dangerous in three guarded
 * paths":
 *   1. Service worker missing (older Safari / privacy modes)
 *   2. Dev build (HMR-friendly)
 *   3. Operator opted out via ``localStorage["amor.pwa"] = "off"``
 *
 * Plus a happy path that calls ``navigator.serviceWorker.register``
 * with the right scope.
 */

import { afterEach, describe, expect, it, vi } from "vitest";


// happy-dom storage shim (matches the i18n test pattern).
const memoryStore: Record<string, string> = {};
const memoryStorage: Storage = {
  get length() {
    return Object.keys(memoryStore).length;
  },
  key(i: number) {
    return Object.keys(memoryStore)[i] ?? null;
  },
  getItem(k: string) {
    return Object.prototype.hasOwnProperty.call(memoryStore, k)
      ? memoryStore[k]!
      : null;
  },
  setItem(k: string, v: string) {
    memoryStore[k] = v;
  },
  removeItem(k: string) {
    delete memoryStore[k];
  },
  clear() {
    for (const k of Object.keys(memoryStore)) delete memoryStore[k];
  },
};
(globalThis as unknown as { localStorage: Storage }).localStorage = memoryStorage;


function installFakeNavigator(opts: {
  withSW?: boolean;
  registerImpl?: typeof navigator.serviceWorker.register;
} = {}) {
  if (!opts.withSW) {
    Object.defineProperty(globalThis, "navigator", {
      value: {},
      configurable: true,
      writable: true,
    });
    return;
  }
  const fakeReg = {
    waiting: null,
    unregister: vi.fn().mockResolvedValue(true),
  };
  const register = opts.registerImpl
    ?? vi.fn().mockResolvedValue(fakeReg);
  const getRegistrations = vi.fn().mockResolvedValue([fakeReg]);
  Object.defineProperty(globalThis, "navigator", {
    value: {
      serviceWorker: { register, getRegistrations },
    },
    configurable: true,
    writable: true,
  });
  return { register, getRegistrations, fakeReg };
}


afterEach(() => {
  memoryStorage.clear();
  vi.unstubAllEnvs();
  vi.resetModules();
});


// ─── feature detection ─────────────────────────────────────────


describe("serviceWorkerSupported()", () => {
  it("returns false when navigator has no serviceWorker", async () => {
    installFakeNavigator({ withSW: false });
    const mod = await import("./pwa");
    expect(mod.serviceWorkerSupported()).toBe(false);
  });

  it("returns true when navigator.serviceWorker exists", async () => {
    installFakeNavigator({ withSW: true });
    const mod = await import("./pwa");
    expect(mod.serviceWorkerSupported()).toBe(true);
  });
});


// ─── operator override ────────────────────────────────────────


describe("pwaForceDisabled()", () => {
  it("returns false when the LS key is unset", async () => {
    const mod = await import("./pwa");
    expect(mod.pwaForceDisabled()).toBe(false);
  });

  it("returns true when LS key is 'off'", async () => {
    memoryStorage.setItem("amor.pwa", "off");
    const mod = await import("./pwa");
    expect(mod.pwaForceDisabled()).toBe(true);
  });

  it("ignores other LS values", async () => {
    memoryStorage.setItem("amor.pwa", "on");
    const mod = await import("./pwa");
    expect(mod.pwaForceDisabled()).toBe(false);
  });
});


// ─── registerServiceWorker ────────────────────────────────────


describe("registerServiceWorker()", () => {
  it("returns null when SW is unsupported", async () => {
    installFakeNavigator({ withSW: false });
    vi.stubEnv("MODE", "production");
    vi.stubEnv("PROD", true);
    const mod = await import("./pwa");
    const reg = await mod.registerServiceWorker();
    expect(reg).toBeNull();
  });

  it("returns null in dev mode (no register call)", async () => {
    const inst = installFakeNavigator({ withSW: true })!;
    vi.stubEnv("MODE", "development");
    vi.stubEnv("PROD", false);
    const mod = await import("./pwa");
    const reg = await mod.registerServiceWorker();
    expect(reg).toBeNull();
    expect(inst.register).not.toHaveBeenCalled();
  });

  it("returns null when operator has set amor.pwa=off", async () => {
    memoryStorage.setItem("amor.pwa", "off");
    const inst = installFakeNavigator({ withSW: true })!;
    vi.stubEnv("MODE", "production");
    vi.stubEnv("PROD", true);
    const mod = await import("./pwa");
    const reg = await mod.registerServiceWorker();
    expect(reg).toBeNull();
    expect(inst.register).not.toHaveBeenCalled();
    // Disabled state forces an unregister sweep so a stale prior
    // SW doesn't keep serving cached responses.
    expect(inst.getRegistrations).toHaveBeenCalled();
  });

  it("registers with scope='/' in production", async () => {
    const inst = installFakeNavigator({ withSW: true })!;
    vi.stubEnv("MODE", "production");
    vi.stubEnv("PROD", true);
    const mod = await import("./pwa");
    const reg = await mod.registerServiceWorker();
    expect(reg).not.toBeNull();
    expect(inst.register).toHaveBeenCalledTimes(1);
    const args = (inst.register as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(args[0]).toBe("/sw.js");
    expect(args[1]).toMatchObject({ scope: "/", type: "classic" });
  });

  it("swallows registration errors", async () => {
    const failing = vi.fn().mockRejectedValue(new Error("network"));
    installFakeNavigator({ withSW: true, registerImpl: failing });
    vi.stubEnv("MODE", "production");
    vi.stubEnv("PROD", true);
    const mod = await import("./pwa");
    const reg = await mod.registerServiceWorker();
    expect(reg).toBeNull();
    expect(failing).toHaveBeenCalled();
  });
});


// ─── unregisterServiceWorker ─────────────────────────────────


describe("unregisterServiceWorker()", () => {
  it("returns false when SW is unsupported", async () => {
    installFakeNavigator({ withSW: false });
    const mod = await import("./pwa");
    expect(await mod.unregisterServiceWorker()).toBe(false);
  });

  it("walks every registration and unregisters them", async () => {
    const inst = installFakeNavigator({ withSW: true })!;
    const mod = await import("./pwa");
    const ok = await mod.unregisterServiceWorker();
    expect(ok).toBe(true);
    expect(inst.fakeReg.unregister).toHaveBeenCalledTimes(1);
  });
});
