import { getDeviceStorageItem, getTelegramWebApp, setDeviceStorageItem } from '@/shared/telegram/webApp';
import type { AppRole } from '@/shared/roles/role';
import { fetchRoleProfile, persistRoleProfile, type RemoteProfilePayload } from '@/shared/profile/profileApi';

const ROLE_LABELS: Record<AppRole, string> = {
  client: 'Клиент',
  specialist: 'Специалист',
  manager: 'Менеджер',
};
export const CLIENT_ROLE_LABEL = 'Клиент';

export type ClientProfileDraft = {
  firstName: string;
  lastName: string;
  email: string;
  phone: string;
  photoUrl: string | null;
};

export type ClientProfileView = ClientProfileDraft & {
  username: string;
  roleLabel: string;
};

type StoredProfile = Partial<ClientProfileDraft> & {
  updatedAt?: number;
};

function getRoleProfileStorageKey(role: AppRole): string {
  return `miniapp:${role}:profile`;
}

function parseStoredProfile(raw: string | null): StoredProfile | null {
  if (!raw) return null;

  try {
    return JSON.parse(raw) as StoredProfile;
  } catch {
    return null;
  }
}

function normalizeStoredProfile(stored: StoredProfile | null, telegramUser: ReturnType<typeof getTelegramUser>): ClientProfileDraft {
  return {
    firstName: stored?.firstName?.trim() || telegramUser?.first_name?.trim() || 'Иван',
    lastName: stored?.lastName?.trim() || telegramUser?.last_name?.trim() || 'Иванов',
    email: stored?.email?.trim() || '',
    phone: stored?.phone?.trim() || '',
    photoUrl: stored?.photoUrl || telegramUser?.photo_url || null,
  };
}

function getTelegramUser() {
  return getTelegramWebApp()?.initDataUnsafe?.user;
}

export function getTelegramUsernameLabel(username: string | undefined): string {
  if (!username) return 'Без никнейма';
  return username.startsWith('@') ? username : `@${username}`;
}

export function getRoleLabel(role: AppRole): string {
  return ROLE_LABELS[role];
}

export function loadRoleProfileDraft(role: AppRole): ClientProfileDraft {
  const stored = parseStoredProfile(localStorage.getItem(getRoleProfileStorageKey(role)));
  const telegramUser = getTelegramUser();

  return normalizeStoredProfile(stored, telegramUser);
}

export function loadRoleProfileView(role: AppRole): ClientProfileView {
  const telegramUser = getTelegramUser();
  const draft = loadRoleProfileDraft(role);

  return {
    ...draft,
    username: getTelegramUsernameLabel(telegramUser?.username),
    roleLabel: getRoleLabel(role),
  };
}

export async function saveRoleProfileDraft(role: AppRole, profile: ClientProfileDraft): Promise<void> {
  const payload: StoredProfile = {
    firstName: profile.firstName.trim(),
    lastName: profile.lastName.trim(),
    email: profile.email.trim(),
    phone: profile.phone.trim(),
    photoUrl: profile.photoUrl,
    updatedAt: Date.now(),
  };

  localStorage.setItem(getRoleProfileStorageKey(role), JSON.stringify(payload));
  await setDeviceStorageItem(getRoleProfileStorageKey(role), JSON.stringify(payload));
  await persistRoleProfile(role, {
    first_name: payload.firstName ?? '',
    last_name: payload.lastName ?? '',
    email: payload.email ?? '',
    phone: payload.phone ?? '',
    photo_url: payload.photoUrl ?? null,
  });
}

export async function loadRoleProfileDraftFromDeviceStorage(role: AppRole): Promise<ClientProfileDraft | null> {
  const raw = await getDeviceStorageItem(getRoleProfileStorageKey(role));
  const stored = parseStoredProfile(raw);
  if (!stored) return null;

  return normalizeStoredProfile(stored, getTelegramUser());
}

export async function loadRoleProfileDraftFromBackend(role: AppRole): Promise<ClientProfileDraft | null> {
  const remoteProfile = await fetchRoleProfile(role);
  if (!remoteProfile) return null;
  return normalizeStoredProfile(
    {
      firstName: remoteProfile.first_name,
      lastName: remoteProfile.last_name,
      email: remoteProfile.email,
      phone: remoteProfile.phone,
      photoUrl: remoteProfile.photo_url ?? null,
    } as StoredProfile,
    getTelegramUser(),
  );
}

export function remoteProfileToDraft(profile: RemoteProfilePayload): ClientProfileDraft {
  return {
    firstName: profile.first_name ?? '',
    lastName: profile.last_name ?? '',
    email: profile.email ?? '',
    phone: profile.phone ?? '',
    photoUrl: profile.photo_url ?? null,
  };
}

export function getClientProfileDisplayName(profile: Pick<ClientProfileView, 'firstName' | 'lastName'>): string {
  return `${profile.firstName} ${profile.lastName}`.trim();
}
