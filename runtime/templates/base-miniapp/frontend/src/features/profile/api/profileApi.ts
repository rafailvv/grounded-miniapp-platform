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

export async function fetchRoleProfile(role: AppRole): Promise<RemoteProfilePayload | null> {
  try {
    return await httpClient.get<RemoteProfilePayload>(`/api/profiles/${role}`);
  } catch {
    return null;
  }
}

export async function persistRoleProfile(role: AppRole, profile: RemoteProfilePayload): Promise<void> {
  await httpClient.put<RemoteProfilePayload>(`/api/profiles/${role}`, profile);
}
