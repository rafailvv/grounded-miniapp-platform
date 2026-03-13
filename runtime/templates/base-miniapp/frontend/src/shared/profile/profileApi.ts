import { httpClient } from '@/shared/http/httpClient';
import type { AppRole } from '@/shared/roles/role';

export type RemoteProfilePayload = {
  first_name: string;
  last_name?: string;
  email?: string;
  phone?: string;
  photo_url?: string | null;
  updated_at?: string | null;
};

export type RemoteDashboard = {
  role: AppRole;
  title: string;
  description: string;
  feature_text: string;
  metrics: Array<{
    metric_id: string;
    label: string;
    value: string;
  }>;
  primary_action_label: string;
  secondary_action_label?: string | null;
};

function apiEnabled(): boolean {
  return import.meta.env.VITE_DISABLE_API !== '1';
}

export async function fetchRoleProfile(role: AppRole): Promise<RemoteProfilePayload | null> {
  if (!apiEnabled()) return null;
  try {
    return await httpClient.get<RemoteProfilePayload>(`/api/profiles/${role}`);
  } catch {
    return null;
  }
}

export async function persistRoleProfile(role: AppRole, profile: RemoteProfilePayload): Promise<void> {
  if (!apiEnabled()) return;
  await httpClient.put<RemoteProfilePayload>(`/api/profiles/${role}`, profile);
}

export async function fetchRoleDashboard(role: AppRole): Promise<RemoteDashboard | null> {
  if (!apiEnabled()) return null;
  try {
    return await httpClient.get<RemoteDashboard>(`/api/dashboard/${role}`);
  } catch {
    return null;
  }
}
