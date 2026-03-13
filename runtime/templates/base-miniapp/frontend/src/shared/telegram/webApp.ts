export type TelegramBackButton = {
  show?: () => void;
  hide?: () => void;
  onClick?: (handler: () => void) => void;
  offClick?: (handler: () => void) => void;
};

type StorageCallback<T> = (error: string | null, value?: T) => void;
const DEVICE_STORAGE_TIMEOUT_MS = 1500;

export type TelegramHapticFeedback = {
  impactOccurred?: (style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft') => TelegramHapticFeedback | void;
  notificationOccurred?: (type: 'error' | 'success' | 'warning') => TelegramHapticFeedback | void;
  selectionChanged?: () => TelegramHapticFeedback | void;
};

export type TelegramDeviceStorage = {
  setItem?: (key: string, value: string, callback?: StorageCallback<boolean>) => TelegramDeviceStorage | void;
  getItem?: (key: string, callback: StorageCallback<string>) => TelegramDeviceStorage | void;
  removeItem?: (key: string, callback?: StorageCallback<boolean>) => TelegramDeviceStorage | void;
  clear?: (callback?: StorageCallback<boolean>) => TelegramDeviceStorage | void;
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
  DeviceStorage?: TelegramDeviceStorage;
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

export function getTelegramUserId(): number | null {
  return getTelegramWebApp()?.initDataUnsafe?.user?.id ?? null;
}

export function getTelegramStartParam(): string | null {
  return getTelegramWebApp()?.initDataUnsafe?.start_param ?? null;
}

export function closeMiniApp(): void {
  getTelegramWebApp()?.close?.();
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

export function getDeviceStorageItem(key: string): Promise<string | null> {
  const deviceStorage = getTelegramWebApp()?.DeviceStorage;
  if (!deviceStorage?.getItem) {
    return Promise.resolve(null);
  }

  return new Promise((resolve) => {
    let settled = false;
    const complete = (value: string | null) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeoutId);
      resolve(value);
    };

    const timeoutId = window.setTimeout(() => complete(null), DEVICE_STORAGE_TIMEOUT_MS);

    try {
      const maybePromise = deviceStorage.getItem?.(key, (error, value) => {
        if (error) {
          complete(null);
          return;
        }

        complete(value ?? null);
      });

      if (maybePromise && typeof (maybePromise as Promise<unknown>).then === 'function') {
        (maybePromise as Promise<unknown>)
          .then((value) => {
            if (typeof value === 'string') {
              complete(value);
              return;
            }
            complete(null);
          })
          .catch(() => complete(null));
      }
    } catch {
      complete(null);
    }
  });
}

export function setDeviceStorageItem(key: string, value: string): Promise<boolean> {
  const deviceStorage = getTelegramWebApp()?.DeviceStorage;
  if (!deviceStorage?.setItem) {
    return Promise.resolve(false);
  }

  return new Promise((resolve) => {
    let settled = false;
    const complete = (status: boolean) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeoutId);
      resolve(status);
    };

    const timeoutId = window.setTimeout(() => complete(false), DEVICE_STORAGE_TIMEOUT_MS);

    try {
      const maybePromise = deviceStorage.setItem?.(key, value, (error, stored) => {
        if (error) {
          complete(false);
          return;
        }

        complete(typeof stored === 'boolean' ? stored : true);
      });

      if (maybePromise && typeof (maybePromise as Promise<unknown>).then === 'function') {
        (maybePromise as Promise<unknown>)
          .then((stored) => {
            if (typeof stored === 'boolean') {
              complete(stored);
              return;
            }
            complete(true);
          })
          .catch(() => complete(false));
      }
    } catch {
      complete(false);
    }
  });
}
