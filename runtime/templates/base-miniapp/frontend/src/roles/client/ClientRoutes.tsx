import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/app/layout/AppShell';
import { ClientHomePage } from '@/roles/client/pages/ClientHomePage';
import { ClientProfilePage } from '@/roles/client/pages/ClientProfile/ClientProfilePage';

export function ClientRoutes(): JSX.Element {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<ClientHomePage />} />
        <Route path="client" element={<ClientHomePage />} />
        <Route path="profile" element={<ClientProfilePage />} />
        <Route path="client/profile" element={<ClientProfilePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
