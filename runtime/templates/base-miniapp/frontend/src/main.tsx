import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from '@/app/App';
import '@/shared/styles/global.css';
import { initTelegramMock } from '@/shared/telegram/mockTelegram';

initTelegramMock();
lockPageZoom();
setupStartupLoaderDismiss();

function lockPageZoom(): void {
  document.addEventListener(
    'touchstart',
    (event) => {
      if (event.touches.length > 1) {
        event.preventDefault();
      }
    },
    { passive: false },
  );

  let lastTouchEnd = 0;
  document.addEventListener(
    'touchend',
    (event) => {
      const now = Date.now();
      if (now - lastTouchEnd <= 300) {
        event.preventDefault();
      }
      lastTouchEnd = now;
    },
    { passive: false },
  );

  document.addEventListener(
    'wheel',
    (event) => {
      if (event.ctrlKey) {
        event.preventDefault();
      }
    },
    { passive: false },
  );

  const preventGestureZoom = (event: Event) => event.preventDefault();
  document.addEventListener('gesturestart', preventGestureZoom, { passive: false });
  document.addEventListener('gesturechange', preventGestureZoom, { passive: false });
  document.addEventListener('gestureend', preventGestureZoom, { passive: false });
}

function setupStartupLoaderDismiss(): void {
  const loader = document.getElementById('startup-loader');
  if (!loader) return;

  const dismiss = () => {
    if (loader.dataset.hidden === '1') return;
    loader.dataset.hidden = '1';
    window.requestAnimationFrame(() => {
      loader.classList.add('startup-loader--hide');
      window.setTimeout(() => loader.remove(), 220);
    });
  };

  window.addEventListener(
    'app-loader-ready',
    () => {
      window.setTimeout(dismiss, 120);
    },
    { once: true },
  );
  window.setTimeout(dismiss, 7000);
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
