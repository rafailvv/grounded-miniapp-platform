import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useCallback, useEffect } from 'react';
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

  useEffect(() => {
    window.parent?.postMessage(
      {
        type: 'runtime-preview-route',
        path: location.pathname,
      },
      '*',
    );
  }, [location.pathname]);

  useEffect(() => {
    function handlePreviewCommand(event: MessageEvent) {
      const payload = event.data;
      if (!payload || typeof payload !== 'object' || payload.type !== 'runtime-preview-command') {
        return;
      }
      if (payload.command === 'refresh') {
        window.location.reload();
        return;
      }
      if (payload.command === 'close') {
        navigate('/');
        return;
      }
      if (payload.command === 'back') {
        if (location.pathname === '/') {
          navigate('/');
          return;
        }
        navigate(-1);
      }
    }

    window.addEventListener('message', handlePreviewCommand);
    return () => window.removeEventListener('message', handlePreviewCommand);
  }, [location.pathname, navigate]);

  return (
    <main className={styles.content}>
      <Outlet />
    </main>
  );
}
