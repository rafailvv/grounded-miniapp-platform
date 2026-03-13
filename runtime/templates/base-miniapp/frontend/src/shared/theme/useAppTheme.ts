import { useEffect } from 'react';
import { getTelegramWebApp } from '@/shared/telegram/webApp';

export type AppTheme = 'light' | 'dark';

function resolveSystemTheme(): AppTheme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function resolveTheme(): AppTheme {
  const telegramTheme = getTelegramWebApp()?.colorScheme;
  if (telegramTheme === 'dark' || telegramTheme === 'light') {
    return telegramTheme;
  }

  return resolveSystemTheme();
}

function applyTheme(theme: AppTheme): void {
  document.documentElement.dataset.theme = theme;
}

export function useAppTheme(): void {
  useEffect(() => {
    applyTheme(resolveTheme());

    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onMediaChange = () => {
      applyTheme(resolveTheme());
    };

    const telegram = getTelegramWebApp();
    const onTelegramThemeChange = () => {
      applyTheme(resolveTheme());
    };

    media.addEventListener('change', onMediaChange);
    telegram?.onEvent?.('themeChanged', onTelegramThemeChange);

    return () => {
      media.removeEventListener('change', onMediaChange);
      telegram?.offEvent?.('themeChanged', onTelegramThemeChange);
    };
  }, []);
}
