import type { JSX } from 'react';
import type { AppRole } from '@/shared/roles/role';
import { ClientRoutes } from '@/roles/client/ClientRoutes';
import { SpecialistRoutes } from '@/roles/specialist/SpecialistRoutes';
import { ManagerRoutes } from '@/roles/manager/ManagerRoutes';

type RoleRouterProps = {
  role: AppRole;
};

const ROUTER_BY_ROLE: Record<AppRole, () => JSX.Element> = {
  client: ClientRoutes,
  specialist: SpecialistRoutes,
  manager: ManagerRoutes,
};

export function RoleRouter({ role }: RoleRouterProps): JSX.Element {
  const RoutesByRole = ROUTER_BY_ROLE[role];
  return <RoutesByRole />;
}
