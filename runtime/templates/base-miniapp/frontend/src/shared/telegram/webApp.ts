export type TelegramBackButton = {
  show?: () => void;
  hide?: () => void;
  onClick?: (handler: () => void) => void;
  offClick?: (handler: () => void) => void;
};

export type TelegramHapticFeedback = {
  impactOccurred?: (style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft') => TelegramHapticFeedback | void;
  notificationOccurred?: (type: 'error' | 'success' | 'warning') => TelegramHapticFeedback | void;
  selectionChanged?: () => TelegramHapticFeedback | void;
};

export type TelegramWebApp = {
  initData?: string;
  initDataUnsafe?: {
    user?: {
      id?: number;
      first_name?: string;
      last_name?: string;
      username?: string;
      photo_url?: string;
    };
    start_param?: string;
  };
  platform?: string;
  colorScheme?: 'light' | 'dark';
  ready?: () => void;
  close?: () => void;
  expand?: () => void;
  requestFullscreen?: () => void | Promise<void>;
  disableVerticalSwipes?: () => void;
  onEvent?: (eventType: string, handler: () => void) => void;
  offEvent?: (eventType: string, handler: () => void) => void;
  BackButton?: TelegramBackButton;
  HapticFeedback?: TelegramHapticFeedback;
};

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp;
    };
  }
}

export function getTelegramWebApp(): TelegramWebApp | null {
  return window.Telegram?.WebApp ?? null;
}

export function isTelegramEnvironment(): boolean {
  const webApp = getTelegramWebApp();
  return Boolean(webApp && typeof webApp.initDataUnsafe === 'object');
}

export function getTelegramStartParam(): string | null {
  return getTelegramWebApp()?.initDataUnsafe?.start_param ?? null;
}

export function triggerHapticImpact(style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft' = 'light'): void {
  try {
    getTelegramWebApp()?.HapticFeedback?.impactOccurred?.(style);
  } catch {
    // no-op
  }
}

export function triggerHapticNotification(type: 'error' | 'success' | 'warning'): void {
  try {
    getTelegramWebApp()?.HapticFeedback?.notificationOccurred?.(type);
  } catch {
    // no-op
  }
}
