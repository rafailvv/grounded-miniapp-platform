import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/app/layout/AppShell';
import { RuntimeManifestProvider, useRuntimeManifest } from '@/shared/runtime/RuntimeManifestProvider';
import type { AppRole } from '@/shared/roles/role';
import { GeneratedRoleScreen } from '@/shared/ui/generated/GeneratedRoleScreen';
import { RoleProfileEditorPage } from '@/shared/ui/templates/RoleProfileEditorPage';

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

  const entryScreen = manifest.routes.find((route) => route.is_entry)?.screen_id ?? Object.keys(manifest.screens)[0] ?? '__fallback__';

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<GeneratedRoleScreen role={role} screenId={entryScreen} />} />
        <Route path="profile" element={<RoleProfileEditorPage role={role} />} />
        <Route path="*" element={<Navigate replace to="/" />} />
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
