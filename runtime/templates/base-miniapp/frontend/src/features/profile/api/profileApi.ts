import type { AppRole } from '@/entities/role/model/role';
import { httpClient } from '@/shared/api/httpClient';

export type RemoteProfilePayload = {
  first_name: string;
  last_name?: string;
  email?: string;
  phone?: string;
  photo_url?: string | null;
  updated_at?: string | null;
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
