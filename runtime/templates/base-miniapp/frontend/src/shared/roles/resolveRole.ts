import { DEFAULT_APP_ROLE, normalizeRole, type AppRole } from '@/shared/roles/role';

type ResolveRoleInput = {
  queryRole: string | null;
  startParamRole: string | null;
  fallbackRole?: string;
};

export function resolveRole({ queryRole, startParamRole, fallbackRole }: ResolveRoleInput): AppRole {
  return normalizeRole(queryRole) || normalizeRole(startParamRole) || normalizeRole(fallbackRole) || DEFAULT_APP_ROLE;
}
