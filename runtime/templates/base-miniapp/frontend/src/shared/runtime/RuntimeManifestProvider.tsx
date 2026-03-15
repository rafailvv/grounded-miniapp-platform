import { createContext, ReactNode, useContext, useEffect, useMemo, useState } from 'react';
import type { AppRole } from '@/shared/roles/role';
import { executeRuntimeAction, fetchRuntimeManifest } from '@/shared/runtime/runtimeApi';
import type { RuntimeActionResult, RuntimeRoleManifest } from '@/shared/runtime/types';

type RuntimeManifestContextValue = {
  manifest: RuntimeRoleManifest | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  runAction: (
    actionId: string,
    body?: {
      payload?: Record<string, unknown>;
      item_id?: string | null;
      current_path?: string | null;
      screen_id?: string | null;
    },
  ) => Promise<RuntimeActionResult>;
};

const RuntimeManifestContext = createContext<RuntimeManifestContextValue | null>(null);

type RuntimeManifestProviderProps = {
  role: AppRole;
  children: ReactNode;
};

export function RuntimeManifestProvider({ role, children }: RuntimeManifestProviderProps): JSX.Element {
  const [manifest, setManifest] = useState<RuntimeRoleManifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const payload = await fetchRuntimeManifest(role);
      setManifest(payload);
    } catch (runtimeError) {
      setError(runtimeError instanceof Error ? runtimeError.message : 'Failed to load runtime manifest.');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [role]);

  async function runAction(
    actionId: string,
    body: {
      payload?: Record<string, unknown>;
      item_id?: string | null;
      current_path?: string | null;
      screen_id?: string | null;
    } = {},
  ) {
    const result = await executeRuntimeAction(role, actionId, body);
    if (result.refresh_manifest !== false) {
      await refresh();
    }
    return result;
  }

  const value = useMemo<RuntimeManifestContextValue>(
    () => ({
      manifest,
      loading,
      error,
      refresh,
      runAction,
    }),
    [manifest, loading, error],
  );

  return <RuntimeManifestContext.Provider value={value}>{children}</RuntimeManifestContext.Provider>;
}

export function useRuntimeManifest(): RuntimeManifestContextValue {
  const context = useContext(RuntimeManifestContext);
  if (!context) {
    throw new Error('useRuntimeManifest must be used inside RuntimeManifestProvider.');
  }
  return context;
}
