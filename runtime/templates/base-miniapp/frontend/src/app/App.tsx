import { useEffect, useState } from 'react';
import { RoleRouter } from '@/app/routing/RoleRouter';
import { useAppBootstrap } from '@/app/bootstrap/useAppBootstrap';
import { Loader } from '@/shared/ui/Loader';
import { useTelegramViewport } from '@/shared/telegram/useTelegramViewport';
import { triggerHapticImpact } from '@/shared/telegram/webApp';
import { useAppTheme } from '@/shared/theme/useAppTheme';

const MIN_LOADER_TIME_MS = 1500;
const MOCK_API_LOADING_TIME_MS = 1000;
const LOADER_FADE_DURATION_MS = 900;

export function App(): JSX.Element {
  useTelegramViewport();
  useAppTheme();
  const state = useAppBootstrap();
  const [isMinLoaderTimePassed, setIsMinLoaderTimePassed] = useState(false);
  const [isMockApiTimerDone, setIsMockApiTimerDone] = useState(false);
  const [isLoaderFadingOut, setIsLoaderFadingOut] = useState(false);
  const [isLoaderHidden, setIsLoaderHidden] = useState(false);

  useEffect(() => {
    const minTimer = window.setTimeout(() => {
      setIsMinLoaderTimePassed(true);
    }, MIN_LOADER_TIME_MS);
    const mockApiTimer = window.setTimeout(() => {
      setIsMockApiTimerDone(true);
    }, MOCK_API_LOADING_TIME_MS);

    return () => {
      window.clearTimeout(minTimer);
      window.clearTimeout(mockApiTimer);
    };
  }, []);

  useEffect(() => {
    const onDocumentClick = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;

      const buttonLike = target.closest(
        'button, [role="button"], input[type="button"], input[type="submit"], input[type="reset"]',
      );
      if (!buttonLike) return;

      if (buttonLike instanceof HTMLButtonElement && buttonLike.disabled) return;
      if (buttonLike instanceof HTMLInputElement && buttonLike.disabled) return;
      if ((buttonLike as HTMLElement).getAttribute('aria-disabled') === 'true') return;

      triggerHapticImpact('light');
    };

    document.addEventListener('click', onDocumentClick, true);
    return () => {
      document.removeEventListener('click', onDocumentClick, true);
    };
  }, []);

  const isBootstrapResolved = state.status !== 'loading';
  const canStartLoaderFade = isBootstrapResolved && isMinLoaderTimePassed && isMockApiTimerDone;
  const isHeartbeatActive = isMinLoaderTimePassed && !isMockApiTimerDone && !isLoaderFadingOut;

  useEffect(() => {
    if (!canStartLoaderFade || isLoaderFadingOut || isLoaderHidden) return;

    setIsLoaderFadingOut(true);

    const timer = window.setTimeout(() => {
      setIsLoaderHidden(true);
    }, LOADER_FADE_DURATION_MS);

    return () => {
      window.clearTimeout(timer);
    };
  }, [canStartLoaderFade, isLoaderFadingOut, isLoaderHidden]);

  const content =
    state.status === 'ready' ? (
      <RoleRouter role={state.role} />
    ) : state.status === 'error' ? (
      <div style={{ padding: 20 }}>{state.message}</div>
    ) : null;

  const shouldShowLoader = !isLoaderHidden || content === null;

  return (
    <>
      {content}
      {shouldShowLoader ? <Loader isFadingOut={isLoaderFadingOut} isHeartbeat={isHeartbeatActive} /> : null}
    </>
  );
}
