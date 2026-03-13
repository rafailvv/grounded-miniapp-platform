import { useEffect } from 'react';

type UseIOSSwipeBackInput = {
  enabled: boolean;
  onBack: () => void;
};

const MIN_BACK_SWIPE_DISTANCE_PX = 72;
const MAX_VERTICAL_DRIFT_PX = 64;
const HORIZONTAL_DOMINANCE_RATIO = 1.2;
const HORIZONTAL_GESTURE_HOST_SELECTOR = [
  '[data-horizontal-scroll]',
  '[data-carousel]',
  '[data-slider]',
  '[data-swiper]',
  '.swiper',
  '.swiper-container',
  '.carousel',
  '.slider',
].join(', ');

function isSupportedSwipeBackDevice(): boolean {
  return /iPhone|Android/i.test(window.navigator.userAgent);
}

function hasSelectedText(): boolean {
  const selection = window.getSelection();
  if (selection && !selection.isCollapsed && selection.toString().trim().length > 0) {
    return true;
  }

  const activeElement = document.activeElement;
  if (activeElement instanceof HTMLInputElement || activeElement instanceof HTMLTextAreaElement) {
    const hasInputSelection =
      typeof activeElement.selectionStart === 'number' &&
      typeof activeElement.selectionEnd === 'number' &&
      activeElement.selectionStart !== activeElement.selectionEnd;

    if (hasInputSelection) {
      return true;
    }
  }

  return false;
}

function isHorizontallyScrollable(element: HTMLElement): boolean {
  const style = window.getComputedStyle(element);
  const overflowX = style.overflowX;
  const canScrollX =
    (overflowX === 'auto' || overflowX === 'scroll' || overflowX === 'overlay') &&
    element.scrollWidth > element.clientWidth + 1;
  const usesHorizontalGesture = style.touchAction.includes('pan-x');

  return canScrollX || usesHorizontalGesture;
}

function isInsideHorizontalGestureZone(target: EventTarget | null): boolean {
  const element = target instanceof Element ? target : null;
  if (!element) return false;

  if (element.closest(HORIZONTAL_GESTURE_HOST_SELECTOR)) {
    return true;
  }

  let node: Element | null = element;
  while (node && node !== document.body) {
    if (node instanceof HTMLElement && isHorizontallyScrollable(node)) {
      return true;
    }
    node = node.parentElement;
  }

  return false;
}

export function useIOSSwipeBack({ enabled, onBack }: UseIOSSwipeBackInput): void {
  useEffect(() => {
    if (!enabled || !isSupportedSwipeBackDevice()) return;

    let tracking = false;
    let canceled = false;
    let startX = 0;
    let startY = 0;
    let lastX = 0;
    let lastY = 0;

    const onTouchStart = (event: TouchEvent) => {
      if (event.touches.length !== 1) return;
      if (hasSelectedText()) return;
      if (isInsideHorizontalGestureZone(event.target)) return;

      const touch = event.touches[0];

      tracking = true;
      canceled = false;
      startX = touch.clientX;
      startY = touch.clientY;
      lastX = touch.clientX;
      lastY = touch.clientY;
    };

    const onTouchMove = (event: TouchEvent) => {
      if (!tracking || event.touches.length !== 1) return;

      const touch = event.touches[0];
      const deltaX = touch.clientX - startX;
      const deltaY = touch.clientY - startY;

      lastX = touch.clientX;
      lastY = touch.clientY;

      if (deltaX < 0) {
        canceled = true;
        return;
      }

      if (Math.abs(deltaY) > MAX_VERTICAL_DRIFT_PX && Math.abs(deltaY) > deltaX) {
        canceled = true;
      }
    };

    const onTouchCancel = () => {
      tracking = false;
      canceled = true;
    };

    const onTouchEnd = () => {
      if (!tracking) return;

      tracking = false;
      if (canceled || hasSelectedText()) return;

      const deltaX = lastX - startX;
      const deltaY = lastY - startY;
      const isHorizontalSwipe = deltaX > Math.abs(deltaY) * HORIZONTAL_DOMINANCE_RATIO;
      const isLongEnough = deltaX >= MIN_BACK_SWIPE_DISTANCE_PX;

      if (isHorizontalSwipe && isLongEnough) {
        onBack();
      }
    };

    window.addEventListener('touchstart', onTouchStart, { passive: true });
    window.addEventListener('touchmove', onTouchMove, { passive: true });
    window.addEventListener('touchend', onTouchEnd, { passive: true });
    window.addEventListener('touchcancel', onTouchCancel, { passive: true });

    return () => {
      window.removeEventListener('touchstart', onTouchStart);
      window.removeEventListener('touchmove', onTouchMove);
      window.removeEventListener('touchend', onTouchEnd);
      window.removeEventListener('touchcancel', onTouchCancel);
    };
  }, [enabled, onBack]);
}
