import { useEffect } from 'react';
import styles from '@/shared/ui/Loader.module.css';

type LoaderProps = {
  isFadingOut?: boolean;
  isHeartbeat?: boolean;
};

export function Loader({ isFadingOut = false, isHeartbeat = false }: LoaderProps): JSX.Element {
  useEffect(() => {
    window.dispatchEvent(new Event('app-loader-ready'));
  }, []);

  return (
    <div className={`${styles.loaderWrapper} ${isFadingOut ? styles.fadeOut : ''}`} aria-label="loading">
      <div className={styles.background} />
      <svg
        className={`${styles.logo} ${isHeartbeat ? styles.heartbeat : ''}`}
        xmlns="http://www.w3.org/2000/svg"
        width="100"
        height="70"
        fill="none"
        viewBox="0 0 54 38"
      >
        <path
          className={styles.basePath}
          d="M53.5 9.7A11 11 0 0 0 50.1 3 10.3 10.3 0 0 0 42.8.5c-3 0-5.5.7-8 2.2A17.3 17.3 0 0 0 29 9.2V1.1h-4.5l.1 22.3c-.9 2.9-2.6 5.3-4.7 7a10.8 10.8 0 0 1-7.2 2.9c-2.7 0-4.7-1-6-3-1.3-2-2-5-2-9.2V1H.2v21.8s0 3.3.4 5.4A11 11 0 0 0 3.9 35c1.8 1.7 4.2 2.6 7.2 2.6s5.6-.8 8-2.3a17 17 0 0 0 5.6-6V37h4.7V15A15 15 0 0 1 34 7.5c2.2-1.8 4.6-2.8 7.3-2.8s4.7 1 6 3c1.2 1.9 1.9 5 1.9 9.2v20h4.7V15s0-3.2-.4-5.3Z"
        />
        <path
          className={styles.animatedPath}
          fill="none"
          stroke="#f2f2f2"
          d="M53.5 9.7A11 11 0 0 0 50.1 3 10.3 10.3 0 0 0 42.8.5c-3 0-5.5.7-8 2.2A17.3 17.3 0 0 0 29 9.2V1.1h-4.5l.1 22.3c-.9 2.9-2.6 5.3-4.7 7a10.8 10.8 0 0 1-7.2 2.9c-2.7 0-4.7-1-6-3-1.3-2-2-5-2-9.2V1H.2v21.8s0 3.3.4 5.4A11 11 0 0 0 3.9 35c1.8 1.7 4.2 2.6 7.2 2.6s5.6-.8 8-2.3a17 17 0 0 0 5.6-6V37h4.7V15A15 15 0 0 1 34 7.5c2.2-1.8 4.6-2.8 7.3-2.8s4.7 1 6 3c1.2 1.9 1.9 5 1.9 9.2v20h4.7V15s0-3.2-.4-5.3Z"
        />
      </svg>
    </div>
  );
}
