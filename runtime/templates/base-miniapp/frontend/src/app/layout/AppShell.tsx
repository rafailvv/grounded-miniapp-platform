import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useCallback } from 'react';
import { useTelegramBackButton } from '@/shared/telegram/useTelegramBackButton';
import { useIOSSwipeBack } from '@/shared/gestures/useIOSSwipeBack';
import styles from '@/app/layout/AppShell.module.css';

export function AppShell(): JSX.Element {
  const location = useLocation();
  const navigate = useNavigate();

  const isRootPage = location.pathname === '/';

  const showNavigate = useCallback(
    (step: number) => {
      navigate(step);
    },
    [navigate],
  );

  const onBackAction = useCallback(() => {
    showNavigate(-1);
  }, [showNavigate]);

  useTelegramBackButton({
    enabled: !isRootPage,
    onBack: onBackAction,
  });
  useIOSSwipeBack({
    enabled: !isRootPage,
    onBack: onBackAction,
  });

  return (
    <main className={styles.content}>
      <Outlet />
    </main>
  );
}
