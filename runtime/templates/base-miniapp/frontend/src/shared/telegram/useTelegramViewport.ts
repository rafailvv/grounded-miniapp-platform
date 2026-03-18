import { useEffect } from 'react';
import { getTelegramWebApp } from '@/shared/telegram/webApp';

const MOBILE_REGEXP = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i;
const SAFE_AREA_EXTRA_PX = 60;

function isMobileDevice(): boolean {
  return MOBILE_REGEXP.test(window.navigator.userAgent);
}

function isEmbeddedPreview(): boolean {
  return window.self !== window.top;
}

export function useTelegramViewport(): void {
  useEffect(() => {
    const applyViewportOffset = () => {
      const shouldSetOffset = isMobileDevice() || isEmbeddedPreview();
      const safeAreaOffset = shouldSetOffset
        ? `calc(env(safe-area-inset-top, 0px) + ${SAFE_AREA_EXTRA_PX}px)`
        : '0px';

      document.documentElement.style.setProperty('--telegram-top-safe-offset', safeAreaOffset);
    };

    const requestFullscreen = async () => {
      const webApp = getTelegramWebApp();
      if (!webApp || !isMobileDevice()) return;

      try {
        await webApp.requestFullscreen?.();
      } catch {
        webApp.expand?.();
      }
    };

    const webApp = getTelegramWebApp();
    if (webApp) {
      webApp.ready?.();
      webApp.expand?.();
      webApp.disableVerticalSwipes?.();
    }

    const onFirstPointerDown = () => {
      void requestFullscreen();
    };

    applyViewportOffset();
    void requestFullscreen();
    window.addEventListener('resize', applyViewportOffset);
    window.addEventListener('pointerdown', onFirstPointerDown, { once: true, passive: true });

    return () => {
      window.removeEventListener('resize', applyViewportOffset);
      window.removeEventListener('pointerdown', onFirstPointerDown);
      document.documentElement.style.setProperty('--telegram-top-safe-offset', '0px');
    };
  }, []);
}
