import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/app/layout/AppShell';
import { RuntimeManifestProvider, useRuntimeManifest } from '@/shared/runtime/RuntimeManifestProvider';
import type { AppRole } from '@/shared/roles/role';
import { GeneratedRoleScreen } from '@/shared/ui/generated/GeneratedRoleScreen';

type GeneratedRoleRoutesProps = {
  role: AppRole;
};

function toNestedPath(path: string): string {
  return path === '/' ? '' : path.replace(/^\//, '');
}

function RoleRouteContent({ role }: GeneratedRoleRoutesProps): JSX.Element {
  const { manifest, loading, error } = useRuntimeManifest();

  if (loading && !manifest) {
    return <div style={{ padding: 20 }}>Loading runtime…</div>;
  }

  if (!manifest) {
    return <div style={{ padding: 20 }}>{error ?? 'Runtime manifest is unavailable.'}</div>;
  }

  if (!manifest.routes.length) {
    return <></>;
  }

  return (
    <Routes>
      <Route element={<AppShell />}>
        {manifest.routes.map((route) =>
          route.path === '/' ? (
            <Route key={route.route_id} index element={<GeneratedRoleScreen role={role} screenId={route.screen_id} />} />
          ) : (
            <Route
              key={route.route_id}
              path={toNestedPath(route.path)}
              element={<GeneratedRoleScreen role={role} screenId={route.screen_id} />}
            />
          ),
        )}
        <Route path="*" element={<Navigate replace to={manifest.entry_path} />} />
      </Route>
    </Routes>
  );
}

export function GeneratedRoleRoutes({ role }: GeneratedRoleRoutesProps): JSX.Element {
  return (
    <RuntimeManifestProvider role={role}>
      <RoleRouteContent role={role} />
    </RuntimeManifestProvider>
  );
}
