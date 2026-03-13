import { useEffect } from 'react';
import { getTelegramWebApp, triggerHapticImpact } from '@/shared/telegram/webApp';

type UseTelegramBackButtonInput = {
  enabled: boolean;
  onBack: () => void;
};

export function useTelegramBackButton({ enabled, onBack }: UseTelegramBackButtonInput): void {
  useEffect(() => {
    const backButton = getTelegramWebApp()?.BackButton;
    if (!backButton) return;

    const onBackWithHaptic = () => {
      triggerHapticImpact('light');
      onBack();
    };

    if (!enabled) {
      backButton.offClick?.(onBackWithHaptic);
      backButton.hide?.();
      return;
    }

    backButton.show?.();
    backButton.offClick?.(onBackWithHaptic);
    backButton.onClick?.(onBackWithHaptic);

    return () => {
      backButton.offClick?.(onBackWithHaptic);
      backButton.hide?.();
    };
  }, [enabled, onBack]);
}
