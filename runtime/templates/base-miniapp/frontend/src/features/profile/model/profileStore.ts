import { type RemoteProfilePayload, fetchRoleProfile, persistRoleProfile } from '@/features/profile/api/profileApi';
import type { AppRole } from '@/entities/role/model/role';
import { getTelegramWebApp } from '@/shared/telegram/webApp';

const ROLE_LABELS: Record<AppRole, string> = {
  client: 'Client',
  specialist: 'Specialist',
  manager: 'Manager',
};

export type RoleProfileDraft = {
  firstName: string;
  lastName: string;
  email: string;
  phone: string;
  photoUrl: string | null;
};

export type RoleProfileView = RoleProfileDraft & {
  username: string;
  roleLabel: string;
};

function getTelegramUser() {
  return getTelegramWebApp()?.initDataUnsafe?.user;
}

export function createEmptyRoleProfileDraft(): RoleProfileDraft {
  return {
    firstName: '',
    lastName: '',
    email: '',
    phone: '',
    photoUrl: null,
  };
}

export function getTelegramUsernameLabel(username: string | undefined): string {
  if (!username) return 'Telegram user';
  return username.startsWith('@') ? username : `@${username}`;
}

export function getRoleLabel(role: AppRole): string {
  return ROLE_LABELS[role];
}

export function remoteProfileToDraft(profile: RemoteProfilePayload | null): RoleProfileDraft {
  if (!profile) {
    return createEmptyRoleProfileDraft();
  }

  return {
    firstName: profile.first_name?.trim() ?? '',
    lastName: profile.last_name?.trim() ?? '',
    email: profile.email?.trim() ?? '',
    phone: profile.phone?.trim() ?? '',
    photoUrl: profile.photo_url ?? null,
  };
}

export function createRoleProfileView(role: AppRole, draft: RoleProfileDraft): RoleProfileView {
  const telegramUser = getTelegramUser();

  return {
    ...draft,
    username: getTelegramUsernameLabel(telegramUser?.username),
    roleLabel: getRoleLabel(role),
  };
}

export async function loadRoleProfileDraftFromBackend(role: AppRole): Promise<RoleProfileDraft> {
  const remoteProfile = await fetchRoleProfile(role);
  return remoteProfileToDraft(remoteProfile);
}

export async function saveRoleProfileDraft(role: AppRole, profile: RoleProfileDraft): Promise<RoleProfileDraft> {
  const payload: RemoteProfilePayload = {
    first_name: profile.firstName.trim(),
    last_name: profile.lastName.trim(),
    email: profile.email.trim(),
    phone: profile.phone.trim(),
    photo_url: profile.photoUrl ?? null,
  };
  await persistRoleProfile(role, payload);
  return remoteProfileToDraft(payload);
}

export function getRoleProfileDisplayName(profile: Pick<RoleProfileView, 'firstName' | 'lastName' | 'roleLabel'>): string {
  const fullName = `${profile.firstName} ${profile.lastName}`.trim();
  return fullName || `${profile.roleLabel} profile`;
}
