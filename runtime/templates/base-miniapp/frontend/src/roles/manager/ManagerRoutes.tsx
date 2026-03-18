import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/app/layout/AppShell';
import { ManagerHomePage } from '@/roles/manager/pages/ManagerHomePage';
import { ManagerProfilePage } from '@/roles/manager/pages/ManagerProfile/ManagerProfilePage';

export function ManagerRoutes(): JSX.Element {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<ManagerHomePage />} />
        <Route path="profile" element={<ManagerProfilePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
