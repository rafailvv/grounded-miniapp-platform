export const APP_ROLES = ['client', 'specialist', 'manager'] as const;

export type AppRole = (typeof APP_ROLES)[number];

export const DEFAULT_APP_ROLE: AppRole = 'client';

export function normalizeRole(rawRole: string | null | undefined): AppRole | null {
  if (!rawRole) return null;

  const role = rawRole.trim().toLowerCase();

  if (role === 'expert') return 'specialist';
  if (role === 'specialist') return 'specialist';
  if (role === 'manager') return 'manager';
  if (role === 'client') return 'client';

  return null;
}
