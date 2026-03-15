import { httpClient } from '@/shared/http/httpClient';
import type { AppRole } from '@/shared/roles/role';
import type { RuntimeActionResult, RuntimeRoleManifest } from '@/shared/runtime/types';

function apiEnabled(): boolean {
  return import.meta.env.VITE_DISABLE_API !== '1';
}

function getFallbackManifest(role: AppRole): RuntimeRoleManifest {
  return {
    role,
    entry_path: '/',
    routes: [],
    navigation: [],
    screens: {},
    metrics: [],
    profile: {
      first_name: '',
      last_name: '',
      email: '',
      phone: '',
    },
    alerts: [],
    activity: [],
    app: {
      title: '',
      goal: '',
      generation_mode: 'basic',
      route_count: 0,
      screen_count: 0,
    },
  };
}

export async function fetchRuntimeManifest(role: AppRole): Promise<RuntimeRoleManifest> {
  if (!apiEnabled()) return getFallbackManifest(role);
  try {
    return await httpClient.get<RuntimeRoleManifest>(`/api/runtime/${role}/manifest`);
  } catch {
    return getFallbackManifest(role);
  }
}

export async function executeRuntimeAction(
  role: AppRole,
  actionId: string,
  body: {
    payload?: Record<string, unknown>;
    item_id?: string | null;
    current_path?: string | null;
    screen_id?: string | null;
  } = {},
): Promise<RuntimeActionResult> {
  if (!apiEnabled()) {
    return {
      status: 'ok',
      message: '',
      next_path: body.current_path ?? '/',
      refresh_manifest: true,
    };
  }
  return httpClient.post<RuntimeActionResult>(`/api/runtime/${role}/actions/${actionId}`, body);
}
