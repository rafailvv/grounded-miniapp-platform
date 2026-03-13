import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/app/layout/AppShell';
import { ManagerHomePage } from '@/roles/manager/pages/ManagerHomePage';
import { ManagerProfilePage } from '@/roles/manager/pages/ManagerProfile/ManagerProfilePage';

export function ManagerRoutes(): JSX.Element {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<ManagerHomePage />} />
        <Route path="/profile" element={<ManagerProfilePage />} />
        <Route path="*" element={<Navigate replace to="/" />} />
      </Route>
    </Routes>
  );
}
