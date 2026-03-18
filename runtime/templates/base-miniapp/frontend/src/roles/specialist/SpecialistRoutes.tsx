import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/app/layout/AppShell';
import { SpecialistHomePage } from '@/roles/specialist/pages/SpecialistHomePage';
import { SpecialistProfilePage } from '@/roles/specialist/pages/SpecialistProfile/SpecialistProfilePage';

export function SpecialistRoutes(): JSX.Element {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<SpecialistHomePage />} />
        <Route path="specialist" element={<SpecialistHomePage />} />
        <Route path="profile" element={<SpecialistProfilePage />} />
        <Route path="specialist/profile" element={<SpecialistProfilePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
