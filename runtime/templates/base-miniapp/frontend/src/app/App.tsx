import { useEffect, useState } from 'react';
import { RoleRouter } from '@/app/routing/RoleRouter';
import { useAppBootstrap } from '@/app/bootstrap/useAppBootstrap';
import { useTelegramViewport } from '@/shared/telegram/useTelegramViewport';
import { triggerHapticImpact } from '@/shared/telegram/webApp';
import { useAppTheme } from '@/shared/theme/useAppTheme';
import { Loader } from '@/shared/ui/loader/Loader';

const LOADER_FADE_DURATION_MS = 240;

export function App(): JSX.Element {
  useTelegramViewport();
  useAppTheme();
  const state = useAppBootstrap();
  const [isLoaderFadingOut, setIsLoaderFadingOut] = useState(false);
  const [isLoaderHidden, setIsLoaderHidden] = useState(false);

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

  useEffect(() => {
    if (!isBootstrapResolved || isLoaderFadingOut || isLoaderHidden) return;

    setIsLoaderFadingOut(true);

    const timer = window.setTimeout(() => {
      setIsLoaderHidden(true);
    }, LOADER_FADE_DURATION_MS);

    return () => {
      window.clearTimeout(timer);
    };
  }, [isBootstrapResolved, isLoaderFadingOut, isLoaderHidden]);

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
      {shouldShowLoader ? <Loader isFadingOut={isLoaderFadingOut} /> : null}
    </>
  );
}
