import type { AppRole } from '@/shared/roles/role';

export type BootstrapState =
  | {
      status: 'loading';
    }
  | {
      status: 'ready';
      role: AppRole;
    }
  | {
      status: 'error';
      message: string;
    };
